#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
  "$ROOT/scripts/setup.command"
fi

source .venv/bin/activate
python -m pip install -r requirements-build.txt
export PYTHONPATH="$ROOT/src"
pyinstaller --clean --noconfirm --name "Anubis Auto Report" --windowed --paths "$ROOT/src" "$ROOT/app.py"

if [ -d "$ROOT/dist/Anubis Auto Report.app" ]; then
  rm -rf "$HOME/Desktop/Anubis Auto Report.app"
  cp -R "$ROOT/dist/Anubis Auto Report.app" "$HOME/Desktop/Anubis Auto Report.app"
  rm -rf "$HOME/Desktop/Anubis Auto Report NOW.app"
  cp -R "$ROOT/dist/Anubis Auto Report.app" "$HOME/Desktop/Anubis Auto Report NOW.app"
  echo "已打包並複製到桌面: $HOME/Desktop/Anubis Auto Report.app"
  echo "已同步新版 GUI 到: $HOME/Desktop/Anubis Auto Report NOW.app"
else
  echo "已打包: $ROOT/dist/Anubis Auto Report"
fi
