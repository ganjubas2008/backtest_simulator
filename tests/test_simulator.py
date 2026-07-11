from __future__ import annotations

import unittest
from dataclasses import replace

import pandas as pd

from market_maker.config import load_config
from market_maker.data import MarketStateBuilder
from market_maker.models import (
    BookSnapshot,
    Fill,
    Liquidity,
    MarketOrder,
    Quote,
    QuotePlan,
    Side,
    Trade,
)
from market_maker.simulator import BacktestEngine, ExecutionSimulator, run_backtest


def book(timestamp: str) -> BookSnapshot:
    return BookSnapshot(
        timestamp=pd.Timestamp(timestamp),
        bid_prices=(99.9, 99.8, 99.7, 99.6, 99.5),
        ask_prices=(100.1, 100.2, 100.3, 100.4, 100.5),
        bid_sizes=(1.0, 1.0, 1.0, 1.0, 1.0),
        ask_sizes=(0.5, 0.5, 1.0, 1.0, 1.0),
    )


def shifted_book(timestamp: str, center: float) -> BookSnapshot:
    return BookSnapshot(
        timestamp=pd.Timestamp(timestamp),
        bid_prices=tuple(center - 0.1 * level for level in range(1, 6)),
        ask_prices=tuple(center + 0.1 * level for level in range(1, 6)),
        bid_sizes=(1.0,) * 5,
        ask_sizes=(1.0,) * 5,
    )


class EmptyStrategy:
    def quote(
        self,
        _market: object,
        _portfolio: object,
        _inventory_limit: float,
        _inventory_target: float,
    ) -> QuotePlan:
        return QuotePlan.empty()

    def inventory_target(
        self,
        _timestamp: object,
        _funding_rate: float,
        _final_day_start_inventory: float,
    ) -> float:
        return 0.0


class ExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config("config/market_maker.yaml")

    def test_new_order_does_not_fill_on_creation_event(self) -> None:
        market = MarketStateBuilder().apply(book("2026-03-19 12:00:00"))
        self.assertIsNotNone(market)
        execution = ExecutionSimulator(self.config)
        plan = QuotePlan(Quote(Side.BUY, 99.8, 0.1), None)
        timestamp = pd.Timestamp("2026-03-19 12:00:00")
        execution.reconcile(plan, market, event_id=10, timestamp=timestamp)
        trade = Trade(timestamp, 99.8, 1.1, Side.SELL)
        self.assertEqual(execution.on_trade(trade, event_id=10), [])
        later_trade = Trade(
            timestamp + pd.Timedelta(milliseconds=1), 99.8, 1.1, Side.SELL
        )
        fills = execution.on_trade(later_trade, event_id=11)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].liquidity, Liquidity.MAKER)
        self.assertAlmostEqual(fills[0].size, 0.1)

    def test_taker_uses_visible_depth(self) -> None:
        market = MarketStateBuilder().apply(book("2026-03-19 12:00:00"))
        self.assertIsNotNone(market)
        execution = ExecutionSimulator(self.config)
        fill = execution.execute_taker(
            MarketOrder(Side.BUY, 0.75), market, pd.Timestamp(market.timestamp)
        )
        expected = (0.5 * 100.1 + 0.25 * 100.2) / 0.75
        self.assertAlmostEqual(fill.price, expected)
        self.assertEqual(fill.liquidity, Liquidity.TAKER)

    def test_taker_rejects_zero_size(self) -> None:
        market = MarketStateBuilder().apply(book("2026-03-19 12:00:00"))
        self.assertIsNotNone(market)
        execution = ExecutionSimulator(self.config)
        with self.assertRaises(ValueError):
            execution.execute_taker(
                MarketOrder(Side.BUY, 0.0), market, pd.Timestamp(market.timestamp)
            )

    def test_quote_plan_side_must_match(self) -> None:
        market = MarketStateBuilder().apply(book("2026-03-19 12:00:00"))
        self.assertIsNotNone(market)
        execution = ExecutionSimulator(self.config)
        with self.assertRaises(ValueError):
            execution.reconcile(
                QuotePlan(Quote(Side.SELL, 99.8, 0.1), None),
                market,
                event_id=1,
                timestamp=pd.Timestamp(market.timestamp),
            )

    def test_order_is_cancelled_when_limit_shrinks(self) -> None:
        market = MarketStateBuilder().apply(book("2026-03-21 12:00:00"))
        self.assertIsNotNone(market)
        execution = ExecutionSimulator(self.config)
        plan = QuotePlan(Quote(Side.BUY, 99.8, 0.1), Quote(Side.SELL, 100.2, 0.1))
        timestamp = pd.Timestamp("2026-03-21 12:00:00")
        execution.reconcile(plan, market, event_id=1, timestamp=timestamp)
        execution.enforce_inventory_limit(0.95, 1.0)
        sides = {order.side for order in execution.active_orders()}
        self.assertNotIn(Side.BUY, sides)
        self.assertIn(Side.SELL, sides)

    def test_book_cross_fills_resting_order(self) -> None:
        builder = MarketStateBuilder()
        market = builder.apply(book("2026-03-19 12:00:00"))
        self.assertIsNotNone(market)
        execution = ExecutionSimulator(replace(self.config, book_cross_policy="fill"))
        execution.reconcile(
            QuotePlan(Quote(Side.BUY, 99.8, 0.1), None),
            market,
            event_id=1,
            timestamp=pd.Timestamp(market.timestamp),
        )
        crossed = builder.apply(shifted_book("2026-03-19 12:00:00.100", 99.6))
        fills = execution.on_book(crossed)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].price, 99.8)
        self.assertEqual(execution.active_orders(), ())

    def test_strict_book_cross_policy_cancels_unknown_fill(self) -> None:
        builder = MarketStateBuilder()
        market = builder.apply(book("2026-03-19 12:00:00"))
        self.assertIsNotNone(market)
        execution = ExecutionSimulator(self.config)
        execution.reconcile(
            QuotePlan(Quote(Side.BUY, 99.8, 0.1), None),
            market,
            event_id=1,
            timestamp=pd.Timestamp(market.timestamp),
        )
        crossed = builder.apply(shifted_book("2026-03-19 12:00:00.100", 99.6))
        self.assertEqual(execution.on_book(crossed), [])
        self.assertEqual(execution.active_orders(), ())
        self.assertEqual(execution.counts["book_cross_cancellations"], 1)

    def test_unknown_depth_does_not_start_at_front_of_queue(self) -> None:
        market = MarketStateBuilder().apply(book("2026-03-19 12:00:00"))
        self.assertIsNotNone(market)
        execution = ExecutionSimulator(self.config)
        execution.reconcile(
            QuotePlan(Quote(Side.BUY, 99.4, 0.1), None),
            market,
            event_id=1,
            timestamp=pd.Timestamp(market.timestamp),
        )
        order = execution.active_orders()[0]
        self.assertEqual(order.queue_ahead, float("inf"))
        equal_trade = Trade(
            pd.Timestamp("2026-03-19 12:00:00.100"), 99.4, 10.0, Side.SELL
        )
        self.assertEqual(execution.on_trade(equal_trade, event_id=2), [])
        through_trade = Trade(
            pd.Timestamp("2026-03-19 12:00:00.200"), 99.3, 0.001, Side.SELL
        )
        self.assertEqual(len(execution.on_trade(through_trade, event_id=3)), 1)

    def test_pressure_and_execution_use_different_depths(self) -> None:
        deep = BookSnapshot(
            timestamp=pd.Timestamp("2026-03-19 12:00:00"),
            bid_prices=tuple(100.0 - 0.1 * level for level in range(1, 21)),
            ask_prices=tuple(100.0 + 0.1 * level for level in range(1, 21)),
            bid_sizes=(0.2,) * 5 + (100.0,) * 15,
            ask_sizes=(0.1,) * 5 + (1000.0,) * 15,
        )
        market = MarketStateBuilder(pressure_levels=5).apply(deep)
        self.assertIsNotNone(market)
        self.assertAlmostEqual(market.pressure, 1 / 3)
        self.assertEqual(len(market.ask_prices), 20)
        execution = ExecutionSimulator(self.config)
        fill = execution.execute_taker(
            MarketOrder(Side.BUY, 1.0),
            market,
            pd.Timestamp(market.timestamp),
        )
        self.assertAlmostEqual(fill.price, 100.45)

    def test_malformed_book_is_rejected_cleanly(self) -> None:
        malformed = BookSnapshot(
            timestamp=pd.Timestamp("2026-03-19 12:00:00"),
            bid_prices=(99.9,),
            ask_prices=(100.1,),
            bid_sizes=(),
            ask_sizes=(1.0,),
        )
        with self.assertRaisesRegex(ValueError, "equal arrays"):
            MarketStateBuilder().apply(malformed)


