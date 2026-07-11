"""Run the stage 1 historical simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_maker import iter_events, load_config, run_backtest, save_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the stage 1 market-making backtest"
    )
    parser.add_argument("--config", type=Path, default=Path("config/baseline.yaml"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    def show_progress(events: int, timestamp: object) -> None:
        print(f"processed {events:,} events through {timestamp}", flush=True)

    result = run_backtest(config, iter_events(config), progress=show_progress)
    save_report(result, config.output_dir)
    print(json.dumps(result.metrics, indent=2, sort_keys=True))
    print(f"results: {config.output_dir}")


if __name__ == "__main__":
    main()
