# ETH Perpetual Market-Making Backtest

This project evaluates a deliberately small market-making strategy on ETH perpetual-futures data from 2026-03-19 through 2026-03-21. It is a research backtest, not a production trading system.

The honest result is negative: the baseline loses money even with zero fees and zero artificial latency. The smaller-order experiment loses less, but it also trades roughly one tenth of the volume. Execution uncertainty—especially orders crossed by a later book snapshot—is the largest unresolved modeling risk.

## Quick start

`run.sh` is the primary entry point. It discovers every YAML strategy profile under `config/`, lets you choose one interactively, runs the backtest, and prints the complete aggregate and daily P&L, inventory, execution, risk, and artifact report.

```bash
./run.sh --setup
./run.sh
```

The menu can also run all profiles, display previously saved results, run the tests, or launch the notebook. For automation, use a command directly:

```bash
./run.sh --list
./run.sh --run baseline
./run.sh --results order_size_0_01
./run.sh --run-all
./run.sh --test
```

The raw dataset and generated outputs are intentionally excluded from Git. Put the local Parquet data at the path described in [`DATA.md`](DATA.md) before running a backtest. Saved metrics can still be viewed at any time with `./run.sh --results PROFILE`.

## Saved results

| Configuration | PnL | Turnover | Max drawdown | Max inventory |
|---|---:|---:|---:|---:|
| `config/baseline.yaml` (0.10 ETH) | -91.41 USD | 370,460 USD | -92.56 USD | 1.215 ETH |
| `config/order_size_0_01.yaml` (0.01 ETH) | -5.22 USD | 37,377 USD | -15.02 USD | 0.280 ETH |

The baseline records 1,806 maker fills, 38 taker fills, and 25,367 cancellations caused by an active order crossing a later historical book. Its final inventory is zero and no inventory-limit invariant is breached.

## Strategy

At each 300 ms decision point, the strategy computes

```text
center = mid + pressure_shift - inventory_shift
bid    = center - (spread / 2 + quote_distance)
ask    = center + (spread / 2 + quote_distance)
```

- Pressure uses displayed size from the first 5 book levels and can move the center by at most 1 tick.
- Inventory can move the center by at most 3 ticks.
- Quotes are never placed through the current best opposite price.
- The normal inventory limit is 2 ETH and includes active orders.
- On the third day, the limit and inventory target decrease linearly to zero.
- Funding changes the preferred inventory before the final day; cash funding is accounted for separately.

## Event and fill model

Events with an identical timestamp are processed as one batch:

1. Apply scheduled funding using the last known funding rate and book.
2. Cancel stale or newly unsafe resting orders.
3. Match existing orders against trades.
4. Apply funding updates and book snapshots.
5. Handle resting orders crossed by the new book.
6. Enforce the current inventory limit, using a taker order if necessary.
7. Calculate and risk-check new quotes.
8. Reconcile desired quotes with active orders, record state, and assert invariants.

A new order cannot fill from an event at its creation timestamp. Maker fills use aggressive trade direction and displayed volume ahead at the quoted price. A trade strictly through the price fills the remaining order. Unknown depth outside the retained 20 levels is treated as an unknown queue, not an empty queue.

The strict baseline cancels crossed resting orders because a snapshot alone does not prove a fill. This is conservative about claiming fills but optimistic about adverse selection, so the crossed-order count must be considered alongside PnL.

Portfolio accounting enforces

```text
equity = cash + inventory * mid
       = realized PnL + unrealized PnL + funding PnL - fees
```

## Project layout

```text
config/             Backtest parameters
market_maker/       Data feed, strategy, risk, execution, accounting, reports
tests/              Deterministic handwritten event streams
notebooks/          Exploratory data analysis
data/               Local source archive and extracted Parquet files
outputs/            Generated reports; not source code
run.sh              Primary interactive and command-line entry point
run_backtest.py     Lower-level Python entry point
run_jupyter.sh      Local-only Jupyter launcher
.github/workflows/  Automated syntax and unit tests
```

The original task constraints are in `REQUIREMENTS.md`; data provenance and shape are in `DATA.md`.

## Manual setup and verification

Python 3.12 is the supported runtime.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Run a full backtest from the project root:

```bash
./run.sh --run baseline
./run.sh --run order_size_0_01
```

`run_backtest.py --config config/PROFILE.yaml` remains available as the lower-level Python interface.

Each run writes these generated files under its configured output directory:

- `metrics.json`: aggregate metrics;
- `daily_metrics.csv`: daily PnL, inventory, and fill metrics;
- `fills.csv`: maker and taker fills;
- `monitoring.parquet`: approximately one-second state snapshots;
- `summary.png`: price, inventory, PnL, and activity plots;
- `manifest.json`: exact config, runtime versions, and SHA-256 hashes of code and input files.

Launch the executed analysis notebook locally with:

```bash
./run.sh --jupyter
```

Jupyter binds only to `127.0.0.1:8888` and is not exposed to the network.

## Known limitations

- The baseline assumes zero fees and zero placement/cancellation latency.
- Only three days of one market are available.
- Queue position is inferred from snapshots and trade tape, not exchange order IDs.
- Snapshot-crossed orders have no provable outcome; the strict baseline cancels them.
- Pressure and fee sensitivity experiments remain to be run and saved.
- This is a single-process historical simulator with no live exchange connectivity.
