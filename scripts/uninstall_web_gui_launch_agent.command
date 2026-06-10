#!/bin/zsh
set -euo pipefail

LABEL="com.anubis.auto-report.webgui"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "已移除 Web GUI 常駐控制台: $PLIST"
