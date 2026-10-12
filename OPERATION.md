# lh-trading 操作手冊

日常操作與第一次上手先看 [QUICKSTART.md](QUICKSTART.md)（含名詞與參數速查）。

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

### 盤中指標擴充（五檔、交易所累計內外盤、均價、基差、時段脈絡；update.md 發現 28）

重啟後端（空手時）＋前端重 build 才生效。**重啟後檢查**：

```bash
# 1. 即時面板新欄位（book／session_flow／vwap／basis／ex_range／cp／ctx）；夜盤時 basis.valid=false、ctx 只有倒數
curl -s http://100.127.125.13:8002/api/market/live | python3 -c "import sys,json; d=json.load(sys.stdin); print({k: d.get(k) for k in ('book','session_flow','vwap','basis','ex_range','cp','ctx')})"

# 2. 頁面最上方行情條的漲跌（▲／▼ 與點數）不再永遠是「—」
curl -s http://100.127.125.13:8002/api/quote/last | head -c 300     # 或直接看畫面

# 3. ticks.db 的盤口欄位有在記（重啟後的成交列 bid1 不是 NULL；舊列是 NULL）
sqlite3 data/ticks.db "select count(*), count(bid1), round(avg(ask1-bid1),2) from ticks where ts > strftime('%s','now','-1 hour')"

# 4. 交易所的日盤高低有沒有混進試算價：開盤（08:45 之後）第一次檢查這個 log，應該是空的
grep "日盤高低與交易所不一致" /tmp/lh_live.log | tail
```

- 第 4 項如果出現，代表交易所的日盤高低和我們從成交價累計的不一樣（最可能是混進了開盤前的試算價）。**重啟後自己累計不完整時改用交易所高低的那個功能，就不該信了**——回報給我，把它關掉。
- `python -m core.intraday_context show` 印今天的時段脈絡（模型係數、各時段倍數）；`screen` 重現發現 28 的 5 個檢定（只讀 `market_state.db`，不連券商）。時段脈絡每天第一次被讀到時在背景算（約 1~2 秒），那之前卡片顯示「計算中」。
- 盤口欄位 `RECORD_BOOK=false` 可關（欄位留空）。只記成交列、是成交「之後」的盤口；容量約增 50%（估每天 +3 MB），舊資料仍是 NULL。累積 2~3 個月後，用事先登記的流程檢驗「五檔失衡預測下一筆方向／價格」「價差與深度當成本指標」「交易所內外盤 vs 自己的統計」，尤其是 scalp 的限價掃單。

### 進場市場狀態紀錄（每筆進場當下的 Hurst／IV，與之後的損益）

需求文件《進場市場狀態紀錄》v1：**只多記欄位，不改任何交易行為**，為日後驗證「Hurst／IV 判斷到底有沒有用」累積資料（補不回來，所以現在先記）。沒有分析、沒有統計；沒有任何策略或風控會讀這些欄位。

- **進場快照**（`entry_snapshots`，每筆進場成交一列）：策略、策略類型（trend／mean_reversion／other，**人工定義**在 `core/entry_log.py` 的 `STRATEGY_TYPES`，新增策略要補）、方向、進場時間（Asia/Taipei +08:00）與價、
  訊號價、停損價、口數，以及 Hurst 值／z／窗口／K 棒週期／雜訊、IV 值／百分位、市場狀態、**這組狀態是什麼時候算出來的**（`state_timestamp`，看有沒有過期）、策略參數。
  市場狀態取「**委託送出當下**已有的最新值」，不重新計算；取不到的欄位是 **null**，原因寫在 `missing_reason`（例如「IV 百分位：歷史不足（3/60 天）」），不補 0。
- **出場**（`exit_fills`，每筆出場成交一列，FIFO 配到先前的進場）：出場時間、價、口數、損益（點與 NTD）、出場原因：`stop_loss`、`stop_loss_relay`（停損單沒成交或送單失敗、重送後才出場）、`take_profit`、`trail_stop`、`revert_target`、`reversal`（反手）、`manual`、`other`。
- 同一個 `trade_id`（＝進場那張委託的券商單號）串起進出場；一張單分好幾次成交仍是**一筆交易**（成交明細在 `events`）。資料在 `data/trade_log.db`（和成交紀錄同一檔，已在備份範圍）。
- **不回補**功能啟用前的交易。**啟用前就存在的部位**被平倉時，出場列的 `trade_id` 是空的、備註「沒有對應的進場快照」（進場價取自啟動時的券商持倉均價）。
- 跟著 `TRADE_LOG` 一起開關，沒有另外的開關。

```bash
curl 'http://100.127.125.13:8002/api/tradelog/entries?days=30' | python3 -m json.tool      # 一筆交易一列（進場快照＋出場彙總）
curl -o entries.csv 'http://100.127.125.13:8002/api/tradelog/entries.csv'                   # CSV（UTF-8 含 BOM，Excel 直接開；時間一律 +08:00）
curl http://100.127.125.13:8002/api/tradelog/entries/status | python3 -m json.tool          # 健康狀態
```

