"""Desired bid and ask calculation without risk or execution logic."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from typing import Protocol

from .config import BacktestConfig
from .models import MarketState, PortfolioSnapshot, Quote, QuotePlan, Side


def _floor_to_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 10)


def _ceil_to_tick(price: float, tick: float) -> float:
    return round(math.ceil(price / tick - 1e-9) * tick, 10)


class Strategy(Protocol):
    def inventory_target(
        self,
        timestamp: datetime,
        funding_rate: float,
        final_day_start_inventory: float,
    ) -> float: ...

    def quote(
        self,
        market: MarketState,
        portfolio: PortfolioSnapshot,
        inventory_limit: float,
        inventory_target: float,
    ) -> QuotePlan: ...


class SimpleMarketMaker:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    def inventory_target(
        self,
        timestamp: datetime,
        funding_rate: float,
        final_day_start_inventory: float,
    ) -> float:
        if timestamp.date() == self.config.final_day:
            day_end = datetime.combine(timestamp.date() + timedelta(days=1), time.min)
            remaining = max(0.0, (day_end - timestamp).total_seconds())
            return final_day_start_inventory * min(1.0, remaining / 86_400.0)
        if timestamp.date() > self.config.final_day:
            return 0.0

        target = -(funding_rate / 0.0001 * self.config.funding_target_per_bp_eth)
        limit = self.config.max_funding_target_eth
        return max(-limit, min(limit, target))

    def quote(
        self,
        market: MarketState,
        portfolio: PortfolioSnapshot,
        inventory_limit: float,
        inventory_target: float,
    ) -> QuotePlan:
        if inventory_limit < self.config.lot_size:
            return QuotePlan.empty()

        tick = self.config.tick_size
        pressure_shift = market.pressure * self.config.max_pressure_shift_ticks * tick
        inventory_scale = max(inventory_limit, self.config.order_size_eth)
        inventory_ratio = (portfolio.inventory - inventory_target) / inventory_scale
        inventory_ratio = max(-1.0, min(1.0, inventory_ratio))
        inventory_shift = inventory_ratio * self.config.max_inventory_shift_ticks * tick

        center = market.mid + pressure_shift - inventory_shift
        half_width = market.spread / 2 + self.config.quote_distance_ticks * tick
        bid_price = min(_floor_to_tick(center - half_width, tick), market.best_bid)
        ask_price = max(_ceil_to_tick(center + half_width, tick), market.best_ask)

        bid: Quote | None = Quote(Side.BUY, bid_price, self.config.order_size_eth)
        ask: Quote | None = Quote(Side.SELL, ask_price, self.config.order_size_eth)
        tolerance = self.config.lot_size / 10
        if portfolio.timestamp.date() == self.config.final_day:
            if portfolio.inventory > inventory_target + tolerance:
                bid = None
            elif portfolio.inventory < inventory_target - tolerance:
                ask = None
        return QuotePlan(bid=bid, ask=ask)
