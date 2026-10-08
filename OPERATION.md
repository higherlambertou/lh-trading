# lh-trading 操作手冊

日常操作與第一次上手先看 [QUICKSTART.md](QUICKSTART.md)（一頁版）。

## 快速開始

### 啟動正式盤（真錢交易）

```bash
nohup ./run_live.sh > /tmp/lh_live_watchdog.log 2>&1 &
```

檢查是否啟動成功（等 20 秒後執行）：
```bash
curl http://100.127.125.13:8002/api/health
```

應該看到：
```json
{"status": "ok", "broker_connected": "True"}
```

### 啟動模擬盤

```bash
nohup ./run_sim.sh > /tmp/lh_sim_watchdog.log 2>&1 &
```

檢查狀態：
```bash
curl http://localhost:8003/api/health
```

### 同時啟動兩個

```bash
nohup ./run_live.sh > /tmp/lh_live_watchdog.log 2>&1 &
nohup ./run_sim.sh > /tmp/lh_sim_watchdog.log 2>&1 &
```

---

## 停止服務

### 停止正式盤

```bash
pkill -f run_live.sh
```

### 停止模擬盤

```bash
pkill -f run_sim.sh
```

### 停止兩個

```bash
pkill -f 'run_(live|sim).sh'
```

---

## 狀態檢查

### 查看後端健康狀態

正式盤：
```bash
curl http://100.127.125.13:8002/api/health
```

模擬盤：
```bash
curl http://localhost:8003/api/health
```

### 查看策略列表

```bash
curl http://100.127.125.13:8002/api/strategy | python3 -m json.tool
```

### 查看部位

```bash
curl http://100.127.125.13:8002/api/position | python3 -m json.tool

# 資料多久沒更新（秒；-1 = 從未取得）。部位約每 5 秒刷新、已實現損益約每 60 秒（策略執行中 5 分鐘）
curl http://100.127.125.13:8002/api/position/meta | python3 -m json.tool
```

### 查看最新報價

```bash
curl http://100.127.125.13:8002/api/quote/last | python3 -m json.tool
```

### 市場狀態（Hurst + IV）

```bash
# 今日狀態（純讀快取）
curl http://100.127.125.13:8002/api/market/state | python3 -m json.tool

# 手動輸入今日 ATM IV（%），並重算今日狀態
curl -X POST http://100.127.125.13:8002/api/market/iv -H 'Content-Type: application/json' -d '{"iv": 18.5}'

# 立刻重算（向券商抓日K/選擇權；策略執行中預設 409，加 ?force=true 強制）
curl -X POST http://100.127.125.13:8002/api/market/refresh

# 每日日誌、驗證統計
curl 'http://100.127.125.13:8002/api/market/journal?limit=14' | python3 -m json.tool
curl 'http://100.127.125.13:8002/api/market/stats?strategy=scalp' | python3 -m json.tool

# 不連券商，只讀 db 印出文字版（含 Hurst 單獨輸出）
python -m core.daily_summary
python -m core.hurst_analyzer

# 回填歷史 IV（CSV 欄位：日期, IV%），讓百分位不用等 60 天
python -m core.iv_monitor import iv_history.csv

# Hurst 穩定度研究：比較日 K 60 根與 5 分 K 版的日間變動（fetch 另開「模擬盤」登入抓 130 天歷史，唯讀；study 只讀本機資料）
python -m core.hurst_study fetch 130
python -m core.hurst_study study
```

`.env` 的 `HURST_FREQ=5m`（`HURST_DAYS=20`）改用 5 分 K 版；預設 `D`（日 K 60 根）。改完要重啟後端，結果見 update.md「發現」6。

資料庫在 `data/market_state.db`（IV 歷史與手動備註無法重建；每個交易日 14:00 自動備份，見「備份與 tick 瘦身」）。

### 盤中即時狀態與成交紀錄

```bash
# 盤中即時：TMF 真實成交的外/內盤比例、日盤振幅、與盤前判斷是否同向（純讀記憶體）
curl http://100.127.125.13:8002/api/market/live | python3 -m json.tool

# 成交紀錄：最近的委託（含實際成交價、滑價；slip 為正 = 對我方不利，單位點）
curl 'http://100.127.125.13:8002/api/tradelog/orders?limit=20' | python3 -m json.tool

# 依 (策略, 原因) 彙總近 N 天：成交結果、滑價分布、送單延遲
curl 'http://100.127.125.13:8002/api/tradelog/summary?days=7' | python3 -m json.tool

# 不連券商，只讀 db 印出文字版彙總
python -m core.trade_log 7
```

