#!/usr/bin/env bash
#
# 正式盤監看啟動器（watchdog）
# ─────────────────────────────────────────────────────────────
# 為什麼需要：shioaji SDK 原生層在永豐 session 不穩時，會卡在 Solace I/O
# 又不釋放 GIL，導致整個 Python（含 asyncio event loop）凍結、/health 變 000。
# 這與模擬/正式無關（同一套 SDK），正式盤一樣會凍——而且凍住時持倉的停損停利
# 完全失效，等於裸著部位放生，比模擬盤危險得多。因此正式盤更需要外部監看：
# 偵測到凍結就自動 kill -9 + 重啟。
#
# 重啟為什麼安全：啟動流程本身會自動對帳既有部位、清理殘留委託，帳不會亂。
# 唯一代價是重啟那 ~10-30 秒沒人看盤，停損會延遲到重啟完成後補上。
#
# ⚠️ 這是「正式盤・真實下單・真錢」，啟動前請確認 .env 的 SIMULATION=false。
#
# 用法：
#   ./run_live.sh                                   # 前景執行（Ctrl+C 會一起關掉）
#   nohup ./run_live.sh > /tmp/lh_live_watchdog.log 2>&1 &   # 背景執行
#
set -u

# ── 單例鎖：防止同時跑兩個 watchdog 互相殘殺 ──────────────────────
# 慘痛教訓：曾經不小心開了兩個 run_live.sh，兩個的 free_port 互相 kill -9
# 對方的子進程，每 15 秒互殺一輪、每輪各 login 一次，半天燒掉 ~1900 次登入，
# 遠超永豐 1000 次/日 上限。這裡用 mkdir 原子鎖（macOS/Linux 皆可，免裝 flock）
# 確保「同一台機器只會有一個」watchdog；並能偵測上一輪 kill -9 沒清掉的陳舊鎖。
LOCK_DIR="/tmp/lh_live_watchdog.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    _old_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
    if [ -n "$_old_pid" ] && kill -0 "$_old_pid" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog-live]  已有另一個 run_live.sh 在執行（PID=$_old_pid），本次拒絕啟動。"
        echo "若確定要重開，先停掉它：kill $_old_pid"
        exit 1
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog-live]  偵測到陳舊鎖（前持有者 PID=${_old_pid:-未知} 已不存在），接管。"
fi
echo $$ > "$LOCK_DIR/pid"

PORT=8002
# 正式盤 main.py 綁定 .env 的 BIND_HOST（多機部署用的 Tailscale IP），
# 不是 localhost，所以健康檢查必須打同一個 host，否則會誤判「啟動失敗」而狂殺健康進程。
BIND_HOST=$(grep -E '^BIND_HOST=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"' ")
HEALTH_HOST="${BIND_HOST:-localhost}"
HEALTH_URL="http://${HEALTH_HOST}:${PORT}/api/health"
APP_LOG="/tmp/lh_live.log"
CHECK_INTERVAL=15      # 每幾秒檢查一次 health
STARTUP_TIMEOUT=120    # 啟動最多等幾秒（登入不穩時會重試，故給寬一點）
FAIL_THRESHOLD=2       # 連續幾次 health 失敗才判定凍結（避免誤判）

CHILD_PID=""

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog-live]  $*"; }

cleanup() {
    log "收到結束訊號，關閉正式盤 (PID=${CHILD_PID})…"
    [ -n "$CHILD_PID" ] && kill "$CHILD_PID" 2>/dev/null
    sleep 2
    [ -n "$CHILD_PID" ] && kill -9 "$CHILD_PID" 2>/dev/null
    rm -rf "$LOCK_DIR"   # 釋放單例鎖
    exit 0
}
trap cleanup INT TERM

health_code() {
    curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$HEALTH_URL" 2>/dev/null
}

# 啟動前先清掉任何佔用 8002 的殘留進程，避免綁不上 port
free_port() {
    local pids
    # 只抓「監聽」該 port 的 server，避免誤殺瀏覽器等 client 連線
    pids=$(lsof -ti:"$PORT" -sTCP:LISTEN 2>/dev/null)
    if [ -n "$pids" ]; then
        log "清掉殘留佔用 ${PORT} 的進程: $pids"
        echo "$pids" | xargs kill -9 2>/dev/null
        sleep 2
    fi
}

log "健康檢查目標：${HEALTH_URL}"
while true; do
    free_port
    log "啟動 main.py（正式盤）…"
    python main.py >> "$APP_LOG" 2>&1 &
    CHILD_PID=$!
    log "已啟動 PID=${CHILD_PID}，等待 startup（最多 ${STARTUP_TIMEOUT}s）…"

    # ── 等待 startup 完成 ──
    up=0
    waited=0
    while [ "$waited" -lt "$STARTUP_TIMEOUT" ]; do
        if ! kill -0 "$CHILD_PID" 2>/dev/null; then
            log "進程在 startup 期間就結束了（登入連續失敗？）"
            break
        fi
        if [ "$(health_code)" = "200" ]; then up=1; break; fi
        sleep 5
        waited=$((waited + 5))
    done

    if [ "$up" -ne 1 ]; then
        log "startup 未就緒，kill 後重啟"
        kill "$CHILD_PID" 2>/dev/null; sleep 2; kill -9 "$CHILD_PID" 2>/dev/null
        sleep 5
        continue
    fi

    log "startup 完成，開始健康監看（每 ${CHECK_INTERVAL}s，連續 ${FAIL_THRESHOLD} 次失敗即重啟）"

    # ── 執行中健康監看 ──
    fails=0
    while true; do
        sleep "$CHECK_INTERVAL"
        if ! kill -0 "$CHILD_PID" 2>/dev/null; then
            log "進程已不存在（自行結束），重啟"
            break
        fi
        code=$(health_code)
        if [ "$code" = "200" ]; then
            fails=0
        else
            fails=$((fails + 1))
            log "health=${code}（連續 ${fails}/${FAIL_THRESHOLD} 次失敗）"
            if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
                log "判定凍結 → kill -9 PID=${CHILD_PID} 並重啟"
                kill -9 "$CHILD_PID" 2>/dev/null
                sleep 3
                break
            fi
        fi
    done

    sleep 3
done