`entries/status` 的 **`alerts` > 0** 代表記錄過程出過問題（**不影響下單**，但要看）：`last_alert` 是最近一次的原因，log 搜尋 `ENTRYLOG ALERT`，`entry_log_errors` 表有明細。常見的：
`orphan_deal`（有成交回報但找不到委託紀錄，例如在券商 App 直接下的單）、`overfill`（同一張委託累計成交超過委託口數，多的當成重複送達略過）、`reconcile`（啟動時券商持倉和紀錄簿算出的對不上）。
`pending_deals` 是還沒處理的成交回報數，平常 0，短暫非 0 正常。同一筆成交回報的同一種問題只通報一次（重啟不會再響）。

### 日盤當沖與保證金減半（TMF 沒有）

期交所「當日沖銷交易保證金減半」只適用**臺股期貨(TX)、電子期貨(TE)、金融期貨(TF)、小型臺指(MTX)**的近二月契約，且**只限一般（日盤）交易時段**；**微型臺指(TMF)不在內，夜盤也不適用**。
2026-10-12 兩次查證，原文在期交所〈保證金一覽表-股價指數類〉（<https://www.taifex.com.tw/cht/5/indexMarging>，頁面更新日 2026/08/12）：「適用本公司臺股期貨、電子期貨、金融期貨及小型臺指期貨等4種商品之最近2個到期月份契約」「適用一般交易時段，盤後交易時段不適用」；
〈交易人常見問答-結算面〉（<https://www.taifex.com.tw/cht/9/tradersQAClearing>）同樣只列這四種。規則會改，下單前以期交所／永豐公告為準。

- **微台(TMF)不管怎麼下單，保證金都是原始保證金**（原始 35,050／維持 26,900）。shioaji 的 `FuturesOCType.DayTrade` 只是把委託標成當沖，不會讓 TMF 的保證金變少，反而多了當沖的限制——所以微台不開放當沖。
- **小台（`MXF`）、大台（`TXF`）有資格**：小台原始 175,250，減半＝87,625，規定再「取千元整數進位」＝**88,000**；大台 701,000 → 351,000。每點 50／200 元，同樣停損點數的風險是微台（每點 10 元）的 5／20 倍。
- 帳戶資格：開立期貨戶滿 3 個月、最近 1 年期貨成交 10 筆以上，另需財力證明（當沖所需保證金不超過 50 萬可免）；永豐端是否要另外申請，請問永豐。
- 當沖單要在收盤前 15 分鐘沖掉（據券商說明：非結算日 13:30、結算日 13:15，**非期交所原文**，請向永豐確認），沒沖掉會被強制沖銷；所以也不能留倉。

**手動下單怎麼用**（2026-10-12 加）：〈手動下單〉選小台或大台，會出現「當沖（日盤保證金減半）」勾選；勾了 → 這張單的倉別送 `DayTrade`（成交紀錄的 `octype` 欄位會記下）。
- **開倉和平倉都要勾**（勾選會留著；停損停利監控的平倉單會自動沿用當沖，日盤以外退回 `Auto`，免得平倉單被拒絕而失去停損）。
- 後端會擋：微台勾當沖（400）、日盤（週一～五 08:45~13:45）以外勾當沖（400）、亂寫的倉別（422）；沒勾的單（含微台、夜盤）跟以前完全一樣。收盤前要沖掉、帳戶資格由永豐管，系統不判斷。
- 策略（scalp 等）不支援當沖，一律 `Auto`（它們只交易微台）。
- ⚠️ **永豐／shioaji 文件沒寫當沖單的開倉、平倉各要怎麼標**（只寫「倉別 Auto／New／Cover／DayTrade」），期交所的作業說明是掃描圖檔也讀不到；「開平倉都標 DayTrade」是常見做法，**不是我驗證過的**。
  第一次請先用 **1 口小台**：下單後到永豐看保證金有沒有減半、平倉有沒有被沖掉；停損停利單也先用小量測。有問題用 `GET /api/order/watches` 看監控的 `octype`，並到永豐確認。

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

### 回測（參數網格＋樣本外＋沒有技巧的對照）

用**真實的策略類別**在本機的 1 分 K 上回測（只讀 `data/market_state.db`，不連券商，服務跑著也能用）。orb、vwap_revert 直接吃 K 棒；
ma_cross、breakout、rsi、bollinger、momentum 是「分鐘版」（每根 K 棒收盤當一個 tick，**參數單位變成分鐘**，不是正式環境的 tick 版）；scalp 是限價單狀態機，不能用 K 棒回測。

```bash
python -m core.backtest --strategy orb                                   # 內建網格；樣本內（前 60% 的天）／樣本外各一欄
python -m core.backtest --strategy all --null 6 --walk-forward 120,20   # 全部策略＋6 組打亂資料的對照＋滾動式驗證（約 2.5 分鐘）
python -m core.backtest --strategy orb --grid my_grid.json --cost 3 --json out.json   # 自訂網格、成本、輸出 JSON
python -m core.backtest --strategy ma_cross --source flow --session night              # 用 flow_1m（TMF，含夜盤，天數較少）
```