### 開盤前試算行情的隔離（log 怎麼看）

08:30~08:45（日盤）與 14:50~15:00（夜盤）的行情是試算價，不是真實成交，三個合約的價差可達數百點。系統只讓畫面看到，不餵給策略／K 棒／即時狀態／tick 落地。

```bash
grep -E "試算時段" /tmp/lh_live.log | tail
# 進入開盤前試算時段（08:30）：試算行情只顯示在畫面，不餵給策略／K 棒／即時狀態／落地
# 開盤前試算時段結束：略過 535 筆試算行情（其中帶 simtrade 旗標 530 筆）   ← 第二個數字應該接近第一個
# 試算時段以外收到 simtrade 旗標的行情（...）：目前不會被擋     ← 若出現，代表還有我不知道的試算時段，請回報
```

- `.env`：`FILTER_PREOPEN_QUOTES=false` 恢復舊行為（試算價會污染 vwap_revert 等指標）；`TICK_STRATEGIES_TMF_ONLY=false` 讓逐 tick 策略恢復吃所有合約。改完要重啟後端。
- 部位刷新連續失敗時 log 會出現 `部位刷新連續失敗 3 次（券商端異常？最近一次：…）`，恢復時 `部位刷新恢復`；平常不會有。

### 市場指標歷史資料（外/內盤逐分鐘、每日指標）

市場指標視覺化要先歷史回放、統計驗證才能上線，需要三個指標合在一起的歷史。資料存在 `data/market_state.db`（有每日備份）：
- `flow_1m`：每分鐘 TMF 真實成交的買／賣／不明 筆數與口數、成交價 OHLC。**重啟後端後**即時記錄（`RECORD_FLOW=false` 可關）；歷史用下面的指令回補。
- `indicator_daily`：每日 Hurst／日 K 方向／IV 與 IV 百分位。

```bash
python -m core.indicator_history status                                  # 目前有多少資料
python -m core.indicator_history daily                                   # 重建每日指標（不連券商）
python -m core.indicator_history flow-from-ticks                         # 把本機 ticks.db 的逐筆聚合進 flow_1m（不連券商）
python -m core.indicator_history flow-fetch --probe --date 2026-10-08    # 向券商抓一天，並和本機 ticks.db 比對數字
python -m core.indicator_history flow-fetch --days 140                   # 補更早的日子（可重複執行，已抓過的會跳過）
python -m core.hurst_study fetch 400                                     # 延長日 K／1 分 K 歷史（Hurst 用）
```

**連線數與流量（會連到券商的兩個指令：`flow-fetch`、`hurst_study fetch`）**
- 它們會另外登入一條「模擬盤」唯讀連線；登入前自動問正式盤後端現有幾條，超過 3 條就不登入並告訴你原因。
- 券商同一身分上限 5 條。後端顯示的連線數**最多落後 2 分鐘**（每 120 秒刷新）——不要連續手動跑好幾次，也不要在它跑的時候重啟後端。
  看現在幾條：`curl http://100.127.125.13:8002/api/position/usage`（`connections`，最多落後 2 分鐘）。
- 流量：單日逐筆約 5～10 MB（每筆約 40 位元組），這個帳戶每日 2 GB；預設單次最多 320 MB、剩餘流量低於 800 MB 就停（`--max-mb`、`--reserve-mb` 可調）。
  券商的流量計數有延遲（抓完當下 +0、約 15 秒後才反映）。
- 券商逐筆的 `date` 是**交易日**（夜盤＋日盤一次給）；休市日會回前一個有資料的日子，寫入是冪等的不會重複。
- IV 歷史補不回來（過期選擇權沒有歷史報價），只能每天累積；三個指標的完整歷史要等 IV 滿 60 天。

### 市場指標回放與統計驗證

```bash
# 日 × 時段的格子＋統計驗證（純讀本機歷史資料，不連券商）
curl 'http://100.127.125.13:8002/api/market/replay?days=40&block_min=15' | python3 -m json.tool | head -60
# 只要格子、不做檢定（比較快）；可調參數：neutral_band、full_at（顏色）、flow_up／flow_down／flow_margin／flow_window（買賣力道）
curl 'http://100.127.125.13:8002/api/market/replay?validate=false&neutral_band=0.03&full_at=0.08'
```

