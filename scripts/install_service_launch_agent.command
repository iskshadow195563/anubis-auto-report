#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$HOME/AnubisAutoReport"
LABEL="com.anubis.auto-report.service"
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

# Telegram Bot API getUpdates 同一時間只能有一個長輪詢程序。
# 新服務已接管 /daily 和 refresh_daily，因此安裝時停用舊 callback handler。
LEGACY_CALLBACK="$HOME/Library/LaunchAgents/com.anubisbridge.callback-handler.plist"
if [ -f "$LEGACY_CALLBACK" ]; then
  launchctl bootout "$DOMAIN" "$LEGACY_CALLBACK" >/dev/null 2>&1 || launchctl unload "$LEGACY_CALLBACK" >/dev/null 2>&1 || true
  mv "$LEGACY_CALLBACK" "$LEGACY_CALLBACK.disabled"
fi

# 每日 23:30 已整合進新常駐服務，避免重啟後同時啟動兩個報表程序。
OLD_DAILY="$HOME/Library/LaunchAgents/com.anubis.auto-report.daily-2330.plist"
if [ -f "$OLD_DAILY" ]; then
  launchctl bootout "$DOMAIN" "$OLD_DAILY" >/dev/null 2>&1 || launchctl unload "$OLD_DAILY" >/dev/null 2>&1 || true
  mv "$OLD_DAILY" "$OLD_DAILY.disabled"
fi

# 舊 openclaw gateway 內曾常駐 AnubisBridge callback handler，
# 也會使用同一個 Telegram Bot getUpdates，必須停用以免 409 Conflict。
OLD_OPENCLAW="$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist"
launchctl bootout "$DOMAIN/ai.openclaw.gateway" >/dev/null 2>&1 || true
if [ -f "$OLD_OPENCLAW" ]; then
  launchctl bootout "$DOMAIN" "$OLD_OPENCLAW" >/dev/null 2>&1 || launchctl unload "$OLD_OPENCLAW" >/dev/null 2>&1 || true
  mv "$OLD_OPENCLAW" "$OLD_OPENCLAW.disabled"
fi
pkill -TERM -f "(^|/| )openclaw( |$)|openclaw.*gateway|node.*openclaw.*gateway" >/dev/null 2>&1 || true
sleep 1
pkill -KILL -f "(^|/| )openclaw( |$)|openclaw.*gateway|node.*openclaw.*gateway" >/dev/null 2>&1 || true

OPENCLAW_DISABLED_AT="$(date +%Y%m%d%H%M%S)"
for OPENCLAW_TG_FILE in \
  "$HOME/.openclaw/credentials/telegram-pairing.json" \
  "$HOME/.openclaw/credentials/telegram-default-allowFrom.json"; do
  if [ -f "$OPENCLAW_TG_FILE" ]; then
    mv "$OPENCLAW_TG_FILE" "$OPENCLAW_TG_FILE.disabled.$OPENCLAW_DISABLED_AT"
  fi
done

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
    <string>cd "$RUNTIME" &amp;&amp; if [ ! -x ".venv/bin/python" ]; then "$RUNTIME/scripts/setup.command"; fi &amp;&amp; source .venv/bin/activate &amp;&amp; export AUTO_REPORT_HOME="$RUNTIME" PYTHONPATH="$RUNTIME/src" &amp;&amp; python "$RUNTIME/app.py" --service</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$RUNTIME</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$RUNTIME/logs/service.out.log</string>
  <key>StandardErrorPath</key>
  <string>$RUNTIME/logs/service.err.log</string>
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
echo "已安裝常駐服務: $PLIST"
echo "運行目錄: $RUNTIME"
