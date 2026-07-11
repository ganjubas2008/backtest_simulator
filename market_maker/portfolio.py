"""Inventory, cash, fees, funding and profit accounting."""

from __future__ import annotations

import math
from datetime import datetime

from .models import Fill, PortfolioSnapshot, Side


class Portfolio:
    def __init__(self) -> None:
        self.inventory = 0.0
        self.cash = 0.0
        self.average_entry_price = 0.0
        self.realized_pnl = 0.0
        self.funding_pnl = 0.0
        self.fees = 0.0

    def apply_fill(self, fill: Fill) -> None:
        if (
            not math.isfinite(fill.price)
            or not math.isfinite(fill.size)
            or not math.isfinite(fill.fee)
            or fill.price <= 0
            or fill.size <= 0
        ):
            raise ValueError(f"invalid fill at {fill.timestamp}: {fill}")

        signed_size = fill.size if fill.side is Side.BUY else -fill.size
        old_inventory = self.inventory
        new_inventory = old_inventory + signed_size

        self.cash -= signed_size * fill.price
        self.cash -= fill.fee
        self.fees += fill.fee

        if math.isclose(old_inventory, 0.0, abs_tol=1e-12):
            self.average_entry_price = fill.price
        elif old_inventory * signed_size > 0:
            old_notional = abs(old_inventory) * self.average_entry_price
            added_notional = abs(signed_size) * fill.price
            self.average_entry_price = (old_notional + added_notional) / abs(
                new_inventory
            )
        else:
            closed_size = min(abs(old_inventory), abs(signed_size))
            direction = math.copysign(1.0, old_inventory)
            self.realized_pnl += (
                closed_size * (fill.price - self.average_entry_price) * direction
            )
            if math.isclose(new_inventory, 0.0, abs_tol=1e-12):
                self.average_entry_price = 0.0
            elif old_inventory * new_inventory < 0:
                self.average_entry_price = fill.price

        self.inventory = (
            0.0 if math.isclose(new_inventory, 0.0, abs_tol=1e-12) else new_inventory
        )

    def apply_funding(self, rate: float, mark_price: float) -> float:
        if not math.isfinite(rate) or not math.isfinite(mark_price) or mark_price <= 0:
            raise ValueError(
                "funding rate and mark price must be finite; price must be positive"
            )
        payment = -self.inventory * mark_price * rate
        self.cash += payment
        self.funding_pnl += payment
        return payment

    def mark(self, timestamp: datetime, mid: float) -> PortfolioSnapshot:
        if not math.isfinite(mid) or mid <= 0:
            raise ValueError(f"invalid mark price at {timestamp}: {mid}")
        unrealized = self.inventory * (mid - self.average_entry_price)
        equity = self.cash + self.inventory * mid
        expected_equity = self.realized_pnl + unrealized + self.funding_pnl - self.fees
        if not math.isclose(equity, expected_equity, abs_tol=1e-7):
            raise AssertionError(
                f"accounting identity failed at {timestamp}: "
                f"equity={equity}, expected={expected_equity}"
            )
        return PortfolioSnapshot(
            timestamp=timestamp,
            inventory=self.inventory,
            cash=self.cash,
            average_entry_price=self.average_entry_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            funding_pnl=self.funding_pnl,
            fees=self.fees,
            equity=equity,
        )