前端在〈市場指標回放〉面板。目前的驗證結論是**沒有顯著差異**（update.md 發現 14），所以這只是回放工具，不是交易訊號。
資料來源是 `flow_1m`（外/內盤逐分鐘）與 `indicator_daily`（每日 Hurst／日 K 方向／IV）；累積更多資料後按面板的「重新計算」就會重新檢定。

### 孤兒 worker（佔住券商連線的殘留進程）

重啟時 main.py 若被強殺，shioaji 子進程（worker）可能沒被關掉，變成父進程是 1 的孤兒，**帶著券商連線一直活著、不會逾時**。
連線數基準值偏高（正常是 1 條，正式盤與模擬盤同跑才 2 條）時先查：

```bash
# 1) 列出 worker 與它們的父進程；PPID 是 1 的就是孤兒（目前正式盤的 worker，PPID 是 main.py 的 PID）
ps -axo pid,ppid,lstart,command | grep -E "multiprocessing\.(spawn|resource_tracker)" | grep -v grep
pgrep -f "python3.11 main.py"                      # 目前正式盤 main.py 的 PID，它的子進程不要動

# 2) 確認孤兒是這個專案的（工作目錄），而且真的連著券商
lsof -a -p <孤兒PID> -d cwd -Fn
lsof -nP -p <孤兒PID> | grep ESTABLISHED

# 3) 關掉孤兒 worker（一般終止訊號即可；它的 resource_tracker 會自己跟著結束，沒結束再 kill 它）
kill <孤兒worker PID>

# 4) 約 2 分鐘後（後端每 120 秒刷新一次）確認連線數回到正常
curl http://100.127.125.13:8002/api/position/usage
```

`core/shioaji_worker.py` 的 `parent_alive()` 修正之後（需重啟後端才生效），worker 會在父進程消失時自己登出並結束，不會再漏。
**修正生效前的最後一次重啟，停掉的還是舊的 worker——重啟後請再檢查一次有沒有新的孤兒。**

### 硬門檻震盪量測

```bash
python -m core.threshold_study     # 約 6 秒、只讀本機資料（日K／1分K／ticks.db），不連券商
```

看各指標（Hurst、IV 百分位、外盤占比、各策略訊號）在硬門檻附近的切換次數、來回比例，以及加遲滯帶能省多少；解讀與結論見 `THRESHOLDS.md`。IV 歷史累積到 ≥40 筆後再跑一次，IV 百分位那項才會有數字。

### 手動停損的平倉確認

手動下單設的停損/停利觸發後，系統送出平倉單**不會立刻移除監看**，而是等券商回報確認：

- 全數成交 → 移除監看（持倉裡別張單的口數不會被動到）
- 沒成交（IOC 被取消）→ 冷卻 2 秒後重送；選擇權的限價每次再放寬約 1% 權利金；重送口數 = 這個監看剩下的口數與當下部位取小
- 結果不明（送單逾時、查不到委託狀態）→ 等 10 秒、確認部位後才決定是否重送，避免重複平倉變成反向開倉
- 最多送 8 次；超過就停止自動重送，log 會出現 `不再自動重送，請立刻手動處理`，委託面板該列顯示「⚠ 平倉失敗」

log 關鍵字：`平倉單已送出`、`前一張平倉單未成交，第 N 次重送`、`平倉單已全數成交，移除監控`。
（策略自動停損仍是送出即視為出場，沒有這套確認——見 update.md「發現」3。）

成交紀錄在 `data/trade_log.db`（**無法回補**；每個交易日 14:00 自動備份）；`TRADE_LOG=false` 可整個關掉。
`outcome=unfilled` 代表 IOC 單超過 30 秒仍沒有任何成交/取消回報，值得查（停損單沒成交就是這種）。

### 破產機率驗證

```bash
# 用 scalp 目前參數試算（不給 capital = 帳戶權益）。win_rate 是「假設」的勝率，不是實測
curl 'http://100.127.125.13:8002/api/risk/ruin?win_rate=0.65&tp_pts=20&sl_pts=60&cost_pts=2' | python3 -m json.tool

# 成交紀錄 ≥30 筆後，改用真實損益分布（不足 30 筆會自動退回參數試算，並在 note 說明）
curl 'http://100.127.125.13:8002/api/risk/ruin?use_history=true' | python3 -m json.tool

# 成交 FIFO 配對成來回的明細（假設紀錄起點空倉、不含手續費與稅）
curl 'http://100.127.125.13:8002/api/risk/trips?limit=50' | python3 -m json.tool

# 不連服務直接算
python -m core.ruin --capital 51482 --tp 20 --sl 60 --win 0.65 --cost-pts 2
```

