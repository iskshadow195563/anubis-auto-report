#!/bin/zsh
set -euo pipefail

LABEL="com.anubis.auto-report"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl unload "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "已移除 LaunchAgent: $PLIST"
