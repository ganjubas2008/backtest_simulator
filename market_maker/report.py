"""Memory-efficient monitoring, metrics and backtest artifacts."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from array import array
from dataclasses import asdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .models import (
    BacktestResult,
    Fill,
    Liquidity,
    MarketState,
    Order,
    PortfolioSnapshot,
    Side,
)

MAX_PLOTTED_FILLS = 20_000


class MonitoringRecorder:
    FLOAT_COLUMNS = (
        "mid",
        "best_bid",
        "best_ask",
        "spread",
        "pressure",
        "inventory_limit",
        "inventory_target",
        "inventory",
        "cash",
        "average_entry_price",
        "realized_pnl",
        "unrealized_pnl",
        "funding_pnl",
        "fees",
        "equity",
        "active_bid",
        "active_ask",
        "active_bid_size",
        "active_ask_size",
    )
    COUNT_COLUMNS = (
        "placements",
        "cancellations",
        "maker_fills",
        "book_cross_fills",
        "book_cross_cancellations",
        "taker_fills",
    )

    def __init__(self) -> None:
        self.timestamps = array("q")
        self.floats = {name: array("d") for name in self.FLOAT_COLUMNS}
        self.counts = {name: array("q") for name in self.COUNT_COLUMNS}

    def append(
        self,
        timestamp: pd.Timestamp,
        market: MarketState,
        portfolio: PortfolioSnapshot,
        inventory_limit: float,
        inventory_target: float,
        active_orders: tuple[Order, ...],
        order_counts: dict[str, int],
    ) -> None:
        active = {order.side: order for order in active_orders}
        timestamp_ns = pd.Timestamp(timestamp).value
        replace_last = bool(self.timestamps) and self.timestamps[-1] == timestamp_ns
        if not replace_last:
            self.timestamps.append(timestamp_ns)
        values = {
            "mid": market.mid,
            "best_bid": market.best_bid,
            "best_ask": market.best_ask,
            "spread": market.spread,
            "pressure": market.pressure,
            "inventory_limit": inventory_limit,
            "inventory_target": inventory_target,
            "inventory": portfolio.inventory,
            "cash": portfolio.cash,
            "average_entry_price": portfolio.average_entry_price,
            "realized_pnl": portfolio.realized_pnl,
            "unrealized_pnl": portfolio.unrealized_pnl,
            "funding_pnl": portfolio.funding_pnl,
            "fees": portfolio.fees,
            "equity": portfolio.equity,
            "active_bid": active[Side.BUY].price if Side.BUY in active else np.nan,
            "active_ask": active[Side.SELL].price if Side.SELL in active else np.nan,
            "active_bid_size": active[Side.BUY].remaining_size
            if Side.BUY in active
            else 0.0,
            "active_ask_size": active[Side.SELL].remaining_size
            if Side.SELL in active
            else 0.0,
        }
        for name, value in values.items():
            if replace_last:
                self.floats[name][-1] = float(value)
            else:
                self.floats[name].append(float(value))
        for name in self.COUNT_COLUMNS:
            value = int(order_counts.get(name, 0))
            if replace_last:
                self.counts[name][-1] = value
            else:
                self.counts[name].append(value)

    def to_frame(self) -> pd.DataFrame:
        data: dict[str, object] = {
            "timestamp": pd.to_datetime(
                np.frombuffer(self.timestamps, dtype=np.int64), unit="ns"
            )
        }
        data.update(
            {
                name: np.frombuffer(values, dtype=np.float64).copy()
                for name, values in self.floats.items()
            }
        )
        data.update(
            {
                name: np.frombuffer(values, dtype=np.int64).copy()
                for name, values in self.counts.items()
            }
        )
        return pd.DataFrame(data).set_index("timestamp")


def fills_to_frame(fills: tuple[Fill, ...]) -> pd.DataFrame:
    if not fills:
        return pd.DataFrame(
            columns=[
                "timestamp",
                "order_id",
                "side",
                "price",
                "size",
                "liquidity",
                "fee",
            ]
        )
    return pd.DataFrame(
        {
            "timestamp": [fill.timestamp for fill in fills],
            "order_id": [fill.order_id for fill in fills],
            "side": [fill.side.value for fill in fills],
            "price": [fill.price for fill in fills],
            "size": [fill.size for fill in fills],
            "liquidity": [fill.liquidity.value for fill in fills],
            "fee": [fill.fee for fill in fills],
        }
    )


def compute_metrics(result: BacktestResult) -> dict[str, float]:
    monitoring = result.monitoring
    fills = fills_to_frame(result.fills)
    equity = monitoring["equity"]
    equity_from_zero = pd.concat([pd.Series([0.0]), equity.reset_index(drop=True)])
    drawdown = equity_from_zero - equity_from_zero.cummax()
    maker = fills[fills["liquidity"] == Liquidity.MAKER.value]
    taker = fills[fills["liquidity"] == Liquidity.TAKER.value]
    filled_maker_orders = int(maker["order_id"].nunique()) if len(maker) else 0
    final = monitoring.iloc[-1]
    placements = result.order_counts.get("placements", 0)
    inventory_breach = (
        monitoring["inventory"].abs() - monitoring["inventory_limit"]
    ).clip(lower=0.0)
    long_order_breach = (
        monitoring["inventory"]
        + monitoring["active_bid_size"]
        - monitoring["inventory_limit"]
    ).clip(lower=0.0)
    short_order_breach = (
        -monitoring["inventory"]
        + monitoring["active_ask_size"]
        - monitoring["inventory_limit"]
    ).clip(lower=0.0)
    return {
        "total_pnl": float(final["equity"]),
        "trading_pnl": float(final["realized_pnl"] + final["unrealized_pnl"]),
        "funding_pnl": float(final["funding_pnl"]),
        "fees": float(final["fees"]),
        "max_drawdown": float(drawdown.min()),
        "final_inventory": float(final["inventory"]),
        "max_abs_inventory": float(monitoring["inventory"].abs().max()),
        "mean_abs_inventory": float(monitoring["inventory"].abs().mean()),
        "mean_abs_target_deviation": float(
            (monitoring["inventory"] - monitoring["inventory_target"]).abs().mean()
        ),
        "max_inventory_limit_breach": float(inventory_breach.max()),
        "max_order_limit_breach": float(
            max(long_order_breach.max(), short_order_breach.max())
        ),
        "maker_fill_count": float(len(maker)),
        "maker_filled_order_count": float(filled_maker_orders),
        "maker_fill_volume": float(maker["size"].sum()) if len(maker) else 0.0,
        "taker_fill_count": float(len(taker)),
        "taker_fill_volume": float(taker["size"].sum()) if len(taker) else 0.0,
        "placements": float(placements),
        "cancellations": float(result.order_counts.get("cancellations", 0)),
        "book_cross_fills": float(result.order_counts.get("book_cross_fills", 0)),
        "book_cross_cancellations": float(
            result.order_counts.get("book_cross_cancellations", 0)
        ),
        "maker_filled_order_ratio": (
            float(filled_maker_orders / placements) if placements else 0.0
        ),
    }


def compute_daily_metrics(result: BacktestResult) -> pd.DataFrame:
    monitoring = result.monitoring.copy()
    monitoring["day"] = monitoring.index.date.astype(str)
    end = monitoring.groupby("day").last()
    end["daily_pnl"] = end["equity"].diff().fillna(end["equity"])
    inventory = monitoring.groupby("day")["inventory"].agg(
        max_abs_inventory=lambda values: values.abs().max(),
        mean_abs_inventory=lambda values: values.abs().mean(),
    )
    daily = end[["equity", "daily_pnl"]].join(inventory)
    daily["funding_pnl"] = end["funding_pnl"].diff().fillna(end["funding_pnl"])
    daily["fees"] = end["fees"].diff().fillna(end["fees"])

    fills = fills_to_frame(result.fills)
    if len(fills):
        fills["day"] = pd.to_datetime(fills["timestamp"]).dt.date.astype(str)
        fill_daily = fills.groupby(["day", "liquidity"]).agg(
            fill_count=("size", "size"), fill_volume=("size", "sum")
        )
        for liquidity in (Liquidity.MAKER.value, Liquidity.TAKER.value):
            if liquidity in fill_daily.index.get_level_values("liquidity"):
                values = fill_daily.xs(liquidity, level="liquidity")
                daily[f"{liquidity}_fill_count"] = values["fill_count"]
                daily[f"{liquidity}_fill_volume"] = values["fill_volume"]
    return daily.fillna(0.0)


def finalize_result(result: BacktestResult) -> BacktestResult:
    result.metrics = compute_metrics(result)
    result.daily_metrics = compute_daily_metrics(result)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(result: BacktestResult) -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    config = result.config
    sources = [root / "run_backtest.py", *sorted((root / "market_maker").glob("*.py"))]

    def describe(path: Path) -> dict[str, object]:
        return {"bytes": path.stat().st_size, "sha256": _sha256(path)}

    return {
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "pandas", "pyarrow", "matplotlib", "PyYAML")
        },
        "config": asdict(config),
        "inputs": {str(path): describe(path) for path in config.input_paths},
        "source": {str(path.relative_to(root)): describe(path) for path in sources},
    }


def save_report(result: BacktestResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if result.metrics is None or result.daily_metrics is None:
        raise ValueError("result must be finalized before it is saved")
    result.monitoring.to_parquet(output_dir / "monitoring.parquet")
    fills_to_frame(result.fills).to_csv(output_dir / "fills.csv", index=False)
    result.daily_metrics.to_csv(output_dir / "daily_metrics.csv")
    (output_dir / "metrics.json").write_text(
        json.dumps(result.metrics, indent=2, sort_keys=True)
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(_manifest(result), default=str, indent=2, sort_keys=True)
    )

    monitoring = result.monitoring
    fills = fills_to_frame(result.fills)
    fig, axes = plt.subplots(4, 1, figsize=(14, 14), sharex=True)
    monitoring[["mid", "active_bid", "active_ask"]].plot(ax=axes[0], linewidth=0.7)
    axes[0].set_title("Price and active quotes")
    if len(fills):
        sample = (
            fills
            if len(fills) <= MAX_PLOTTED_FILLS
            else fills.iloc[:: max(1, len(fills) // MAX_PLOTTED_FILLS)]
        )
        colors = sample["side"].map({Side.BUY.value: "green", Side.SELL.value: "red"})
        axes[0].scatter(sample["timestamp"], sample["price"], s=5, c=colors, alpha=0.5)

    monitoring[["inventory", "inventory_target", "inventory_limit"]].plot(
        ax=axes[1], linewidth=0.8
    )
    axes[1].plot(
        monitoring.index,
        -monitoring["inventory_limit"],
        linewidth=0.8,
        label="-inventory_limit",
    )
    axes[1].set_title("Inventory and limit")
    axes[1].legend()

    monitoring[["equity", "realized_pnl", "unrealized_pnl", "funding_pnl"]].plot(
        ax=axes[2], linewidth=0.8
    )
    axes[2].set_title("Profit and loss")

    monitoring[
        [
            "placements",
            "cancellations",
            "maker_fills",
            "book_cross_fills",
            "book_cross_cancellations",
            "taker_fills",
        ]
    ].plot(ax=axes[3], linewidth=0.8)
    axes[3].set_title("Cumulative order activity")
    fig.tight_layout()
    fig.savefig(output_dir / "summary.png", dpi=140)
    plt.close(fig)
