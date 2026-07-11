from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import datetime

import pandas as pd

from market_maker.config import load_config, validate_config
from market_maker.data import MarketStateBuilder
from market_maker.models import BookSnapshot, Fill, Liquidity, Quote, QuotePlan, Side
from market_maker.portfolio import Portfolio
from market_maker.risk import InventoryRiskManager
from market_maker.strategy import CoinFlip, MarketMaker, create_strategy


def book(timestamp: str = "2026-03-19 12:00:00") -> BookSnapshot:
    return BookSnapshot(
        timestamp=pd.Timestamp(timestamp),
        bid_prices=(99.9, 99.8, 99.7, 99.6, 99.5),
        ask_prices=(100.1, 100.2, 100.3, 100.4, 100.5),
        bid_sizes=(1.0, 1.0, 1.0, 1.0, 1.0),
        ask_sizes=(1.0, 1.0, 1.0, 1.0, 1.0),
    )


class PortfolioTests(unittest.TestCase):
    def test_round_trip_and_accounting(self) -> None:
        portfolio = Portfolio()
        portfolio.apply_fill(
            Fill(
                pd.Timestamp("2026-03-19"),
                1,
                Side.BUY,
                100.0,
                0.1,
                Liquidity.MAKER,
                0.0,
            )
        )
        portfolio.apply_fill(
            Fill(
                pd.Timestamp("2026-03-19"),
                2,
                Side.SELL,
                101.0,
                0.1,
                Liquidity.MAKER,
                0.0,
            )
        )
        state = portfolio.mark(pd.Timestamp("2026-03-19"), 101.0)
        self.assertAlmostEqual(state.inventory, 0.0)
        self.assertAlmostEqual(state.realized_pnl, 0.1)
        self.assertAlmostEqual(state.equity, 0.1)

    def test_funding_is_separate(self) -> None:
        portfolio = Portfolio()
        portfolio.apply_fill(
            Fill(
                pd.Timestamp("2026-03-19"),
                1,
                Side.BUY,
                100.0,
                1.0,
                Liquidity.MAKER,
                0.0,
            )
        )
        payment = portfolio.apply_funding(0.001, 100.0)
        state = portfolio.mark(pd.Timestamp("2026-03-19"), 100.0)
        self.assertAlmostEqual(payment, -0.1)
        self.assertAlmostEqual(state.funding_pnl, -0.1)
        self.assertAlmostEqual(state.realized_pnl, 0.0)
        self.assertAlmostEqual(state.equity, -0.1)

    def test_fee_is_charged_once(self) -> None:
        portfolio = Portfolio()
        portfolio.apply_fill(
            Fill(
                pd.Timestamp("2026-03-19"),
                1,
                Side.BUY,
                100.0,
                1.0,
                Liquidity.MAKER,
                0.2,
            )
        )
        state = portfolio.mark(pd.Timestamp("2026-03-19"), 100.0)
        self.assertAlmostEqual(state.fees, 0.2)
        self.assertAlmostEqual(state.equity, -0.2)

    def test_invalid_fill_is_rejected(self) -> None:
        portfolio = Portfolio()
        fill = Fill(
            pd.Timestamp("2026-03-19"), 1, Side.BUY, 100.0, 0.0, Liquidity.MAKER, 0.0
        )
        with self.assertRaises(ValueError):
            portfolio.apply_fill(fill)


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config("config/market_maker.yaml")

    def test_duplicate_dates_are_rejected(self) -> None:
        duplicate = replace(
            self.config, dates=(self.config.dates[0], self.config.dates[0])
        )
        with self.assertRaises(ValueError):
            validate_config(duplicate)

    def test_non_finite_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_config(replace(self.config, tick_size=math.nan))


class RiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config("config/market_maker.yaml")
        cls.risk = InventoryRiskManager(cls.config)

    def test_final_day_limit_decreases_linearly(self) -> None:
        start = self.risk.limit_at(datetime(2026, 3, 21, 0, 0))
        noon = self.risk.limit_at(datetime(2026, 3, 21, 12, 0))
        end = self.risk.limit_at(datetime(2026, 3, 22, 0, 0))
        self.assertAlmostEqual(start, 2.0)
        self.assertAlmostEqual(noon, 1.0)
        self.assertAlmostEqual(end, 0.0)

    def test_quotes_are_clipped_at_one_limit(self) -> None:
        market = MarketStateBuilder().apply(book())
        self.assertIsNotNone(market)
        portfolio = Portfolio()
        portfolio.apply_fill(
            Fill(
                pd.Timestamp("2026-03-19"),
                1,
                Side.BUY,
                100.0,
                1.95,
                Liquidity.MAKER,
                0.0,
            )
        )
        state = portfolio.mark(pd.Timestamp("2026-03-19"), 100.0)
        plan = QuotePlan(Quote(Side.BUY, 99.8, 0.1), Quote(Side.SELL, 100.2, 0.1))
        decision = self.risk.apply(plan, state, 2.0)
        self.assertIsNotNone(decision.approved_quotes.bid)
        self.assertTrue(math.isclose(decision.approved_quotes.bid.size, 0.05))
        self.assertAlmostEqual(decision.approved_quotes.ask.size, 0.1)

    def test_final_day_target_reduces_linearly(self) -> None:
        strategy = MarketMaker(self.config)
        start = strategy.inventory_target(datetime(2026, 3, 21, 0, 0), 0.0, 0.4)
        noon = strategy.inventory_target(datetime(2026, 3, 21, 12, 0), 0.0, 0.4)
        end = strategy.inventory_target(datetime(2026, 3, 22, 0, 0), 0.0, 0.4)
        self.assertAlmostEqual(start, 0.4)
        self.assertAlmostEqual(noon, 0.2)
        self.assertAlmostEqual(end, 0.0)

    def test_final_day_quotes_only_toward_target(self) -> None:
        strategy = MarketMaker(self.config)
        market = MarketStateBuilder().apply(book("2026-03-21 12:00:00"))
        self.assertIsNotNone(market)
        portfolio = Portfolio()
        portfolio.apply_fill(
            Fill(
                pd.Timestamp("2026-03-21 12:00:00"),
                1,
                Side.BUY,
                100.0,
                0.3,
                Liquidity.MAKER,
                0.0,
            )
        )
        state = portfolio.mark(pd.Timestamp("2026-03-21 12:00:00"), 100.0)
        plan = strategy.quote(market, state, inventory_limit=1.0, inventory_target=0.2)
        self.assertIsNone(plan.bid)
        self.assertIsNotNone(plan.ask)


class StrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config("config/coin_flip.yaml")
        cls.market = MarketStateBuilder().apply(book())
        cls.portfolio = Portfolio().mark(pd.Timestamp(book().timestamp), 100.0)

    def test_config_selects_named_strategy(self) -> None:
        market_maker = create_strategy(load_config("config/market_maker.yaml"))
        coin_flip = create_strategy(self.config, seed=7)
        self.assertIsInstance(market_maker, MarketMaker)
        self.assertIsInstance(coin_flip, CoinFlip)

    def test_coin_flip_seed_is_repeatable(self) -> None:
        first = CoinFlip(self.config, seed=7)
        second = CoinFlip(self.config, seed=7)
        first_plans = [
            first.quote(self.market, self.portfolio, 2.0, 0.0) for _ in range(20)
        ]
        second_plans = [
            second.quote(self.market, self.portfolio, 2.0, 0.0) for _ in range(20)
        ]
        self.assertEqual(first_plans, second_plans)
        self.assertTrue(
            all((plan.bid is None) != (plan.ask is None) for plan in first_plans)
        )

    def test_coin_flip_creates_random_seed(self) -> None:
        strategy = CoinFlip(self.config)
        self.assertIsInstance(strategy.seed, int)


if __name__ == "__main__":
    unittest.main()