結果對**勝率與賺賠比非常敏感**；模型假設每筆獨立、損益固定，不含跳空與滑價，所以是「下限的提醒」不是預言。`cost_pts` 要自己填（手續費＋稅＋滑價折成點）。

### 備份與 tick 瘦身

**自動備份**：每個交易日 `BACKUP_TIME`（預設 14:00）把 `market_state.db`、`trade_log.db` 備份到 `data/backup/日期/`（`BACKUP_DIR` 可改位置），
保留 `BACKUP_KEEP_DAYS`（預設 14）天。用 SQLite 線上備份，服務不用停；失敗會 10 分鐘後重試。`BACKUP=false` 關閉。

```bash
python -m core.backup                       # 手動備份一次
ls data/backup/                             # 看有哪些日期
```

> 備份和原檔在同一顆硬碟，擋得住誤刪與資料庫損毀，擋不住硬碟壞掉——想更安全就把 `BACKUP_DIR` 指到另一顆硬碟或雲端同步資料夾（副本是單一獨立檔，可以直接同步）。

**還原**（先停服務；`-wal`／`-shm` 一定要一起刪，否則舊的 WAL 會被套到還原的檔案上）：

```bash
cp data/backup/2026-10-08/market_state.db data/market_state.db
rm -f data/market_state.db-wal data/market_state.db-shm
```

**ticks.db 瘦身**：重啟後新資料只存真實成交（約少 77%）並帶 `total_volume`；`RECORD_QUOTE_UPDATES=true` 可恢復全存。
舊資料要手動清（**先停服務**，VACUUM 需要獨佔鎖）：

```bash
python -m core.tick_store compact                    # 預覽：共幾列、其中幾列是報價更新（不改任何東西）
python -m core.tick_store compact --apply            # 真的清掉報價更新並 VACUUM
python -m core.tick_store compact --keep-days 30 --apply   # 另外刪掉 30 天前的成交
```

---

## 日誌查看

### watchdog 日誌（服務管理）

正式盤：
```bash
tail -f /tmp/lh_live_watchdog.log
```

模擬盤：
```bash
tail -f /tmp/lh_sim_watchdog.log
```

### 應用程式日誌（服務詳情）

正式盤：
```bash
tail -f /tmp/lh_live.log
```

模擬盤：
```bash
tail -f /tmp/lh_sim.log
```

### 搜尋錯誤

```bash
grep -i error /tmp/lh_live.log | tail -20
```

---

## 清理重啟（遇到問題時用）

### 完全清理 + 重啟正式盤

```bash
pkill -f run_live.sh
rm -rf /tmp/lh_live_watchdog.lock.d /tmp/lh_live.log
nohup ./run_live.sh > /tmp/lh_live_watchdog.log 2>&1 &
```

### 完全清理 + 重啟兩個

```bash
pkill -f 'run_(live|sim).sh'
rm -rf /tmp/lh_live_watchdog.lock.d /tmp/lh_sim_watchdog.lock.d /tmp/lh_live.log /tmp/lh_sim.log
nohup ./run_live.sh > /tmp/lh_live_watchdog.log 2>&1 &
nohup ./run_sim.sh > /tmp/lh_sim_watchdog.log 2>&1 &
```

---

## 常見狀況

### ✅ 一切正常

watchdog 日誌顯示：
```
broker 已連線（耗時 15s），進入正常監看
```

health 回傳：
```json
{"status": "ok", "broker_connected": "True"}
```

---

### ❌ 服務凍結（state=down）

**現象**：watchdog 檢查失敗，自動重啟

**日誌信息**：
```
異常 state=down（連續 2/2，elapsed=...）
判定故障(down) → kill -9 PID=... 並重啟（自動重連）
連續第 N 次短命重啟，退避等待 XXs（保護登入額度）
```

**說明**：
- 這是**正常的自動恢復機制**，服務不用手動干預
- ⚠ 但**持倉不會跟著恢復**：重啟後策略不會自動啟動、手動下單的停損停利監控也消失（update.md 發現 18）。
  當時若有部位，服務回來後第一件事是看〈部位〉，再重新啟動策略（會接管部位並套用它的停損停利）或手動平倉
