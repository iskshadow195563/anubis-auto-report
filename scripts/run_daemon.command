#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
  "$ROOT/scripts/setup.command"
fi

source .venv/bin/activate
export AUTO_REPORT_HOME="$ROOT"
export PYTHONPATH="$ROOT/src"
python "$ROOT/app.py" --daemon
