#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

shopt -s nullglob
CONFIGS=("$PROJECT_ROOT"/config/*.yaml)
shopt -u nullglob

PYTHON=""
SELECTED_CONFIG=""

die() {
  echo "Error: $*" >&2
  exit 1
}

profile_name() {
  local filename
  filename="$(basename -- "$1")"
  echo "${filename%.yaml}"
}

profile_title() {
  case "$(profile_name "$1")" in
    baseline) echo "Baseline market maker (0.10 ETH orders)" ;;
    order_size_0_01) echo "Small-order market maker (0.01 ETH orders)" ;;
    *) profile_name "$1" ;;
  esac
}

require_configs() {
  (("${#CONFIGS[@]}" > 0)) || die "No strategy profiles found in config/*.yaml"
}

yaml_path_value() {
  local config="$1"
  local key="$2"
  local value
  value="$(awk -v key="$key" '
    $1 == key ":" {
      sub(/^[^:]+:[[:space:]]*/, "")
      print
      exit
    }
  ' "$config")"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  [[ -n "$value" ]] || die "Missing $key in $config"

  if [[ "$value" = /* ]]; then
    echo "$value"
  else
    echo "$PROJECT_ROOT/$value"
  fi
}

output_directory() {
  yaml_path_value "$1" "output_directory"
}

data_directory() {
  yaml_path_value "$1" "directory"
}

find_python() {
  if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  else
    die "Python 3.12 is required. Install it, then run ./run.sh --setup."
  fi
}

bootstrap_python() {
  local python base_python
  python="$(command -v python3)"
  base_python="$(
    "$python" -c \
      'import sys; print(getattr(sys, "_base_executable", sys.executable))'
  )"

  if [[ -x "$base_python" ]]; then
    echo "$base_python"
  else
    echo "$python"
  fi
}

venv_has_pip() {
  [[ -x "$PROJECT_ROOT/.venv/bin/python" ]] &&
    "$PROJECT_ROOT/.venv/bin/python" -m pip --version >/dev/null 2>&1
}

create_with_virtualenv() {
  local python="$1"
  local bootstrap="${TMPDIR:-/tmp}/hft-virtualenv.pyz"

  echo "Using PyPA virtualenv."
  if command -v curl >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -fL --retry 3 \
      -o "$bootstrap" https://bootstrap.pypa.io/virtualenv.pyz
  elif command -v wget >/dev/null 2>&1; then
    wget --https-only -O "$bootstrap" \
      https://bootstrap.pypa.io/virtualenv.pyz
  else
    die "Install python3-venv, curl, or wget, then retry."
  fi

  "$python" "$bootstrap" "$PROJECT_ROOT/.venv" ||
    die "Could not create a Python environment with pip."
}

setup_environment() {
  local python venv_python
  command -v python3 >/dev/null 2>&1 || die "Python 3.12 is required."
  python="$(bootstrap_python)"
  venv_python="$PROJECT_ROOT/.venv/bin/python"

  if [[ ! -x "$venv_python" ]]; then
    echo "Creating the local Python environment..."
    if ! "$python" -m venv "$PROJECT_ROOT/.venv"; then
      echo
      echo "Standard venv support is unavailable; using PyPA virtualenv."
      create_with_virtualenv "$python"
    fi
  else
    echo "Using the existing local Python environment."
  fi

  [[ -x "$venv_python" ]] || die "The local Python environment was not created."
  if ! venv_has_pip; then
    echo "pip is missing from the local environment; repairing it..."
    if ! "$venv_python" -m ensurepip --upgrade || ! venv_has_pip; then
      echo
      echo "Standard pip bootstrapping is unavailable."
      create_with_virtualenv "$python"
    fi
  fi
  venv_has_pip || die "pip could not be installed in $PROJECT_ROOT/.venv."

  "$venv_python" -m pip install -r "$PROJECT_ROOT/requirements.txt"
  PYTHON="$venv_python"
  echo
  echo "Environment ready: $PROJECT_ROOT/.venv"
}

ensure_runtime() {
  find_python
  if "$PYTHON" -c 'import matplotlib, pandas, pyarrow, yaml' >/dev/null 2>&1; then
    return
  fi

  if [[ -t 0 ]]; then
    echo "The backtest dependencies are not installed."
    read -r -p "Create .venv and install them now? [Y/n] " answer
    case "${answer:-y}" in
      y|Y|yes|YES) setup_environment ;;
      *) die "Run ./run.sh --setup before running a strategy." ;;
    esac
  else
    die "Dependencies are missing. Run ./run.sh --setup first."
  fi
}

resolve_config() {
  local requested="${1%.yaml}"
  requested="${requested#config/}"
  local config

  require_configs
  for config in "${CONFIGS[@]}"; do
    if [[ "$(profile_name "$config")" == "$requested" ]]; then
      echo "$config"
      return
    fi
  done
  die "Unknown strategy profile '$1'. Run ./run.sh --list to see the choices."
}

list_profiles() {
  local config output state
  require_configs
  echo "Available strategy profiles:"
  for config in "${CONFIGS[@]}"; do
    output="$(output_directory "$config")"
    if [[ -f "$output/metrics.json" ]]; then
      state="saved results available"
    else
      state="not run yet"
    fi
    printf '  %-20s %-46s %s\n' \
      "$(profile_name "$config")" "$(profile_title "$config")" "$state"
  done
}

choose_config() {
  local index choice config
  require_configs
  echo
  echo "Choose a strategy profile:"
  index=1
  for config in "${CONFIGS[@]}"; do
    printf '  %d) %s [%s]\n' \
      "$index" "$(profile_title "$config")" "$(profile_name "$config")"
    ((index += 1))
  done
  echo "  q) Back"
  read -r -p "> " choice

  if [[ "$choice" == "q" || "$choice" == "Q" ]]; then
    return 1
  fi
  [[ "$choice" =~ ^[0-9]+$ ]] || {
    echo "Invalid selection." >&2
    return 1
  }
  ((choice >= 1 && choice <= ${#CONFIGS[@]})) || {
    echo "Invalid selection." >&2
    return 1
  }
  SELECTED_CONFIG="${CONFIGS[choice - 1]}"
}

show_results() {
  local config="$1"
  local output metrics daily title
  output="$(output_directory "$config")"
  metrics="$output/metrics.json"
  daily="$output/daily_metrics.csv"
  title="$(profile_title "$config")"
  [[ -f "$metrics" ]] ||
    die "No saved results for $(profile_name "$config"). Run the strategy first."

  find_python
  "$PYTHON" - "$title" "$metrics" "$daily" "$output" <<'PY'
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

title = sys.argv[1]
metrics_path = Path(sys.argv[2])
daily_path = Path(sys.argv[3])
output_dir = Path(sys.argv[4])
metrics = json.loads(metrics_path.read_text())
shown: set[str] = set()


def money(value: float) -> str:
    value = float(value)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def number(value: float, decimals: int = 3) -> str:
    return f"{float(value):,.{decimals}f}"


def count(value: float) -> str:
    return f"{int(round(float(value))):,}"


def percent(value: float) -> str:
    return f"{100 * float(value):,.3f}%"


def section(name: str) -> None:
    print(f"\n{name}")
    print("-" * len(name))


def item(label: str, key: str, formatter=number) -> None:
    if key in metrics:
        shown.add(key)
        print(f"  {label:<34} {formatter(metrics[key]):>16}")


print("=" * 72)
print(f"RESULTS: {title}")
print("=" * 72)

section("P&L (USD)")
item("Total P&L", "total_pnl", money)
item("Trading P&L", "trading_pnl", money)
item("Funding P&L", "funding_pnl", money)
item("Fees", "fees", money)
item("Maximum drawdown", "max_drawdown", money)

section("Inventory (ETH)")
item("Final inventory", "final_inventory")
item("Maximum absolute inventory", "max_abs_inventory")
item("Mean absolute inventory", "mean_abs_inventory")
item("Mean absolute target deviation", "mean_abs_target_deviation")

section("Execution")
item("Maker fills", "maker_fill_count", count)
item("Maker fill volume (ETH)", "maker_fill_volume")
item("Maker-filled orders", "maker_filled_order_count", count)
item("Maker-filled order ratio", "maker_filled_order_ratio", percent)
item("Taker fills", "taker_fill_count", count)
item("Taker fill volume (ETH)", "taker_fill_volume")

section("Order activity")
item("Placements", "placements", count)
item("Cancellations", "cancellations", count)
item("Book-cross cancellations", "book_cross_cancellations", count)
item("Book-cross fills", "book_cross_fills", count)

section("Risk invariant checks")
item("Maximum inventory-limit breach", "max_inventory_limit_breach")
item("Maximum active-order-limit breach", "max_order_limit_breach")

remaining = sorted(set(metrics) - shown)
if remaining:
    section("Additional metrics")
    for key in remaining:
        print(f"  {key.replace('_', ' ').title():<34} {number(metrics[key]):>16}")

if daily_path.is_file():
    with daily_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if rows:
        section("Daily P&L and inventory")
        print(
            f"  {'Day':<12} {'Daily P&L':>13} {'End equity':>13} "
            f"{'Funding':>11} {'Fees':>10} {'Max |inv|':>11} {'Mean |inv|':>12}"
        )
        for row in rows:
            print(
                f"  {row['day']:<12} {money(row['daily_pnl']):>13} "
                f"{money(row['equity']):>13} {money(row['funding_pnl']):>11} "
                f"{money(row['fees']):>10} "
                f"{number(row['max_abs_inventory']):>11} "
                f"{number(row['mean_abs_inventory']):>12}"
            )

        section("Daily execution")
        print(
            f"  {'Day':<12} {'Maker fills':>12} {'Maker ETH':>12} "
            f"{'Taker fills':>12} {'Taker ETH':>12}"
        )
        for row in rows:
            print(
                f"  {row['day']:<12} {count(row['maker_fill_count']):>12} "
                f"{number(row['maker_fill_volume']):>12} "
                f"{count(row['taker_fill_count']):>12} "
                f"{number(row['taker_fill_volume']):>12}"
            )


def file_size(path: Path) -> str:
    value = float(path.stat().st_size)
    units = ("B", "KiB", "MiB", "GiB")
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.1f} {unit}"


section("Report artifacts")
for filename in (
    "metrics.json",
    "daily_metrics.csv",
    "fills.csv",
    "monitoring.parquet",
    "summary.png",
    "manifest.json",
):
    path = output_dir / filename
    status = file_size(path) if path.is_file() else "not generated"
    print(f"  {filename:<24} {status:>14}  {path}")
print()
PY
}

run_profile() {
  local config="$1"
  local data_dir
  ensure_runtime
  data_dir="$(data_directory "$config")"
  [[ -d "$data_dir" ]] ||
    die "Market data is missing at $data_dir. See DATA.md for setup details."

  echo
  echo "Running: $(profile_title "$config")"
  echo "Config:  ${config#"$PROJECT_ROOT"/}"
  echo
  "$PYTHON" "$PROJECT_ROOT/run_backtest.py" --config "$config"
  show_results "$config"
}

run_all_profiles() {
  local config
  require_configs
  for config in "${CONFIGS[@]}"; do
    run_profile "$config"
  done
}

show_all_results() {
  local config output found=0
  require_configs
  for config in "${CONFIGS[@]}"; do
    output="$(output_directory "$config")"
    if [[ -f "$output/metrics.json" ]]; then
      show_results "$config"
      found=1
    fi
  done
  ((found == 1)) || die "No saved results are available. Run a strategy first."
}

run_tests() {
  ensure_runtime
  "$PYTHON" -m unittest discover -s "$PROJECT_ROOT/tests" -v
}

launch_jupyter() {
  if [[ ! -x "$PROJECT_ROOT/.venv/bin/jupyter" ]]; then
    if [[ -t 0 ]]; then
      echo "Jupyter is not installed in .venv."
      read -r -p "Set up the environment now? [Y/n] " answer
      case "${answer:-y}" in
        y|Y|yes|YES) setup_environment ;;
        *) die "Run ./run.sh --setup first." ;;
      esac
    else
      die "Jupyter is missing. Run ./run.sh --setup first."
    fi
  fi
  exec "$PROJECT_ROOT/run_jupyter.sh"
}

print_help() {
  cat <<'EOF'
Usage: ./run.sh [command]

With no command, an interactive menu lets you choose a strategy and action.

Commands:
  --list                    List strategy profiles
  --run NAME                Run one profile and print its complete report
  --strategy NAME           Alias for --run
  --run-all                 Run every profile
  --results NAME            Print saved results for one profile
  --results all             Print all saved results
  --setup                   Create .venv and install pinned dependencies
  --test                    Run the unit test suite
  --jupyter                 Launch the local-only analysis notebook
  -h, --help                Show this help

Examples:
  ./run.sh --run baseline
  ./run.sh --results order_size_0_01
EOF
}

interactive_menu() {
  local choice
  while true; do
    echo
    echo "ETH Perpetual Market-Making Backtest"
    echo "===================================="
    echo "  1) Run one strategy profile"
    echo "  2) Run all strategy profiles"
    echo "  3) View saved results"
    echo "  4) View all saved results"
    echo "  5) List strategy profiles"
    echo "  6) Set up the Python environment"
    echo "  7) Run tests"
    echo "  8) Launch Jupyter"
    echo "  q) Quit"
    read -r -p "> " choice || return

    case "$choice" in
      1) if choose_config; then run_profile "$SELECTED_CONFIG"; fi ;;
      2) run_all_profiles ;;
      3) if choose_config; then show_results "$SELECTED_CONFIG"; fi ;;
      4) show_all_results ;;
      5) list_profiles ;;
      6) setup_environment ;;
      7) run_tests ;;
      8) launch_jupyter ;;
      q|Q) return ;;
      *) echo "Invalid selection." >&2 ;;
    esac
  done
}

main() {
  if (($# == 0)); then
    [[ -t 0 ]] || {
      print_help
      exit 2
    }
    interactive_menu
    return
  fi

  case "$1" in
    --list)
      (($# == 1)) || die "--list does not accept arguments"
      list_profiles
      ;;
    --run|--strategy)
      (($# == 2)) || die "$1 requires a profile name"
      run_profile "$(resolve_config "$2")"
      ;;
    --run-all)
      (($# == 1)) || die "--run-all does not accept arguments"
      run_all_profiles
      ;;
    --results)
      (($# == 2)) || die "--results requires a profile name or 'all'"
      if [[ "$2" == "all" ]]; then
        show_all_results
      else
        show_results "$(resolve_config "$2")"
      fi
      ;;
    --setup)
      (($# == 1)) || die "--setup does not accept arguments"
      setup_environment
      ;;
    --test)
      (($# == 1)) || die "--test does not accept arguments"
      run_tests
      ;;
    --jupyter)
      (($# == 1)) || die "--jupyter does not accept arguments"
      launch_jupyter
      ;;
    -h|--help)
      print_help
      ;;
    *)
      die "Unknown command '$1'. Run ./run.sh --help."
      ;;
  esac
}

main "$@"
