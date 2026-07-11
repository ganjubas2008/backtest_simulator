"""Streaming order-book, trade and funding data feed."""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from .config import BacktestConfig
from .models import (
    BookSnapshot,
    FundingUpdate,
    MarketEvent,
    MarketState,
    Side,
    Trade,
)


def _book_columns(levels: int) -> list[str]:
    return [
        "datetime",
        *(f"bid_price_{level}" for level in range(1, levels + 1)),
        *(f"ask_price_{level}" for level in range(1, levels + 1)),
        *(f"bid_qty_{level}" for level in range(1, levels + 1)),
        *(f"ask_qty_{level}" for level in range(1, levels + 1)),
    ]


def _iter_books(
    path: Path, levels: int, batch_size: int = 100_000
) -> Iterator[BookSnapshot]:
    columns = _book_columns(levels)
    parquet = pq.ParquetFile(path)
    expected = set(columns)
    available = set(parquet.schema_arrow.names)
    missing = expected - available
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")

    for batch in parquet.iter_batches(columns=columns, batch_size=batch_size):
        frame = batch.to_pandas()
        for row in frame.itertuples(index=False, name=None):
            offset = 1
            bid_prices = tuple(float(value) for value in row[offset : offset + levels])
            offset += levels
            ask_prices = tuple(float(value) for value in row[offset : offset + levels])
            offset += levels
            bid_sizes = tuple(float(value) for value in row[offset : offset + levels])
            offset += levels
            ask_sizes = tuple(float(value) for value in row[offset : offset + levels])
            yield BookSnapshot(
                timestamp=pd.Timestamp(row[0]),
                bid_prices=bid_prices,
                ask_prices=ask_prices,
                bid_sizes=bid_sizes,
                ask_sizes=ask_sizes,
            )


def _iter_trades(path: Path) -> Iterator[Trade]:
    frame = pd.read_parquet(path, columns=["datetime", "price", "size", "is_maker_ask"])
    for timestamp, price, size, is_maker_ask in frame.itertuples(
        index=False, name=None
    ):
        maker_ask = float(is_maker_ask)
        if maker_ask not in (0.0, 1.0):
            raise ValueError(f"invalid is_maker_ask at {timestamp}: {is_maker_ask}")
        yield Trade(
            timestamp=pd.Timestamp(timestamp),
            price=float(price),
            size=float(size),
            aggressor_side=Side.BUY if maker_ask == 1.0 else Side.SELL,
        )


def _iter_funding(path: Path) -> Iterator[FundingUpdate]:
    frame = pd.read_parquet(path, columns=["datetime", "funding_rate"])
    for timestamp, rate in frame.itertuples(index=False, name=None):
        yield FundingUpdate(timestamp=pd.Timestamp(timestamp), rate=float(rate))


def _event_key(event: MarketEvent) -> tuple[pd.Timestamp, int]:
    priority = (
        0 if isinstance(event, Trade) else 1 if isinstance(event, FundingUpdate) else 2
    )
    return pd.Timestamp(event.timestamp), priority


def iter_day_events(data_dir: Path, day: date, levels: int) -> Iterator[MarketEvent]:
    name = f"{day.isoformat()}.parquet"
    streams = (
        _iter_books(data_dir / "orderbook" / name, levels),
        _iter_trades(data_dir / "trades" / name),
        _iter_funding(data_dir / "fundings" / name),
    )
    yield from heapq.merge(*streams, key=_event_key)


def iter_events(config: BacktestConfig) -> Iterator[MarketEvent]:
    missing = [path for path in config.input_paths if not path.is_file()]
    if missing:
        paths = "\n".join(f"- {path}" for path in missing)
        raise FileNotFoundError(f"missing input files:\n{paths}")

    previous_timestamp: pd.Timestamp | None = None
    for day in config.dates:
        for event in iter_day_events(config.data_dir, day, config.execution_levels):
            timestamp = pd.Timestamp(event.timestamp)
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise ValueError(f"timestamps moved backwards at {timestamp}")
            previous_timestamp = timestamp
            yield event


class MarketStateBuilder:
    def __init__(self, pressure_levels: int = 5) -> None:
        self._state: MarketState | None = None
        self._funding_rate = 0.0
        self._pressure_levels = pressure_levels

    @property
    def state(self) -> MarketState | None:
        return self._state

    def apply(self, event: BookSnapshot | FundingUpdate) -> MarketState | None:
        if isinstance(event, FundingUpdate):
            if not math.isfinite(event.rate):
                raise ValueError(f"invalid funding rate at {event.timestamp}")
            self._funding_rate = event.rate
            if self._state is not None:
                self._state = replace(self._state, funding_rate=event.rate)
        elif isinstance(event, BookSnapshot):
            self._state = self._from_book(event)
        return self._state

    def _from_book(self, book: BookSnapshot) -> MarketState:
        depth = len(book.bid_prices)
        arrays = (book.ask_prices, book.bid_sizes, book.ask_sizes)
        if depth < self._pressure_levels or any(
            len(values) != depth for values in arrays
        ):
            raise ValueError(
                f"book at {book.timestamp} must have equal arrays with at least "
                f"{self._pressure_levels} levels"
            )

        values = (*book.bid_prices, *book.ask_prices, *book.bid_sizes, *book.ask_sizes)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError(f"invalid book value at {book.timestamp}")
        if any(
            left <= right for left, right in zip(book.bid_prices, book.bid_prices[1:])
        ):
            raise ValueError(f"bid prices are not descending at {book.timestamp}")
        if any(
            left >= right for left, right in zip(book.ask_prices, book.ask_prices[1:])
        ):
            raise ValueError(f"ask prices are not ascending at {book.timestamp}")

        best_bid = book.bid_prices[0]
        best_ask = book.ask_prices[0]
        if best_bid >= best_ask:
            raise ValueError(
                f"crossed book at {book.timestamp}: {best_bid} >= {best_ask}"
            )

        levels = min(self._pressure_levels, len(book.bid_sizes), len(book.ask_sizes))
        bid_depth = sum(book.bid_sizes[:levels])
        ask_depth = sum(book.ask_sizes[:levels])
        pressure = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        return MarketState(
            timestamp=book.timestamp,
            bid_prices=book.bid_prices,
            ask_prices=book.ask_prices,
            bid_sizes=book.bid_sizes,
            ask_sizes=book.ask_sizes,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=(best_bid + best_ask) / 2,
            spread=best_ask - best_bid,
            pressure=pressure,
            funding_rate=self._funding_rate,
        )