class BacktestTests(unittest.TestCase):
    def test_small_end_to_end_run_finishes_flat(self) -> None:
        config = replace(
            load_config("config/market_maker.yaml"),
            decision_interval_ms=100,
            snapshot_interval_ms=100,
        )
        events = [
            book("2026-03-19 12:00:00.000"),
            Trade(pd.Timestamp("2026-03-19 12:00:00.100"), 99.8, 1.1, Side.SELL),
            book("2026-03-19 12:00:00.200"),
        ]
        result = run_backtest(config, events)
        maker = [fill for fill in result.fills if fill.liquidity is Liquidity.MAKER]
        taker = [fill for fill in result.fills if fill.liquidity is Liquidity.TAKER]
        self.assertEqual(len(maker), 1)
        self.assertEqual(len(taker), 1)
        self.assertAlmostEqual(result.metrics["final_inventory"], 0.0)
        self.assertLessEqual(
            result.metrics["max_abs_inventory"], config.inventory_limit_eth
        )
        self.assertLessEqual(
            (
                result.monitoring["inventory"].abs()
                - result.monitoring["inventory_limit"]
            ).max(),
            1e-12,
        )
        self.assertLessEqual(result.metrics["max_inventory_limit_breach"], 1e-12)
        self.assertLessEqual(result.metrics["max_order_limit_breach"], 1e-12)
        self.assertGreater(result.metrics["total_pnl"], 0.0)
        self.assertTrue(result.monitoring.index.is_unique)

    def test_stale_order_cannot_fill(self) -> None:
        config = replace(
            load_config("config/market_maker.yaml"),
            decision_interval_ms=100,
            snapshot_interval_ms=100,
            stale_book_ms=1_000,
        )
        events = [
            book("2026-03-19 12:00:00"),
            Trade(pd.Timestamp("2026-03-19 12:00:02"), 99.8, 1.1, Side.SELL),
            book("2026-03-19 12:00:02.100"),
        ]
        result = run_backtest(config, events)
        maker = [fill for fill in result.fills if fill.liquidity is Liquidity.MAKER]
        self.assertEqual(maker, [])

    def test_same_timestamp_is_processed_before_requoting(self) -> None:
        config = replace(
            load_config("config/market_maker.yaml"),
            decision_interval_ms=300,
            snapshot_interval_ms=100,
            max_pressure_shift_ticks=0.0,
            max_inventory_shift_ticks=0.0,
        )
        timestamp = pd.Timestamp("2026-03-19 12:00:00.300")
        events = [
            book("2026-03-19 12:00:00"),
            Trade(timestamp, 99.8, 1.1, Side.SELL),
            Trade(timestamp, 99.8, 1.1, Side.SELL),
            book("2026-03-19 12:00:00.400"),
        ]
        result = run_backtest(config, events)
        maker = [fill for fill in result.fills if fill.liquidity is Liquidity.MAKER]
        self.assertEqual(len(maker), 1)

    def test_backwards_time_is_rejected(self) -> None:
        config = load_config("config/market_maker.yaml")
        events = [book("2026-03-19 12:00:01"), book("2026-03-19 12:00:00")]
        with self.assertRaises(ValueError):
            run_backtest(config, events)

    def test_forced_taker_uses_current_book(self) -> None:
        config = replace(
            load_config("config/market_maker.yaml"),
            decision_interval_ms=100,
            snapshot_interval_ms=100,
        )
        engine = BacktestEngine(config, EmptyStrategy())
        engine.portfolio.apply_fill(
            Fill(
                pd.Timestamp("2026-03-21 21:59:59"),
                None,
                Side.BUY,
                100.0,
                0.1,
                Liquidity.TAKER,
                0.0,
            )
        )
        result = engine.run(
            [
                shifted_book("2026-03-21 22:00:00", 100.0),
                shifted_book("2026-03-21 23:00:00", 90.0),
            ]
        )
        taker = [fill for fill in result.fills if fill.liquidity is Liquidity.TAKER]
        self.assertTrue(taker)
        old_book_threshold = 95.0
        self.assertTrue(all(fill.price < old_book_threshold for fill in taker))


if __name__ == "__main__":
    unittest.main()
