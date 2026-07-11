"""Shared data objects for the simulator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

    from .config import BacktestConfig


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Liquidity(str, Enum):
    MAKER = "maker"
    TAKER = "taker"


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    timestamp: datetime
    bid_prices: tuple[float, ...]
    ask_prices: tuple[float, ...]
    bid_sizes: tuple[float, ...]
    ask_sizes: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Trade:
    timestamp: datetime
    price: float
    size: float
    aggressor_side: Side


@dataclass(frozen=True, slots=True)
class FundingUpdate:
    timestamp: datetime
    rate: float


MarketEvent = BookSnapshot | Trade | FundingUpdate


@dataclass(frozen=True, slots=True)
class MarketState:
    timestamp: datetime
    bid_prices: tuple[float, ...]
    ask_prices: tuple[float, ...]
    bid_sizes: tuple[float, ...]
    ask_sizes: tuple[float, ...]
    best_bid: float
    best_ask: float
    mid: float
    spread: float
    pressure: float
    funding_rate: float


@dataclass(frozen=True, slots=True)
class Quote:
    side: Side
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class QuotePlan:
    bid: Quote | None
    ask: Quote | None

    @classmethod
    def empty(cls) -> "QuotePlan":
        return cls(bid=None, ask=None)


@dataclass(slots=True)
class Order:
    order_id: int
    side: Side
    price: float
    remaining_size: float
    queue_ahead: float
    created_event_id: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MarketOrder:
    side: Side
    size: float


@dataclass(frozen=True, slots=True)
class Fill:
    timestamp: datetime
    order_id: int | None
    side: Side
    price: float
    size: float
    liquidity: Liquidity
    fee: float


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    timestamp: datetime
    inventory: float
    cash: float
    average_entry_price: float
    realized_pnl: float
    unrealized_pnl: float
    funding_pnl: float
    fees: float
    equity: float


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved_quotes: QuotePlan
    liquidation: MarketOrder | None


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    monitoring: pd.DataFrame
    fills: tuple[Fill, ...]
    order_counts: dict[str, int]
    strategy_seed: int | None = None
    metrics: dict[str, float] | None = None
    daily_metrics: pd.DataFrame | None = None
