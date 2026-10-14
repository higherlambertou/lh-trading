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
# 重啟那幾十秒到幾分鐘（重新登入）沒人看盤；手動停損停利現在會落地、重啟後和券商部位對帳恢復，
# 持倉中出事會推播手機（update.md 發現 32）。策略不會自動重啟，有持倉但沒人保護時會推播警告。
#
# ⚠️ 這是「正式盤・真實下單・真錢」，啟動前請確認 .env 的 SIMULATION=false。
#
# 用法：
#   ./run_live.sh                                   # 前景執行（Ctrl+C 會一起關掉）
#   nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &  # 背景執行（用 >>，不要用 >：第二個 watchdog 被單例鎖擋下來時，> 會先把正在跑的那個的 log 清空）
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
        echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog-live]  已有另一個 run_live.sh 在執行（PID=${_old_pid}），本次拒絕啟動。"
        echo "若確定要重開，先停掉它：kill ${_old_pid}"
        exit 1
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog-live]  偵測到陳舊鎖（前持有者 PID=${_old_pid:-未知} 已不存在），接管。"
fi
echo $$ > "$LOCK_DIR/pid"

PORT=8002
PY=/Users/lambert/anaconda3/bin/python3.11
# 正式盤 main.py 綁定 .env 的 BIND_HOST（多機部署用的 Tailscale IP），
# 不是 localhost，所以健康檢查必須打同一個 host，否則會誤判「啟動失敗」而狂殺健康進程。
BIND_HOST=$(grep -E '^BIND_HOST=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"' ")
HEALTH_HOST="${BIND_HOST:-localhost}"
HEALTH_URL="http://${HEALTH_HOST}:${PORT}/api/health"
APP_LOG="/tmp/lh_live.log"
CHECK_INTERVAL=15      # 每幾秒檢查一次 health
GRACE_PERIOD=200       # (重)啟動後容許「還沒登入完成」的寬限秒數（永豐 Solace 登入可達 ~135s）
FAIL_THRESHOLD=2       # 寬限期過後，連續幾次異常才重啟（避免單次抖動誤判）

# ── 重啟退避：保護永豐「登入 1000 次/日」額度 ─────────────────────────
# 永豐/網路爛掉時進程會「啟動→秒凍→重啟」無限循環，每輪燒一次登入。
# 連續短命重啟時把間隔逐次拉長（30s→120s→600s 封頂）；
# 存活超過 STABLE_SECS 視為健康，退避歸零。
BACKOFF_STEPS=(0 30 120 600)   # 第 N 次連續短命重啟前等待秒數（最後一檔封頂）
STABLE_SECS=600                # 子進程存活超過此秒數＝穩定，重置退避
consec_short=0                 # 連續「短命」重啟計數

CHILD_PID=""
SLEEP_PID=""           # isleep 目前在睡的那個 sleep（cleanup 要一起收掉）

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  [watchdog-live]  $*"; }

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
    log "收到結束訊號，關閉正式盤 (PID=${CHILD_PID})…"
    [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null
    [ -n "$CHILD_PID" ] && kill "$CHILD_PID" 2>/dev/null
    sleep 2
    [ -n "$CHILD_PID" ] && kill -9 "$CHILD_PID" 2>/dev/null
    rm -rf "$LOCK_DIR"   # 釋放單例鎖
    exit 0
}
trap cleanup INT TERM

# 持倉中出事就推播手機（update.md 發現 32）：凍結時問不到後端，所以讀後端每 5 秒寫一次的持倉狀態檔
# （data/position_state_live.json）。沒有持倉、沒有狀態檔、或沒設定通道就什麼都不送（設定見 core/notify.py）。
# 放背景執行：推播最多等幾秒的網路逾時，不能拖慢重啟（重啟越慢，持倉沒保護的時間越長）；失敗也不影響重啟。
notify_trouble() {   # $1 = freeze | exit    $2 = down | nobroker | gone
    ( "$PY" -m core.notify watchdog --event "$1" --detail "$2" --mode live >> "$APP_LOG" 2>&1 & ) 2>/dev/null
}

# 回傳健康狀態：
#   healthy  = HTTP 200 且 broker_connected:True（真正可用）
#   nobroker = HTTP 200 但 broker 沒連上券商（登入中 or 斷線）
#   noiface  = BIND_HOST 這個位址不在本機（Tailscale 斷線）：不是後端的問題，不判定故障
#   down     = 沒回應/逾時/非 200（凍結：threadpool 被卡死的 Solace 呼叫佔光，
#              連 /health 都拿不到執行緒 → 0 bytes 逾時）
health_state() {
    local resp code body
    if ! iface_up; then echo "noiface"; return; fi            # 位址不在：打不到 health 不代表後端壞了
    resp=$(curl -s -m 5 -w $'\n%{http_code}' "$HEALTH_URL" 2>/dev/null)
    code=${resp##*$'\n'}
    body=${resp%$'\n'*}
    if [ "$code" != "200" ]; then echo "down"; return; fi
    case "$body" in
        *'"broker_connected":"True"'*) echo "healthy" ;;
        *) echo "nobroker" ;;
    esac
}

