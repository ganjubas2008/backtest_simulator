"""Run the stage 1 historical simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_maker import (
    create_strategy,
    iter_events,
    load_config,
    run_backtest,
    save_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an ETH perpetual strategy backtest"
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/market_maker.yaml")
    )
    parser.add_argument(
        "--seed", type=int, help="coin-flip seed; a random seed is used when omitted"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    strategy = create_strategy(config, args.seed)
    seed = getattr(strategy, "seed", None)
    if seed is not None:
        print(f"coin-flip seed: {seed}", flush=True)

    def show_progress(events: int, timestamp: object) -> None:
        print(f"processed {events:,} events through {timestamp}", flush=True)

    result = run_backtest(
        config, iter_events(config), strategy=strategy, progress=show_progress
    )
    save_report(result, config.output_dir)
    print(json.dumps(result.metrics, indent=2, sort_keys=True))
    print(f"results: {config.output_dir}")


if __name__ == "__main__":
    main()
