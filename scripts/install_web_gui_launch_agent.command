#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$HOME/AnubisAutoReport"
LABEL="com.anubis.auto-report.webgui"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
mkdir -p "$RUNTIME"

/usr/bin/rsync -a \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  --exclude "build" \
  --exclude "dist" \
  --exclude "logs" \
  --exclude "outputs" \
  --exclude "state" \
  "$ROOT/app.py" \
  "$ROOT/requirements.txt" \
  "$ROOT/requirements-build.txt" \
  "$ROOT/README.md" \
  "$ROOT/.env" \
  "$ROOT/.env.example" \
  "$ROOT/assets" \
  "$ROOT/src" \
  "$ROOT/scripts" \
  "$RUNTIME/"

chmod +x "$RUNTIME"/scripts/*.command

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd "$RUNTIME" &amp;&amp; if [ ! -x ".venv/bin/python" ]; then "$RUNTIME/scripts/setup.command"; fi &amp;&amp; source .venv/bin/activate &amp;&amp; export AUTO_REPORT_HOME="$RUNTIME" PYTHONPATH="$RUNTIME/src" &amp;&amp; python "$RUNTIME/app.py" --gui --no-browser --gui-port 8765</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$RUNTIME</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$RUNTIME/logs/web_gui.out.log</string>
  <key>StandardErrorPath</key>
  <string>$RUNTIME/logs/web_gui.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AUTO_REPORT_HOME</key>
    <string>$RUNTIME</string>
    <key>TZ</key>
    <string>Asia/Hong_Kong</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 || launchctl load "$PLIST"
echo "已安裝 Web GUI 常駐控制台: $PLIST"
echo "本地控制台: http://127.0.0.1:8765/"
