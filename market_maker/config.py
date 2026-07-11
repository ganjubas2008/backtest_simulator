"""Configuration loading and validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

MAX_BOOK_LEVELS = 20


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    data_dir: Path
    dates: tuple[date, ...]
    pressure_levels: int
    execution_levels: int
    tick_size: float
    lot_size: float
    stale_book_ms: int
    funding_interval_hours: int
    strategy_name: str
    decision_interval_ms: int
    order_size_eth: float
    quote_distance_ticks: int
    max_pressure_shift_ticks: float
    max_inventory_shift_ticks: float
    funding_target_per_bp_eth: float
    max_funding_target_eth: float
    inventory_limit_eth: float
    final_inventory_eth: float
    placement_latency_ms: int
    cancellation_latency_ms: int
    maker_fee_bps: float
    taker_fee_bps: float
    queue_model: str
    book_cross_policy: str
    snapshot_interval_ms: int
    output_dir: Path

    @property
    def final_day(self) -> date:
        return self.dates[-1]

    @property
    def input_paths(self) -> tuple[Path, ...]:
        return tuple(
            self.data_dir / folder / f"{day.isoformat()}.parquet"
            for day in self.dates
            for folder in ("orderbook", "trades", "fundings")
        )


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_config(path: str | Path) -> BacktestConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"configuration must be a mapping: {path}")

    data = raw["data"]
    market = raw["market"]
    strategy = raw["strategy"]
    risk = raw["risk"]
    simulation = raw["simulation"]
    reporting = raw["reporting"]

    config = BacktestConfig(
        data_dir=Path(data["directory"]),
        dates=tuple(_as_date(value) for value in data["dates"]),
        pressure_levels=int(data["pressure_levels"]),
        execution_levels=int(data["execution_levels"]),
        tick_size=float(market["tick_size"]),
        lot_size=float(market["lot_size"]),
        stale_book_ms=int(market["stale_book_ms"]),
        funding_interval_hours=int(market["funding_interval_hours"]),
        strategy_name=str(strategy["name"]),
        decision_interval_ms=int(strategy["decision_interval_ms"]),
        order_size_eth=float(strategy["order_size_eth"]),
        quote_distance_ticks=int(strategy.get("quote_distance_ticks", 0)),
        max_pressure_shift_ticks=float(strategy.get("max_pressure_shift_ticks", 0)),
        max_inventory_shift_ticks=float(strategy.get("max_inventory_shift_ticks", 0)),
        funding_target_per_bp_eth=float(
            strategy.get("funding_target_per_bp_eth", 0)
        ),
        max_funding_target_eth=float(strategy.get("max_funding_target_eth", 0)),
        inventory_limit_eth=float(risk["inventory_limit_eth"]),
        final_inventory_eth=float(risk["final_inventory_eth"]),
        placement_latency_ms=int(simulation["placement_latency_ms"]),
        cancellation_latency_ms=int(simulation["cancellation_latency_ms"]),
        maker_fee_bps=float(simulation["maker_fee_bps"]),
        taker_fee_bps=float(simulation["taker_fee_bps"]),
        queue_model=str(simulation["queue_model"]),
        book_cross_policy=str(simulation["book_cross_policy"]),
        snapshot_interval_ms=int(reporting["snapshot_interval_ms"]),
        output_dir=Path(reporting["output_directory"]),
    )
    validate_config(config)
    return config


def _validate_values(config: BacktestConfig) -> None:
    if not config.dates or tuple(sorted(set(config.dates))) != config.dates:
        raise ValueError("dates must be non-empty, unique, and sorted")

    positive = {
        "tick_size": config.tick_size,
        "lot_size": config.lot_size,
        "order_size_eth": config.order_size_eth,
        "inventory_limit_eth": config.inventory_limit_eth,
    }
    for name, value in positive.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")

    nonnegative = {
        "max_pressure_shift_ticks": config.max_pressure_shift_ticks,
        "max_inventory_shift_ticks": config.max_inventory_shift_ticks,
        "funding_target_per_bp_eth": config.funding_target_per_bp_eth,
        "max_funding_target_eth": config.max_funding_target_eth,
    }
    for name, value in nonnegative.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")

    if not math.isfinite(config.final_inventory_eth):
        raise ValueError("final_inventory_eth must be finite")
    if not all(
        math.isfinite(fee) for fee in (config.maker_fee_bps, config.taker_fee_bps)
    ):
        raise ValueError("fees must be finite")


def _validate_constraints(config: BacktestConfig) -> None:
    if config.order_size_eth > config.inventory_limit_eth:
        raise ValueError("order size cannot exceed inventory limit")
    if config.max_funding_target_eth > config.inventory_limit_eth:
        raise ValueError("funding target cannot exceed inventory limit")
    if config.quote_distance_ticks < 0:
        raise ValueError("quote_distance_ticks must be non-negative")
    if config.decision_interval_ms <= 0 or config.snapshot_interval_ms <= 0:
        raise ValueError("decision and snapshot intervals must be positive")
    if config.stale_book_ms <= 0:
        raise ValueError("stale_book_ms must be positive")
    if config.funding_interval_hours <= 0 or 24 % config.funding_interval_hours:
        raise ValueError("funding_interval_hours must divide 24")
    if not 1 <= config.pressure_levels <= config.execution_levels <= MAX_BOOK_LEVELS:
        raise ValueError("book levels must satisfy 1 <= pressure <= execution <= 20")
    if config.placement_latency_ms != 0 or config.cancellation_latency_ms != 0:
        raise ValueError("stage 1 intentionally uses zero latency")
    if config.final_inventory_eth != 0:
        raise ValueError("stage 1 requires zero final inventory")


def _validate_alignment(config: BacktestConfig) -> None:
    for name, size in {
        "order_size_eth": config.order_size_eth,
        "inventory_limit_eth": config.inventory_limit_eth,
    }.items():
        lots = size / config.lot_size
        if not math.isclose(lots, round(lots), abs_tol=1e-9):
            raise ValueError(f"{name} must be aligned to lot_size")


def _validate_modes(config: BacktestConfig) -> None:
    if config.strategy_name not in {"market_maker", "coin_flip"}:
        raise ValueError("strategy name must be market_maker or coin_flip")
    if config.queue_model != "visible_volume_ahead":
        raise ValueError("only visible_volume_ahead is supported in stage 1")
    if config.book_cross_policy not in {"cancel", "fill"}:
        raise ValueError("book_cross_policy must be cancel or fill")


def validate_config(config: BacktestConfig) -> None:
    _validate_values(config)
    _validate_constraints(config)
    _validate_alignment(config)
    _validate_modes(config)
