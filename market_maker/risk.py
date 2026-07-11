"""Single inventory limit and final-day shrinking-limit rules."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from .config import BacktestConfig
from .models import MarketOrder, PortfolioSnapshot, Quote, QuotePlan, RiskDecision, Side


class InventoryRiskManager:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    def limit_at(self, timestamp: datetime) -> float:
        if timestamp.date() < self.config.final_day:
            return self.config.inventory_limit_eth
        if timestamp.date() > self.config.final_day:
            return 0.0

        day_end = datetime.combine(timestamp.date() + timedelta(days=1), time.min)
        remaining = max(0.0, (day_end - timestamp).total_seconds())
        return self.config.inventory_limit_eth * min(1.0, remaining / 86_400.0)

    def apply(
        self,
        quote_plan: QuotePlan,
        portfolio: PortfolioSnapshot,
        inventory_limit: float,
    ) -> RiskDecision:
        inventory = portfolio.inventory
        tolerance = 1e-12

        if abs(inventory) > inventory_limit + tolerance:
            excess = abs(inventory) - inventory_limit
            size = min(abs(inventory), self._ceil_to_lot(excess))
            side = Side.SELL if inventory > 0 else Side.BUY
            return RiskDecision(QuotePlan.empty(), MarketOrder(side, size))

        bid_capacity = max(0.0, inventory_limit - inventory)
        ask_capacity = max(0.0, inventory_limit + inventory)
        bid = self._clip(quote_plan.bid, bid_capacity)
        ask = self._clip(quote_plan.ask, ask_capacity)
        return RiskDecision(QuotePlan(bid, ask), None)

    def _clip(self, quote: Quote | None, capacity: float) -> Quote | None:
        if quote is None:
            return None
        size = self._floor_to_lot(min(quote.size, capacity))
        if size < self.config.lot_size:
            return None
        return Quote(side=quote.side, price=quote.price, size=size)

    def _floor_to_lot(self, size: float) -> float:
        lot = self.config.lot_size
        return round(math.floor(size / lot + 1e-9) * lot, 10)

    def _ceil_to_lot(self, size: float) -> float:
        lot = self.config.lot_size
        return round(math.ceil(size / lot - 1e-9) * lot, 10)
