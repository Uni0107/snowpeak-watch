#!/bin/zsh
# 定期実行を解除する（11/28 を過ぎたら実行）
launchctl bootout "gui/$(id -u)/com.user.snowpeak-watch" 2>/dev/null
rm -f ~/Library/LaunchAgents/com.user.snowpeak-watch.plist
echo "解除しました。"
