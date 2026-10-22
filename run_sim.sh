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
#   nohup ./run_sim.sh >> /tmp/lh_sim_watchdog.log 2>&1 &  # 背景執行（用 >>，不要用 >：第二個 watchdog 被單例鎖擋下來時，> 會先把正在跑的那個的 log 清空）
#
set -u

# ── 單例鎖：防止同時跑兩個 watchdog 互相殘殺（用 mkdir 原子鎖，macOS/Linux 皆可）──
# 兩個 watchdog 會互相 kill -9 對方的子進程、每輪各 login 一次，狂燒登入額度。
LOCK_DIR="/tmp/lh_sim_watchdog.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    _old_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
    if [ -n "$_old_pid" ] && kill -0 "$_old_pid" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog]  已有另一個 run_sim.sh 在執行（PID=${_old_pid}），本次拒絕啟動。"
        echo "若確定要重開，先停掉它：kill ${_old_pid}"
        exit 0          # 不是錯誤：已經有一個在跑就好。用 0 結束，launchd（KeepAlive 只在異常結束時重啟）才不會一直重試（update.md 發現 69）
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog]  偵測到陳舊鎖（前持有者 PID=${_old_pid:-未知} 已不存在），接管。"
fi
echo $$ > "$LOCK_DIR/pid"

PORT=8003
# 模擬盤綁 SIM_BIND_HOST → BIND_HOST（Tailscale 位址）→ 127.0.0.1（main_sim.py／core/bind_check.sim_bind_host；不再是 0.0.0.0），健康檢查要打同一個位址
_env_val() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"' "; }
HEALTH_HOST="$(_env_val SIM_BIND_HOST)"
[ -z "$HEALTH_HOST" ] && HEALTH_HOST="$(_env_val BIND_HOST)"
HEALTH_HOST="${HEALTH_HOST:-127.0.0.1}"
HEALTH_URL="http://${HEALTH_HOST}:${PORT}/api/health"
APP_LOG="/tmp/lh_sim.log"
PY=/Users/lambert/anaconda3/bin/python3.11
# shioaji 1.5.8 的獨立環境（專案裡的 .venv，沿用 anaconda 的其他套件；update.md 發現 68）。沒有 .venv 就用 anaconda 本身（舊版 1.5.3）。
# 退回舊版：mv .venv .venv.off 之後重啟 watchdog（1.5.3 已從 PyPI 下架，備份在 data/backup/sdk/shioaji-1.5.3）。
[ -x "$PWD/.venv/bin/python" ] && PY="$PWD/.venv/bin/python"
CHECK_INTERVAL=15      # 每幾秒檢查一次 health
GRACE_PERIOD=200       # (重)啟動後容許「還沒登入完成」的寬限秒數（永豐 Solace 登入可達 ~135s）
FAIL_THRESHOLD=2       # 寬限期過後，連續幾次異常才重啟（避免單次抖動誤判）

# 重啟退避（保護永豐登入 1000 次/日 額度），詳見 run_live.sh 同段註解
BACKOFF_STEPS=(0 30 120 600)
STABLE_SECS=600
ROLLOVER_MIN_UPTIME=60            # 換月重啟：後端啟動後至少這麼久才理會它的要求（登入、訂閱要時間；也防止壞掉的旗標讓行程一直被重開）
ROLLOVER_MIN_GAP=300              # 兩次換月重啟之間至少隔這麼久
consec_short=0
last_rollover=0                # 上一次換月重啟的時間（epoch 秒）：太頻繁就忽略後端的要求

CHILD_PID=""
SLEEP_PID=""           # isleep 目前在睡的那個 sleep（cleanup 要一起收掉）

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog]  $*"; }

# 可被結束訊號打斷的 sleep（update.md 發現 36）：bash 要等「前景指令」跑完才會執行 trap，
# 所以直接 sleep 600（連續短命重啟的最後一檔退避）時，pkill／kill 最長 10 分鐘後才生效——這段時間 watchdog 還活著、
# 單例鎖還在，別人重開會被擋下來（2026-10-14 實際發生）。改成背景 sleep + wait：訊號一到 wait 立刻返回、trap 立刻跑。
isleep() {
    sleep "$1" &
    SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null
    SLEEP_PID=""
}

cleanup() {
    log "收到結束訊號，關閉模擬盤 (PID=${CHILD_PID})…"
    [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null
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
        *'"broker_connected":"True"'*)
            # restart_wanted=rollover：後端發現訂閱的近月合約已結算，要重新登入才會換到下個月（update.md 發現 38）
            case "$body" in
                *'"restart_wanted":"rollover"'*) echo "rollover" ;;
                *) echo "healthy" ;;
            esac ;;
        # session=stale：後端行程活著、但券商那邊沒報價或查詢一直失敗（連線假死，update.md 發現 65）
        *'"session":"stale"'*) echo "stale" ;;
        *) echo "nobroker" ;;
    esac
}

