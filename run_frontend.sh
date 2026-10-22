#!/usr/bin/env bash
# 前端啟動（launchd 登入時呼叫一次；手動也可以用）。update.md 發現 69。
# 3002 已經有人在聽（你手動開的 npm run start）就直接結束（exit 0）；還沒 build 過就先 build。
set -u
cd "$(dirname "$0")/frontend" || exit 1
log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  [frontend]  $*"; }
if lsof -ti:3002 -sTCP:LISTEN >/dev/null 2>&1; then
    log "3002 已經有前端在跑，不重複啟動"
    exit 0
fi
if [ ! -f .next/BUILD_ID ]; then
    log "還沒 build 過，先 npm run build"
    npm run build || { log "build 失敗"; exit 1; }
fi
log "啟動前端（npm run start，port 3002）"
exec npm run start
