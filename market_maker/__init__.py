"""Simple market-making strategy and historical simulator."""

from .config import BacktestConfig, load_config
from .data import iter_events
from .report import save_report
from .simulator import run_backtest

__all__ = [
    "BacktestConfig",
    "iter_events",
    "load_config",
    "run_backtest",
    "save_report",
]
