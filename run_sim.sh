#!/usr/bin/env bash
#
# 模擬盤監看啟動器（watchdog）
# ─────────────────────────────────────────────────────────────
# 為什麼需要：shioaji SDK 原生層在永豐 session 不穩時，會卡在 Solace I/O
# 又不釋放 GIL，導致整個 Python（含 asyncio event loop）凍結、/health 變 000。
# 這無法純靠 Python 程式碼根治，因此用外部監看：偵測到凍結就自動重啟。
# 啟動流程本身會自動對帳既有部位、清理殘留委託，所以重啟是安全的。
#
# 用法：
#   ./run_sim.sh                                   # 前景執行（Ctrl+C 會一起關掉）
#   nohup ./run_sim.sh > /tmp/lh_sim_watchdog.log 2>&1 &   # 背景執行
#
set -u

# ── 單例鎖：防止同時跑兩個 watchdog 互相殘殺（用 mkdir 原子鎖，macOS/Linux 皆可）──
# 兩個 watchdog 會互相 kill -9 對方的子進程、每輪各 login 一次，狂燒登入額度。
LOCK_DIR="/tmp/lh_sim_watchdog.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    _old_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
    if [ -n "$_old_pid" ] && kill -0 "$_old_pid" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog]  已有另一個 run_sim.sh 在執行（PID=$_old_pid），本次拒絕啟動。"
        echo "若確定要重開，先停掉它：kill $_old_pid"
        exit 1
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog]  偵測到陳舊鎖（前持有者 PID=${_old_pid:-未知} 已不存在），接管。"
fi
echo $$ > "$LOCK_DIR/pid"

PORT=8003
HEALTH_URL="http://localhost:${PORT}/api/health"
APP_LOG="/tmp/lh_sim.log"
CHECK_INTERVAL=15      # 每幾秒檢查一次 health
GRACE_PERIOD=200       # (重)啟動後容許「還沒登入完成」的寬限秒數（永豐 Solace 登入可達 ~135s）
FAIL_THRESHOLD=2       # 寬限期過後，連續幾次異常才重啟（避免單次抖動誤判）

CHILD_PID=""

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog]  $*"; }

cleanup() {
    log "收到結束訊號，關閉模擬盤 (PID=${CHILD_PID})…"
    [ -n "$CHILD_PID" ] && kill "$CHILD_PID" 2>/dev/null
    sleep 2
    [ -n "$CHILD_PID" ] && kill -9 "$CHILD_PID" 2>/dev/null
    rm -rf "$LOCK_DIR"   # 釋放單例鎖
    exit 0
}
trap cleanup INT TERM

# 回傳健康狀態：healthy(200+broker:True) / nobroker(200但沒連券商) / down(逾時或非200=凍結)
health_state() {
    local resp code body
    resp=$(curl -s -m 5 -w $'\n%{http_code}' "$HEALTH_URL" 2>/dev/null)
    code=${resp##*$'\n'}
    body=${resp%$'\n'*}
    if [ "$code" != "200" ]; then echo "down"; return; fi
    case "$body" in
        *'"broker_connected":"True"'*) echo "healthy" ;;
        *) echo "nobroker" ;;
    esac
}

# 啟動前先清掉任何佔用 8003 的殘留進程，避免綁不上 port
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

log "健康檢查目標：${HEALTH_URL}（每 ${CHECK_INTERVAL}s；啟動寬限 ${GRACE_PERIOD}s；凍結或 broker 斷線都會自動重啟）"
# 統一監看迴圈：wall-clock 計時、broker-aware。詳見 run_live.sh 同段註解。
while true; do
    free_port
    log "啟動 main_sim.py…"
    python main_sim.py >> "$APP_LOG" 2>&1 &
    CHILD_PID=$!
    started=$(date +%s)
    ready=0
    fails=0
    log "已啟動 PID=${CHILD_PID}，登入中（寬限 ${GRACE_PERIOD}s）…"

    while true; do
        sleep "$CHECK_INTERVAL"
        if ! kill -0 "$CHILD_PID" 2>/dev/null; then
            log "進程已不存在（登入失敗或自行結束），重啟"
            break
        fi
        state=$(health_state)
        elapsed=$(( $(date +%s) - started ))

        if [ "$state" = "healthy" ]; then
            if [ "$ready" -eq 0 ]; then
                log "broker 已連線（耗時 ${elapsed}s），進入正常監看"
                ready=1
            fi
            fails=0
            continue
        fi

        if [ "$ready" -eq 0 ] && [ "$elapsed" -lt "$GRACE_PERIOD" ]; then
            log "登入中…(${elapsed}s, state=${state})"
            continue
        fi

        fails=$((fails + 1))
        log "異常 state=${state}（連續 ${fails}/${FAIL_THRESHOLD}，elapsed=${elapsed}s）"
        if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
            log "判定故障(${state}) → kill -9 PID=${CHILD_PID} 並重啟（自動重連）"
            kill -9 "$CHILD_PID" 2>/dev/null
            sleep 3
            break
        fi
    done

    sleep 3
done
