#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hft-setup-test.XXXXXX")"
trap 'rm -rf "$TEST_ROOT"' EXIT

cp "$PROJECT_ROOT/run.sh" "$TEST_ROOT/run.sh"
: >"$TEST_ROOT/requirements.txt"

python3 -m venv --without-pip "$TEST_ROOT/.venv"
if "$TEST_ROOT/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  echo "Test setup failed: the fixture unexpectedly contains pip." >&2
  exit 1
fi

# A failed/partial pip seed can leave its launcher behind even though the
# module is unavailable. The setup code must test Python's pip module, not
# merely the presence of this executable.
touch "$TEST_ROOT/.venv/bin/pip"
chmod +x "$TEST_ROOT/.venv/bin/pip"

PATH="$TEST_ROOT/.venv/bin:$PATH" "$TEST_ROOT/run.sh" --setup
"$TEST_ROOT/.venv/bin/python" -m pip --version