- watchdog 每 15 秒檢查一次，連續 2 次失敗才重啟
- 短命重啟（活不滿 10 分鐘）會自動延長重啟間隔（30s → 120s → 600s），保護永豐的登入額度限制

**如果頻繁凍結**：
- 檢查網路連線穩定性
- 查詢永豐 Solace 是否有問題
- 考慮改到其他網路環境測試

---

### ⚠️ 無法連線券商（state=nobroker）

**現象**：health 回傳 200，但 `broker_connected` 是 `False`

**日誌信息**：
```
登入失敗或自行結束，重啟
異常 state=nobroker（連續 2/2，...）
```

**可能原因**：
1. `.env` 裡的金鑰不對
2. 永豐券商連線問題
3. CA 憑證配置有誤

**解決**：
- 檢查 `.env` 的 `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY`
- 確認 `CA_PATH` 指向正確的憑證檔案（`.pfx`）
- 檢查 `SIMULATION` 的值（`false`=正式盤、`true`=模擬盤）

---

### ❌ health 拿不到（curl 連不上）

**現象**：`curl http://100.127.125.13:8002/api/health` 沒有回應

**可能原因**：
1. 服務沒啟動
2. 正在登入中（等 60 秒）
3. 綁定的 IP 不對

**檢查**：
```bash
# 看 watchdog 有沒有在跑
ps aux | grep run_live.sh

# 看 Python 進程有沒有在跑
ps aux | grep main.py

# 看 watchdog 日誌
tail -20 /tmp/lh_live_watchdog.log
```

---

### 🔧  前端無法連線後端

**現象**：Dashboard 載不出數據，或顯示「無法連線」

**可能原因**：
1. 前端 `.env.local` 的 `NEXT_PUBLIC_API_URL` 設錯了（應該是 `http://100.127.125.13:8002/api`）
2. 後端沒啟動
3. CORS 設定問題

**檢查**：
```bash
# 確認後端在線
curl http://100.127.125.13:8002/api/health

# 確認前端設定
cat frontend/.env.local | grep NEXT_PUBLIC_API_URL
```

**修正**：
- 編輯 `frontend/.env.local`，改正 API URL
- **重啟前端**（需要重新烤進 JS bundle）：
  ```bash
  cd frontend
  npm run dev
  ```

---

## 前端開發

### 啟動前端開發 server（port 3002）

```bash
cd frontend
npm run dev
```

### 前端正式部署

```bash
cd frontend
npm run build && npm run start
```

---

## 額外工具

### 抓 Python 堆疊（凍結時除錯）

如果服務凍結了，可以發信號讓它吐出所有執行緒的堆疊（幫助診斷）：

```bash
# 找出 Python PID
ps aux | grep main.py | grep -v grep

# 發信號
kill -USR1 <PID>

# 堆疊會印到 log
tail -100 /tmp/lh_live.log
```

### 重設登入額度計數

永豐有「1000 次/日登入限制」。如果頻繁重啟超限，watchdog 會自動退避保護。每日 08:00（盤開前）自動重置。

如果手動改日期測試，可能需要重啟系統讓計數生效。

---

## 常用組合命令

### 一鍵重啟所有

```bash
pkill -f 'run_(live|sim).sh'
sleep 2
rm -rf /tmp/lh_*_watchdog.lock.d /tmp/lh_*.log
nohup ./run_live.sh > /tmp/lh_live_watchdog.log 2>&1 &
nohup ./run_sim.sh > /tmp/lh_sim_watchdog.log 2>&1 &
echo "✓ 已啟動，監看中…"
sleep 5
curl http://100.127.125.13:8002/api/health && echo "" && curl http://localhost:8003/api/health
```

### 監看雙引擎狀態

```bash
echo "=== 正式盤 ===" && curl http://100.127.125.13:8002/api/health && echo "" && \
echo "=== 模擬盤 ===" && curl http://localhost:8003/api/health && echo "" && \
echo "=== watchdog ===" && tail -3 /tmp/lh_live_watchdog.log
```

---

## 應急聯絡

如果反覆凍結無法解決：
1. 停止正式盤（改用模擬盤測試）
2. 聯繫永豐客服檢查 Solace 連線品質
3. 考慮改到辦公室或其他網路環境
