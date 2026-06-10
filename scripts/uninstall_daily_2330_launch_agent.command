#!/bin/zsh
set -euo pipefail

LABEL="com.anubis.auto-report.daily-2330"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "已移除每日 23:30 排程: $PLIST"