- `--grid` 檔案格式見 `core/backtest.py` 開頭說明；參數名稱打錯會直接報錯（不會被靜默忽略）。
- 每筆來回成本 `--cost` 預設 4 點是**假設**（手續費＋稅＋滑價），請改成自己的。盤中停損停利用 K 棒高低判斷，兩個價位同一根都碰到算停損先到；收盤前強制平倉。
- **怎麼讀**：挑 N 組裡「樣本內最好的」本來就會被高估，看樣本外；看 `--null` 的對照——打亂資料（沒有任何規律）用同樣流程也會挑出看起來不錯的組合；
  `--walk-forward` 是誠實的「先最佳化再交易」成績（每次只用過去的天數挑參數）。t 值是以天為單位，|t|>2 才算有一點證據，挑了很多組要更高。
  最後一行「偵測力」告訴你：以這份資料的天數，每天平均邊際小於多少點就看不出來。
- 2026-10-12 的結果（267 個日盤、154 組參數）：沒有任何一組有可靠的正期望值，見 update.md 發現 23。
- **前端也能跑**：儀表板〈分析〉分頁的〈策略回測〉面板（說明見 FRONTEND.md）。後端把同一個 `python -m core.backtest` 放在**獨立子行程**執行（降低優先權、同時只跑一個、預設 2 個平行行程、逾時 30 分鐘），
  所以不會拖慢下單與報價。`BACKTEST_API=false` 關掉；`BACKTEST_WORKERS`、`BACKTEST_TIMEOUT_SEC` 調整。工作狀態只在後端記憶體，重啟後端就沒了。
  API：`GET /api/backtest/info`（本機有多少資料）、`POST /api/backtest/run`、`GET /api/backtest/jobs[/{id}]`、`POST /api/backtest/jobs/{id}/cancel`。

### 策略構想的檢驗（缺口、前日高低、外資未平倉、波動度標準化）

2026-10-12 對四個策略構想做了檢驗，結果都沒找到優勢（update.md 發現 27）。工具留著，之後新資料累積了可以重跑：

```bash
python -m core.structure_study                      # 缺口＋前日高低（只讀本機 bars_1m；8 個事先固定的檢定）
python -m core.structure_study levels               # 只跑前日高低（新資料累積後驗證「突破後失敗」就用這個）
python -m core.oi_study fetch                       # 下載期交所公開資料到 data/taifex/（補缺的季／月，每次請求間隔 1 秒）
python -m core.oi_study run                         # 外資期貨未平倉 vs 隔天日盤報酬（3 個事先固定的檢定）
python -m core.backtest --strategy all --warmup 20 --walk-forward 120,20 --null 6            # 固定點數版（前 20 天只用來算參考振幅）
python -m core.backtest --strategy all --scaled --walk-forward 120,20 --null 6               # 波動度標準化版：點數型參數改成近 20 日平均日內振幅的倍數
python -m core.structure_study confirm --start 2020-03-02 --end 2025-09-02    # 前日高低「突破後失敗」的獨立驗證（單一事先固定的單尾檢定）
python -m core.oi_study fetch --only tx --start-year 2003                     # 期交所 TX 日行情（2003 起，約 280 個檔、7.8 MB）
python -m core.trend_study                                                    # 慢速順勢（TSMOM120／MA200／Donchian55）對買進持有的 α
python -m core.vrp_study fetch                                                # 選擇權歷史 ATM IV（2016 起，每月下載一次、只留每天一筆，共約 0.5 MB）
python -m core.vrp_study run                                                  # IV 與之後實際波動 RV：波動風險溢酬、低／高 IV 的條件檢驗
python -m core.hurst_study fetch 2415 2025-09-02                              # 券商 1 分 K 補到 2020-03-02（一次模擬盤唯讀登入、登入前先看連線數；2026-10-12 做過了）
```

- **判定標準在看結果之前就固定**（檢定數量、Bonferroni 門檻、怎麼讀）：換分組、換定義再找，找到的「顯著」就不可信。想測新想法時照同樣做法：先寫下假設與門檻，再跑一次。
- `data/taifex/` 是下載的公開資料（`oi/`、`tx/`、`vrp/`），只當資料讀（程式會拒絕錯誤頁）；三大法人期貨資料網站只開放最近約 3 年；TX 日行情到 2003 年；選擇權行情到 2001 年。
- **獨立驗證**：從一份資料看到的現象，要用從沒看過的資料驗證（券商期貨 1 分 K 最早到 2020-03-02，`structure_study confirm` 就是這樣做的）；單一事先固定的檢定，不是再挑一次。
- **券商歷史資料**：`hurst_study fetch` 是獨立登入（模擬盤、唯讀），一次做完；登入前會問後端目前的連線數（> 3 就不登入），歷史查詢吃流量（約 400 MB／5 年）、盤中不要反覆輪詢。
- 偵測力有限：每個報告都寫了「看得出多大的效果」；**沒過關不等於沒有效果**，只是這份資料看不出來。

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
