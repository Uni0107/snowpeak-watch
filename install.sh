#!/bin/zsh
# 定期実行（10分ごと）を登録する
cd "$(dirname "$0")"
cp com.user.snowpeak-watch.plist ~/Library/LaunchAgents/
launchctl bootout "gui/$(id -u)/com.user.snowpeak-watch" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.user.snowpeak-watch.plist
echo "登録しました。ログ: $PWD/check.log"
