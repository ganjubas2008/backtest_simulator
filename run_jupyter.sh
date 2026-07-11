#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

export JUPYTER_CONFIG_DIR="$PROJECT_ROOT/.jupyter"
export JUPYTER_DATA_DIR="$PROJECT_ROOT/.jupyter-data"
export IPYTHONDIR="$PROJECT_ROOT/.ipython"
export MPLCONFIGDIR="$PROJECT_ROOT/.mplconfig"

mkdir -p "$JUPYTER_CONFIG_DIR" "$JUPYTER_DATA_DIR" "$IPYTHONDIR" "$MPLCONFIGDIR"

JUPYTER="$PROJECT_ROOT/.venv/bin/jupyter"
if [[ ! -x "$JUPYTER" ]]; then
  echo "Missing .venv. Follow the setup steps in README.md first." >&2
  exit 1
fi

exec "$JUPYTER" lab notebooks/01_hft_eda.ipynb \
  --ip=127.0.0.1 \
  --port=8888 \
  --no-browser