# ── BIND_HOST（Tailscale 位址）在不在本機（update.md 發現 36）──────────────
# Tailscale 斷線時，這個位址會從本機消失——這不是後端的問題。以前 health 打不到就當成「凍結」把後端殺掉，重啟又綁不上位址，
# 一路退避到 10 分鐘，Tailscale 回來後還要再等；而且每次失敗的啟動都白登入一次券商。現在：位址不在＝不判定故障、不殺後端、
# 起不來就等位址回來再啟動，並推播一次（不看有沒有持倉：dashboard 連不上你想知道）。
iface_up() {   # BIND_HOST 是本機的某個位址嗎？（沒設、localhost、0.0.0.0 一律算有）
    case "${BIND_HOST:-}" in ""|localhost|127.0.0.1|0.0.0.0) return 0 ;; esac
    { ifconfig 2>/dev/null || ip addr 2>/dev/null; } | grep -qE "inet ${BIND_HOST//./\\.}( |/)"
}

notify_noiface() {   # 背景推播一次（冷卻 30 分鐘，跨進程）；沒設定通道就只寫 log
    ( "$PY" -m core.notify send "⚠️ Tailscale 位址不見了" "正式盤後端的位址 ${BIND_HOST} 不在本機（Tailscale 斷線？），dashboard 連不上。後端若還在跑不會被當成故障殺掉；起不來的話會等位址回來再啟動。" \
        --key "watchdog:noiface:live" --cooldown 1800 >> "$APP_LOG" 2>&1 & ) 2>/dev/null
}

wait_iface() {   # 位址不在就等（每 5 秒看一次）；不啟動後端、不登入券商、不累計退避
    local n=0
    while ! iface_up; do
        n=$((n + 1))
        if [ "$n" -eq 1 ]; then
            log "BIND_HOST=${BIND_HOST} 不在本機（Tailscale 沒連上？）：先不啟動後端，等位址回來"
            notify_noiface
        fi
        [ $((n % 12)) -eq 0 ] && log "還在等 ${BIND_HOST}…（已等約 $((n * 5)) 秒）"
        isleep 5
    done
    [ "$n" -gt 0 ] && log "位址回來了（等了約 $((n * 5)) 秒），繼續啟動"
    return 0
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

log "健康檢查目標：${HEALTH_URL}（每 ${CHECK_INTERVAL}s；啟動寬限 ${GRACE_PERIOD}s；凍結或 broker 斷線都會自動重啟）"
# ── 統一監看迴圈（取代舊的「先等 startup 再監看」兩段式）──────────────
# 用 wall-clock 計時，避免舊版 bug：health 逾時(5s)沒被計入 waited，凍結時
# 實際耗時是計數的兩倍，240s 變實際 ~480s 才重啟。
# 判定「可用」= healthy（HTTP200 + broker:True）。三種異常都會重啟：
#   - down（凍結，連 /health 都逾時）
#   - nobroker（HTTP200 但沒連上券商＝登入失敗或中途斷線）
# 寬限期：(重)啟動後 GRACE_PERIOD 秒內、且還沒成功連上過，容許 down/nobroker
#         （因為永豐登入本來就慢）。一旦成功連上過(ready=1)，寬限即失效，
#         之後任何 down/nobroker 連續 FAIL_THRESHOLD 次就重啟 → 這就是「broker 斷線自動重連」。
while true; do
    # ── 退避等待（連續短命重啟時逐次拉長間隔，省登入額度）──
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

    wait_iface
    free_port
    log "啟動 main.py（正式盤）…"
    "$PY" main.py >> "$APP_LOG" 2>&1 &
    CHILD_PID=$!
    started=$(date +%s)
    ready=0
    fails=0
    noiface_n=0
    log "已啟動 PID=${CHILD_PID}，登入中（寬限 ${GRACE_PERIOD}s）…"

    while true; do
        isleep "$CHECK_INTERVAL"
        if ! kill -0 "$CHILD_PID" 2>/dev/null; then
            log "進程已不存在（登入失敗或自行結束），重啟"
            notify_trouble exit gone
            break
        fi
        state=$(health_state)
        elapsed=$(( $(date +%s) - started ))

        if [ "$state" = "noiface" ]; then          # Tailscale 位址不在：不計入失敗、不殺後端，等它回來
            noiface_n=$((noiface_n + 1))
            if [ "$noiface_n" -eq 1 ]; then
                log "BIND_HOST=${BIND_HOST} 不在本機（Tailscale 斷線？）：後端還在跑，不當成故障，等位址回來"
                notify_noiface
            fi
            continue
        fi
        if [ "$noiface_n" -gt 0 ]; then
            log "位址回來了（約 $((noiface_n * CHECK_INTERVAL)) 秒）"
            noiface_n=0
            fails=0
        fi

        if [ "$state" = "healthy" ]; then
            if [ "$ready" -eq 0 ]; then
                log "broker 已連線（耗時 ${elapsed}s），進入正常監看"
                ready=1
            fi
            fails=0
            continue
        fi

        # 異常（down=凍結 / nobroker=沒連上券商）
        if [ "$ready" -eq 0 ] && [ "$elapsed" -lt "$GRACE_PERIOD" ]; then
            log "登入中…(${elapsed}s, state=${state})"
            continue
        fi

        fails=$((fails + 1))
        log "異常 state=${state}（連續 ${fails}/${FAIL_THRESHOLD}，elapsed=${elapsed}s）"
        if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
            log "判定故障(${state}) → kill -9 PID=${CHILD_PID} 並重啟（自動重連）"
            notify_trouble freeze "$state"
            kill -9 "$CHILD_PID" 2>/dev/null
            sleep 3
            break
        fi
    done

    # ── 更新退避計數：短命（活不到 STABLE_SECS）就累加，否則歸零 ──
    lifetime=$(( $(date +%s) - started ))
    if [ "$lifetime" -lt "$STABLE_SECS" ]; then
        consec_short=$((consec_short + 1))
    else
        consec_short=0
    fi

    sleep 3
done
