"""Deterministic event loop, active orders and fill simulation."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, time

import pandas as pd

from .config import BacktestConfig
from .data import MarketStateBuilder
from .models import (
    BacktestResult,
    BookSnapshot,
    Fill,
    FundingUpdate,
    Liquidity,
    MarketEvent,
    MarketOrder,
    MarketState,
    Order,
    Quote,
    QuotePlan,
    Side,
    Trade,
)
from .portfolio import Portfolio
from .report import MonitoringRecorder, finalize_result
from .risk import InventoryRiskManager
from .strategy import SimpleMarketMaker, Strategy

MAX_ACTIVE_ORDERS = 2


class ExecutionSimulator:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self._orders: dict[Side, Order] = {}
        self._next_order_id = 1
        self.counts: Counter[str] = Counter()

    def active_orders(self) -> tuple[Order, ...]:
        return tuple(self._orders.values())

    def reconcile(
        self,
        plan: QuotePlan,
        market: MarketState,
        event_id: int,
        timestamp: pd.Timestamp,
    ) -> None:
        desired = {Side.BUY: plan.bid, Side.SELL: plan.ask}
        for side in (Side.BUY, Side.SELL):
            quote = desired[side]
            current = self._orders.get(side)
            if quote is None:
                if current is not None:
                    self._cancel(side)
                continue
            if quote.side is not side:
                raise ValueError(
                    f"{side.value} plan contains a {quote.side.value} quote"
                )

            if current is not None:
                price_changed = (
                    abs(current.price - quote.price) >= self.config.tick_size - 1e-9
                )
                size_too_large = (
                    current.remaining_size > quote.size + self.config.lot_size / 10
                )
                if not price_changed and not size_too_large:
                    continue
                self._cancel(side)

            self._place(quote, market, event_id, timestamp)

    def cancel_all(self) -> None:
        for side in tuple(self._orders):
            self._cancel(side)

    def enforce_inventory_limit(self, inventory: float, inventory_limit: float) -> None:
        bid = self._orders.get(Side.BUY)
        ask = self._orders.get(Side.SELL)
        if bid is not None and inventory + bid.remaining_size > inventory_limit + 1e-12:
            self._cancel(Side.BUY)
        if (
            ask is not None
            and inventory - ask.remaining_size < -inventory_limit - 1e-12
        ):
            self._cancel(Side.SELL)

    def on_book(self, market: MarketState) -> list[Fill]:
        fills: list[Fill] = []
        for order in tuple(self._orders.values()):
            crossed = (
                order.side is Side.BUY and order.price >= market.best_ask - 1e-9
            ) or (order.side is Side.SELL and order.price <= market.best_bid + 1e-9)
            if crossed:
                if self.config.book_cross_policy == "cancel":
                    self._cancel(order.side)
                    self._increment("book_cross_cancellations")
                    continue
                fill = self._make_fill(
                    timestamp=pd.Timestamp(market.timestamp),
                    order_id=order.order_id,
                    side=order.side,
                    price=order.price,
                    size=order.remaining_size,
                    liquidity=Liquidity.MAKER,
                )
                fills.append(fill)
                del self._orders[order.side]
                self._increment("maker_fills")
                self._increment("book_cross_fills")
                continue
            visible = self._visible_size(order.side, order.price, market)
            if visible is not None:
                order.queue_ahead = min(order.queue_ahead, visible)
        return fills

    def on_trade(self, trade: Trade, event_id: int) -> list[Fill]:
        if trade.size <= 0 or trade.price <= 0:
            raise ValueError(f"invalid trade at {trade.timestamp}")
        order_side = Side.SELL if trade.aggressor_side is Side.BUY else Side.BUY
        order = self._orders.get(order_side)
        if (
            order is None
            or order.created_event_id >= event_id
            or pd.Timestamp(trade.timestamp) <= pd.Timestamp(order.created_at)
        ):
            return []

        if order.side is Side.BUY:
            if trade.price > order.price + 1e-9:
                return []
            traded_through = trade.price < order.price - 1e-9
        else:
            if trade.price < order.price - 1e-9:
                return []
            traded_through = trade.price > order.price + 1e-9

        if traded_through:
            fill_size = order.remaining_size
        else:
            available = max(0.0, trade.size - order.queue_ahead)
            order.queue_ahead = max(0.0, order.queue_ahead - trade.size)
            fill_size = min(order.remaining_size, available)

        if fill_size < self.config.lot_size / 10:
            return []
        fill = self._make_fill(
            timestamp=pd.Timestamp(trade.timestamp),
            order_id=order.order_id,
            side=order.side,
            price=order.price,
            size=fill_size,
            liquidity=Liquidity.MAKER,
        )
        order.remaining_size -= fill_size
        self._increment("maker_fills")
        if order.remaining_size < self.config.lot_size / 10:
            del self._orders[order.side]
        return [fill]

    def execute_taker(
        self, order: MarketOrder, market: MarketState, timestamp: pd.Timestamp
    ) -> Fill:
        if not math.isfinite(order.size) or order.size <= 0:
            raise ValueError(f"invalid taker size at {timestamp}: {order.size}")

        prices = market.ask_prices if order.side is Side.BUY else market.bid_prices
        sizes = market.ask_sizes if order.side is Side.BUY else market.bid_sizes
        remaining = order.size
        notional = 0.0
        executed = 0.0
        for price, available in zip(prices, sizes):
            size = min(remaining, available)
            notional += size * price
            executed += size
            remaining -= size
            if remaining <= self.config.lot_size / 10:
                break
        if remaining > self.config.lot_size / 10:
            raise RuntimeError(
                f"insufficient visible depth for taker liquidation at {timestamp}: "
                f"missing {remaining} ETH"
            )
        fill = self._make_fill(
            timestamp=timestamp,
            order_id=None,
            side=order.side,
            price=notional / executed,
            size=executed,
            liquidity=Liquidity.TAKER,
        )
        self._increment("taker_fills")
        return fill

    def _place(
        self, quote: Quote, market: MarketState, event_id: int, timestamp: pd.Timestamp
    ) -> None:
        if (
            not math.isfinite(quote.price)
            or not math.isfinite(quote.size)
            or quote.price <= 0
            or quote.size <= 0
        ):
            raise ValueError(f"invalid quote at {timestamp}: {quote}")
        visible = self._visible_size(quote.side, quote.price, market)
        if visible is not None:
            queue_ahead = visible
        elif self._outside_known_depth(quote.side, quote.price, market):
            queue_ahead = math.inf
        else:
            queue_ahead = 0.0
        self._orders[quote.side] = Order(
            order_id=self._next_order_id,
            side=quote.side,
            price=quote.price,
            remaining_size=quote.size,
            queue_ahead=queue_ahead,
            created_event_id=event_id,
            created_at=timestamp,
        )
        self._next_order_id += 1
        self._increment("placements")

    def _cancel(self, side: Side) -> None:
        del self._orders[side]
        self._increment("cancellations")

    def _visible_size(
        self, side: Side, price: float, market: MarketState
    ) -> float | None:
        prices = market.bid_prices if side is Side.BUY else market.ask_prices
        sizes = market.bid_sizes if side is Side.BUY else market.ask_sizes
        for level_price, level_size in zip(prices, sizes):
            if math.isclose(level_price, price, abs_tol=self.config.tick_size / 10):
                return level_size
        return None

    def _outside_known_depth(
        self, side: Side, price: float, market: MarketState
    ) -> bool:
        if side is Side.BUY:
            return price < market.bid_prices[-1] - 1e-9
        return price > market.ask_prices[-1] + 1e-9

    def _make_fill(
        self,
        timestamp: pd.Timestamp,
        order_id: int | None,
        side: Side,
        price: float,
        size: float,
        liquidity: Liquidity,
    ) -> Fill:
        fee_bps = (
            self.config.maker_fee_bps
            if liquidity is Liquidity.MAKER
            else self.config.taker_fee_bps
        )
        fee = price * size * fee_bps / 10_000
        return Fill(timestamp, order_id, side, price, size, liquidity, fee)

    def _increment(self, name: str) -> None:
        self.counts[name] += 1


class BacktestEngine:
    def __init__(
        self,
        config: BacktestConfig,
        strategy: Strategy,
        progress: Callable[[int, pd.Timestamp], None] | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.risk = InventoryRiskManager(config)
        self.portfolio = Portfolio()
        self.execution = ExecutionSimulator(config)
        self.market_builder = MarketStateBuilder(config.pressure_levels)
        self.recorder = MonitoringRecorder()
        self.fills: list[Fill] = []
        self.progress = progress
        self._next_decision: pd.Timestamp | None = None
        self._next_record: pd.Timestamp | None = None
        self._funding_times = self._build_funding_times()
        self._funding_index = 0
        self._last_timestamp: pd.Timestamp | None = None
        self._final_day_start_inventory: float | None = None

    def run(self, events: Iterable[MarketEvent]) -> BacktestResult:
        event_count = 0
        batch_timestamp: pd.Timestamp | None = None
        batch: list[MarketEvent] = []
        next_progress = 500_000
        for event in events:
            timestamp = pd.Timestamp(event.timestamp)
            if pd.isna(timestamp):
                raise ValueError("event timestamp cannot be NaT")
            if batch_timestamp is not None and timestamp < batch_timestamp:
                raise ValueError(
                    f"timestamps moved backwards: {timestamp} < {batch_timestamp}"
                )
            if batch_timestamp is not None and timestamp != batch_timestamp:
                event_count += len(batch)
                self._process_timestamp(batch_timestamp, batch, event_count)
                while self.progress is not None and event_count >= next_progress:
                    self.progress(event_count, batch_timestamp)
                    next_progress += 500_000
                batch = []
            batch_timestamp = timestamp
            batch.append(event)

        if batch_timestamp is not None:
            event_count += len(batch)
            self._process_timestamp(batch_timestamp, batch, event_count)
            while self.progress is not None and event_count >= next_progress:
                self.progress(event_count, batch_timestamp)
                next_progress += 500_000

        if (
            event_count == 0
            or self._last_timestamp is None
            or self.market_builder.state is None
        ):
            raise RuntimeError("event stream is empty")
        self._finalize(self._last_timestamp, self.market_builder.state)
        result = BacktestResult(
            config=self.config,
            monitoring=self.recorder.to_frame(),
            fills=tuple(self.fills),
            order_counts=dict(self.execution.counts),
        )
        return finalize_result(result)

    def _process_timestamp(
        self,
        timestamp: pd.Timestamp,
        events: list[MarketEvent],
        event_id: int,
    ) -> None:
        self._last_timestamp = timestamp
        if (
            self._final_day_start_inventory is None
            and timestamp.date() >= self.config.final_day
        ):
            self._final_day_start_inventory = self.portfolio.inventory
        self._apply_due_funding(timestamp)
        self._prepare_existing_orders(timestamp)

        ordered = sorted(events, key=self._event_priority)
        for event in ordered:
            if isinstance(event, Trade):
                self._apply_fills(self.execution.on_trade(event, event_id))

        for event in ordered:
            if isinstance(event, Trade):
                continue
            market = self.market_builder.apply(event)
            if isinstance(event, BookSnapshot) and market is not None:
                self._apply_fills(self.execution.on_book(market))

        market = self.market_builder.state
        if market is None:
            return
        self._enforce_current_limit(timestamp, market)
        if self._decision_due(timestamp):
            self._make_decision(timestamp, event_id, market)
        if self._record_due(timestamp):
            self._record(timestamp, market)
        self._assert_invariants(timestamp, market)

    @staticmethod
    def _event_priority(event: MarketEvent) -> int:
        if isinstance(event, Trade):
            return 0
        if isinstance(event, FundingUpdate):
            return 1
        return 2

    def _make_decision(
        self, timestamp: pd.Timestamp, event_id: int, market: MarketState
    ) -> None:
        stale_ms = (timestamp - pd.Timestamp(market.timestamp)).total_seconds() * 1_000
        if stale_ms > self.config.stale_book_ms:
            self.execution.cancel_all()
            return

        inventory_limit = self.risk.limit_at(timestamp)
        portfolio = self.portfolio.mark(timestamp, market.mid)
        start_inventory = self._final_day_start_inventory or 0.0
        inventory_target = self.strategy.inventory_target(
            timestamp, market.funding_rate, start_inventory
        )
        desired = self.strategy.quote(
            market, portfolio, inventory_limit, inventory_target
        )
        decision = self.risk.apply(desired, portfolio, inventory_limit)
        if decision.liquidation is not None:
            self.execution.cancel_all()
            fill = self.execution.execute_taker(decision.liquidation, market, timestamp)
            self._apply_fills([fill])
            return
        self.execution.reconcile(decision.approved_quotes, market, event_id, timestamp)

    def _apply_fills(self, fills: list[Fill]) -> None:
        for fill in fills:
            self.portfolio.apply_fill(fill)
            self.fills.append(fill)

    def _apply_due_funding(self, timestamp: pd.Timestamp) -> None:
        while (
            self._funding_index < len(self._funding_times)
            and self._funding_times[self._funding_index] <= timestamp
        ):
            market = self.market_builder.state
            if market is not None:
                self.portfolio.apply_funding(market.funding_rate, market.mid)
            self._funding_index += 1

    def _prepare_existing_orders(self, timestamp: pd.Timestamp) -> None:
        market = self.market_builder.state
        if market is None:
            return
        if self._is_stale(timestamp, market):
            self.execution.cancel_all()
            return
        inventory_limit = self.risk.limit_at(timestamp)
        portfolio = self.portfolio.mark(timestamp, market.mid)
        self.execution.enforce_inventory_limit(portfolio.inventory, inventory_limit)
        if abs(portfolio.inventory) > inventory_limit + 1e-12:
            self.execution.cancel_all()

    def _enforce_current_limit(
        self, timestamp: pd.Timestamp, market: MarketState
    ) -> None:
        inventory_limit = self.risk.limit_at(timestamp)
        portfolio = self.portfolio.mark(timestamp, market.mid)
        self.execution.enforce_inventory_limit(portfolio.inventory, inventory_limit)
        if abs(portfolio.inventory) <= inventory_limit + 1e-12:
            return
        self.execution.cancel_all()
        if self._is_stale(timestamp, market):
            return
        decision = self.risk.apply(QuotePlan.empty(), portfolio, inventory_limit)
        if decision.liquidation is None:
            raise AssertionError(
                "risk manager did not liquidate inventory outside the limit"
            )
        fill = self.execution.execute_taker(decision.liquidation, market, timestamp)
        self._apply_fills([fill])

    def _is_stale(self, timestamp: pd.Timestamp, market: MarketState) -> bool:
        age_ms = (timestamp - pd.Timestamp(market.timestamp)).total_seconds() * 1_000
        return age_ms > self.config.stale_book_ms

    def _assert_invariants(self, timestamp: pd.Timestamp, market: MarketState) -> None:
        limit = self.risk.limit_at(timestamp)
        portfolio = self.portfolio.mark(timestamp, market.mid)
        orders = self.execution.active_orders()
        if len(orders) > MAX_ACTIVE_ORDERS or len(
            {order.side for order in orders}
        ) != len(orders):
            raise AssertionError("more than one active order per side")
        for order in orders:
            if order.side is Side.BUY and order.price >= market.best_ask - 1e-9:
                raise AssertionError("active bid crosses the current ask")
            if order.side is Side.SELL and order.price <= market.best_bid + 1e-9:
                raise AssertionError("active ask crosses the current bid")
            if not math.isclose(
                order.price / self.config.tick_size,
                round(order.price / self.config.tick_size),
                abs_tol=1e-7,
            ):
                raise AssertionError("order price is not aligned to tick size")
        bid_size = sum(
            order.remaining_size for order in orders if order.side is Side.BUY
        )
        ask_size = sum(
            order.remaining_size for order in orders if order.side is Side.SELL
        )
        if portfolio.inventory + bid_size > limit + 1e-9:
            raise AssertionError("active bid can breach the inventory limit")
        if portfolio.inventory - ask_size < -limit - 1e-9:
            raise AssertionError("active ask can breach the inventory limit")
        if (
            not self._is_stale(timestamp, market)
            and abs(portfolio.inventory) > limit + 1e-9
        ):
            raise AssertionError("inventory exceeds the current limit")

    def _decision_due(self, timestamp: pd.Timestamp) -> bool:
        interval = pd.Timedelta(milliseconds=self.config.decision_interval_ms)
        if self._next_decision is None:
            self._next_decision = timestamp
        if timestamp < self._next_decision:
            return False
        while self._next_decision <= timestamp:
            self._next_decision += interval
        return True

    def _record_due(self, timestamp: pd.Timestamp) -> bool:
        interval = pd.Timedelta(milliseconds=self.config.snapshot_interval_ms)
        if self._next_record is None:
            self._next_record = timestamp
        if timestamp < self._next_record:
            return False
        while self._next_record <= timestamp:
            self._next_record += interval
        return True

    def _record(self, timestamp: pd.Timestamp, market: MarketState) -> None:
        portfolio = self.portfolio.mark(timestamp, market.mid)
        start_inventory = self._final_day_start_inventory or 0.0
        inventory_target = self.strategy.inventory_target(
            timestamp, market.funding_rate, start_inventory
        )
        self.recorder.append(
            timestamp,
            market,
            portfolio,
            self.risk.limit_at(timestamp),
            inventory_target,
            self.execution.active_orders(),
            dict(self.execution.counts),
        )

    def _finalize(self, timestamp: pd.Timestamp, market: MarketState) -> None:
        self.execution.cancel_all()
        if not math.isclose(
            self.portfolio.inventory,
            self.config.final_inventory_eth,
            abs_tol=1e-12,
        ):
            if self._is_stale(timestamp, market):
                raise RuntimeError("cannot close final inventory against a stale book")
            side = Side.SELL if self.portfolio.inventory > 0 else Side.BUY
            order = MarketOrder(side, abs(self.portfolio.inventory))
            self._apply_fills([self.execution.execute_taker(order, market, timestamp)])
        if not math.isclose(self.portfolio.inventory, 0.0, abs_tol=1e-9):
            raise AssertionError(
                f"final inventory is not zero: {self.portfolio.inventory}"
            )
        self._record(timestamp, market)
        self._assert_invariants(timestamp, market)

    def _build_funding_times(self) -> list[pd.Timestamp]:
        return [
            pd.Timestamp(datetime.combine(day, time(hour=hour)))
            for day in self.config.dates
            for hour in range(0, 24, self.config.funding_interval_hours)
        ]


def run_backtest(
    config: BacktestConfig,
    events: Iterable[MarketEvent],
    strategy: Strategy | None = None,
    progress: Callable[[int, pd.Timestamp], None] | None = None,
) -> BacktestResult:
    return BacktestEngine(
        config, strategy or SimpleMarketMaker(config), progress=progress
    ).run(events)