LOG_MAX_MB=100         # app log 超過這麼大，(重)啟動前改名成 .1（只留一份舊的；update.md 發現 68）
rotate_app_log() {     # 只在子進程已經停掉、還沒啟動新的之前呼叫（沒有人在寫這個檔）
    local size
    size=$(stat -f %z "$APP_LOG" 2>/dev/null || stat -c %s "$APP_LOG" 2>/dev/null || echo 0)
    if [ "${size:-0}" -gt $((LOG_MAX_MB * 1024 * 1024)) ]; then
        mv -f "$APP_LOG" "${APP_LOG}.1"
        log "app log 超過 ${LOG_MAX_MB}MB，改名成 ${APP_LOG}.1（只留一份舊的）"
    fi
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
    if [ "$consec_short" -gt 0 ]; then
        idx=$consec_short
        max_idx=$(( ${#BACKOFF_STEPS[@]} - 1 ))
        [ "$idx" -gt "$max_idx" ] && idx=$max_idx
        wait_s=${BACKOFF_STEPS[$idx]}
        if [ "$wait_s" -gt 0 ]; then
            log "連續第 ${consec_short} 次短命重啟，退避等待 ${wait_s}s（保護登入額度）…"
            isleep "$wait_s"
        fi
    fi

    free_port
    rotate_app_log
    log "啟動 main_sim.py…"
    "$PY" main_sim.py >> "$APP_LOG" 2>&1 &
    CHILD_PID=$!
    started=$(date +%s)
    ready=0
    fails=0
    planned=0                      # 這輪是有計畫的重啟（換月）：不算短命、不退避
    log "已啟動 PID=${CHILD_PID}，登入中（寬限 ${GRACE_PERIOD}s）…"

    while true; do
        isleep "$CHECK_INTERVAL"
        if ! kill -0 "$CHILD_PID" 2>/dev/null; then
            log "進程已不存在（登入失敗或自行結束），重啟"
            break
        fi
        state=$(health_state)
        elapsed=$(( $(date +%s) - started ))

        if [ "$state" = "rollover" ]; then          # 後端要求換月重啟：近月合約已結算，要重新登入才會取得下個月的合約
            now_s=$(date +%s)
            if [ "$elapsed" -ge "$ROLLOVER_MIN_UPTIME" ] && [ $((now_s - last_rollover)) -ge "$ROLLOVER_MIN_GAP" ]; then
                log "換月：後端要求重啟（近月合約已結算）→ 正常關閉 PID=${CHILD_PID} 後重啟"
                last_rollover=$now_s
                planned=1
                kill "$CHILD_PID" 2>/dev/null        # 先請它正常關閉（worker 才會登出券商、不留孤兒連線）
                for _ in 1 2 3 4 5 6 7 8 9 10; do
                    kill -0 "$CHILD_PID" 2>/dev/null || break
                    sleep 1
                done
                kill -9 "$CHILD_PID" 2>/dev/null
                sleep 3
                break
            fi
            state="healthy"                          # 剛啟動或太頻繁：這次忽略旗標，照一般健康處理
        fi

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
            if [ "$state" = "stale" ]; then        # 連線假死：後端還活著，先請它正常關閉（worker 才會登出券商、不留孤兒連線），逾時才 -9
                log "判定券商連線假死(stale) → 正常關閉 PID=${CHILD_PID} 後重啟"
                kill "$CHILD_PID" 2>/dev/null
                for _ in 1 2 3 4 5 6 7 8 9 10; do
                    kill -0 "$CHILD_PID" 2>/dev/null || break
                    sleep 1
                done
                kill -9 "$CHILD_PID" 2>/dev/null
                sleep 3
                break
            fi
            log "判定故障(${state}) → kill -9 PID=${CHILD_PID} 並重啟（自動重連）"
            kill -9 "$CHILD_PID" 2>/dev/null
            sleep 3
            break
        fi
    done

    lifetime=$(( $(date +%s) - started ))
    if [ "$planned" -eq 1 ]; then
        consec_short=0                 # 換月是有計畫的重啟：不算短命、不退避
    elif [ "$lifetime" -lt "$STABLE_SECS" ]; then
        consec_short=$((consec_short + 1))
    else
        consec_short=0
    fi

    sleep 3
done
