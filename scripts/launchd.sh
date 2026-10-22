#!/usr/bin/env bash
# 登入自動啟動的安裝／移除／狀態（update.md 發現 69）。
#   scripts/launchd.sh install     # 複製到 ~/Library/LaunchAgents 並載入（已經在跑的不受影響；之後每次登入自動啟動）
#   scripts/launchd.sh uninstall   # 卸載並刪除（不會停掉正在跑的 watchdog／前端）
#   scripts/launchd.sh status
set -u
cd "$(dirname "$0")/.." || exit 1
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS="com.lh.trading.live com.lh.trading.frontend"
case "${1:-status}" in
    install)
        mkdir -p "$AGENTS"
        for l in $LABELS; do
            plutil -lint "deploy/launchd/$l.plist" >/dev/null || { echo "設定檔格式錯誤：$l"; exit 1; }
            launchctl bootout "$DOMAIN/$l" 2>/dev/null
            cp "deploy/launchd/$l.plist" "$AGENTS/$l.plist"
            launchctl bootstrap "$DOMAIN" "$AGENTS/$l.plist" && echo "已安裝：$l"
        done ;;
    uninstall)
        for l in $LABELS; do
            launchctl bootout "$DOMAIN/$l" 2>/dev/null && echo "已卸載：$l"
            rm -f "$AGENTS/$l.plist"
        done ;;
    status)
        for l in $LABELS; do
            if launchctl print "$DOMAIN/$l" >/dev/null 2>&1; then
                st=$(launchctl print "$DOMAIN/$l" | awk -F'= ' '/^\tstate =/{print $2; exit}')
                ex=$(launchctl print "$DOMAIN/$l" | awk -F'= ' '/last exit code =/{print $2; exit}')
                echo "${l}：已安裝（狀態 ${st:-?}，上次結束碼 ${ex:-—}）"
            else
                echo "${l}：沒有安裝"
            fi
        done ;;
    *) echo "用法：scripts/launchd.sh install|uninstall|status"; exit 2 ;;
esac
