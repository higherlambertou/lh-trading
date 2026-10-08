# lh-trading — 台指期自動交易系統

微型台指期貨（TMF）自動交易系統。後端 FastAPI（Python）+ 前端 Next.js，
透過永豐金 **shioaji** SDK 連線下單與接收報價。

> ⚠️ **回應一律用繁體中文。**
> ⚠️ **`.env` 內含「正式盤・真錢」憑證；`SIMULATION=false` 代表真實下單。
> 絕對不要 commit `.env` 或 `*.pfx`/`*.p12`/`*.pem`/`*.key`（已在 .gitignore）。**

---

## 進度紀錄

日常操作與上手步驟見 [QUICKSTART.md](QUICKSTART.md)。需求進度與更新紀錄在 [update.md](update.md)。完成任何需求（`需求文件/`）或重要更新時，同步更新它的「需求進度」表與「更新紀錄」；`需求文件/` 內的原始文件不改動。

---

## 啟動方式（用 watchdog，不要直接 `python main.py`）

shioaji 原生層在永豐 Solace session 不穩時會卡在 I/O 又不釋放 GIL，
凍結整個 Python（含 asyncio event loop），`/health` 變 000、dashboard 全當。
**這是 SDK 層問題，純 Python 改不掉**，所以用外部 watchdog 監看、偵測凍結就自動重啟。
重啟是安全的：啟動流程會自動對帳既有部位、清掉殘留委託。

**Linux / macOS（家機部署）**
```bash
# 模擬盤（port 8003，main_sim.py 強制 SIMULATION=true，綁 0.0.0.0）
nohup ./run_sim.sh  > /tmp/lh_sim_watchdog.log  2>&1 &

# 正式盤（port 8002，main.py 依 .env，綁 BIND_HOST）— 真錢，啟動前確認 .env
nohup ./run_live.sh > /tmp/lh_live_watchdog.log 2>&1 &

# 前端（port 3002）
cd frontend && npm run dev      # 開發
# 或 npm run build && npm run start   # 正式
```

**Windows（PowerShell，開發機）** — `.sh` 用了 `lsof`/`/tmp`/`trap` 不能在 Windows 跑，
改用對應的 `.ps1`（行為一致：`Get-NetTCPConnection` 取代 `lsof`、`Invoke-WebRequest` 取代 `curl`、
`Stop-Process` 取代 `kill -9`、`try/finally` 取代 `trap`）：
```powershell
# 模擬盤（port 8003，永遠不碰真錢）
powershell -ExecutionPolicy Bypass -File .\run_sim.ps1

# 正式盤（port 8002，真錢，確認 .env 的 SIMULATION=false）
powershell -ExecutionPolicy Bypass -File .\run_live.ps1

# 前端（另開一個 PowerShell 視窗）
cd frontend; npm run dev
```
- log 在 `%TEMP%\lh_sim.out.log`／`lh_sim.err.log`（正式盤為 `lh_live.*`）；
  PowerShell 的 `Start-Process` 不能把 stdout/stderr 導到同一檔，故拆兩個，且**每次重啟覆寫**。
- 停止：在該視窗按 **Ctrl+C**，`finally` 會把 python 子進程一起關掉。
- 想長期掛機正式交易，建議仍用 Linux 家機跑 `.sh`；`.ps1` 主要供 Windows 本地開發/測試。

watchdog 行為（兩版一致，wall-clock 計時）：每 15s 檢查 `/health`，判斷三態——
`healthy`(200+`broker_connected:True`)／`nobroker`(200 但沒連券商＝登入失敗或斷線)／
`down`(逾時或非 200＝凍結，連 health 都拿不到 threadpool 執行緒)。
(重)啟動後有 `GRACE_PERIOD`(200s)寬限給永豐登入（可達 ~135s）；**一旦成功連上過，
寬限即失效**，之後 `nobroker`/`down` 連續 2 次（≈30s）就 kill + 重啟＝**broker 斷線自動重連**。

**log 位置**
- watchdog：`/tmp/lh_sim_watchdog.log`、`/tmp/lh_live_watchdog.log`
- 應用程式：`/tmp/lh_sim.log`、`/tmp/lh_live.log`

**凍結時抓堆疊**（main_sim.py 已掛 faulthandler，免 root）：
```bash
kill -USR1 <pid>   # 所有 thread 的 Python 堆疊會印到 app log
```

**停止**：對 watchdog 進程下 `kill <watchdog_pid>`（會一併關掉子進程）。
若只 kill 子進程，watchdog 會自動把它拉回來。

---

## 環境變數（`.env`，參考 `.env.example`）

| 變數 | 說明 |
|---|---|
| `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` | 永豐 API 金鑰 |
| `CA_PATH` / `CA_PASSWORD` / `PERSON_ID` | CA 憑證（下單必須，純報價可略） |
| `SIMULATION` | `true`=模擬不下單；`false`=正式真實下單 |
| `DEV` | 正式啟動設 `false`（避免 reload 干擾） |
| `BIND_HOST` | 後端綁定 IP（多機部署用，如 Tailscale IP）。**`run_live.sh` 的健康檢查會讀它**，沒設則 fallback `localhost` |
| `PORT` | 正式盤後端 port（預設 8002） |
| `CORS_ORIGINS` | 允許的前端來源，逗號分隔 |

> 模擬盤固定 port 8003、綁 `0.0.0.0`（見 `main_sim.py`），不受 `BIND_HOST`/`PORT` 影響。

**換機部署檢查清單（home machine pull 後）**
1. 建 `.env`：填金鑰、`CA_PATH`、設該機自己的 `BIND_HOST` / `CORS_ORIGINS` / `PORT`。
2. 放 CA 憑證 `.pfx` 到 `CA_PATH` 指的路徑。
3. 建 `frontend/.env.local`（參考 `frontend/.env.example`）：填該機的後端位址。
4. `pip install -r requirements.txt`、`cd frontend && npm install`。

### 跨裝置存取（手機／平板連 dashboard）

要讓**其他裝置**（同一個 Tailscale tailnet）連得到，把 `localhost` 全換成本機 Tailscale IP（例 `100.97.169.26`），共 4 個變數：

| 檔案 | 變數 | 設成 |
|---|---|---|
| `.env` | `BIND_HOST` | `100.97.169.26`（後端綁此 IP，本機自己也得用此 IP 存取，localhost 會不通）|
| `.env` | `CORS_ORIGINS` | `http://localhost:3002,http://100.97.169.26:3002` |
| `frontend/.env.local` | `NEXT_PUBLIC_API_URL` | `http://100.97.169.26:8002/api` |
| `frontend/.env.local` | `NEXT_PUBLIC_SIM_URL` | `http://100.97.169.26:8003/api` |

> ⚠️ **`NEXT_PUBLIC_*` 是啟動時烤進 JS bundle 的**，改完**一定要重啟 `npm run dev`**，否則別的裝置載到的還是舊位址。
> 典型症狀：**手機看得到前端畫面、但後端沒資料** ＝ 前端沒重啟，JS 裡仍是 `localhost`，而 `localhost` 在手機上指手機自己。
> 另：前端 dev server 要對外聽，用 `npm run dev -- -H 0.0.0.0`；後端改 `BIND_HOST` 後也要重啟。

---

## 架構重點

- **`main.py`** — 正式盤入口，FastAPI app 定義處；依 `.env` 綁 `BIND_HOST:PORT`。
- **`main_sim.py`** — 模擬盤入口，強制 `SIMULATION=true`/`DEV=false`，綁 `0.0.0.0:8003`，掛 faulthandler。
- **`core/broker.py`** — shioaji 連線封裝。`call`（同步）/`acall`（丟 executor，非阻塞 event loop）為重連安全包裝。
  登入有硬性逾時（`LOGIN_TIMEOUT`，預設 25s）避免 Solace 卡死時 startup 無限懸住。
- **委託回報解析**（`core/shioaji_worker.py` 的 `extract_order_event`／`to_mapping`）：shioaji 1.5.x 的回報是 `OrderEventDict`，**不是 dict 子類**，不要用 `isinstance(msg, dict)` 判斷；
  解析壞了 scalp 會認不出自己的成交而重複進場（update.md 發現 21）。重啟後 log 前 3 筆回報會印出解析結果，萃取不到 trade_id 會警告。
- **`core/quote_hub.py`** — 報價訂閱與派發；每個 tick 用 `run_coroutine_threadsafe` 派給策略。
- **`strategies/base.py`** — 策略基底。`_go()` 進場、`_check_sl_tp()` 停損停利，
  皆有**重入防護**（先改 state 再 await，避免報價重入時重複下單 / OcType.Auto 反向疊單）。
  `start()` 會 `_sync_position_from_broker()` 與券商對帳既有部位。
  風控的「當日」＝交易日（`RISK_DAY_START`，預設 15:00 換日）：當日損益 = 累計 − 換日基準（`_day_base`），**已實現損益一律走 `_add_realized()`**
  （它會先換日再記帳；策略裡不要直接 `realized_pnl +=`，`tests/test_risk_day.py` 會檢查）。損益只在記憶體，後端重啟歸零。
- **`strategies/scalp.py`** — 限價掃單，有自己的 `_phase` 狀態機；
  覆寫 `_on_position_synced()` 在帶倉啟動時把既有部位接管進狀態機（否則會卡在 idle）。
  `max_qty` 是**持倉上限**：進場前 `_entry_gate()` 向券商查 TMF 部位與未成交委託，已有就不進場（查不到也不進；`ENTRY_POSITION_CHECK=false` 關閉）。
  狀態機的鐵則同 base：**先改 state 再 await**（`_do_enter`、入場單逾時處理都踩過：await 期間每個 tick 都會進 `on_quote`），await 之後要重新確認狀態才能改；測試在 `tests/test_scalp_state.py`。
- 同帳戶同合約**一次只能跑一個策略**（`api/routes_strategy.py` 有 409 守衛）。
  停止策略時，策略自己記有部位、或券商帳上有 TMF 部位（策略不知道的也算；查不到券商也先不停），一律回 409，`?force=true` 才強制停止；
  `STOP_POSITION_CHECK=false` 關掉券商那一層。`stop_all()`（關機）不經過這個檢查。

### 市場狀態判斷（盤前 Hurst + IV → 策略對應 → 日誌）

依《市場狀態判斷系統.md》：開倉前先判斷市場狀態。**預設只顯示＋警告，不擋下單**（`MARKET_STATE_GATE=off` 關警告）。
流程：盤前 08:30 算一次今日狀態（盤中不改，手動重算除外）→ 盤後 13:50 更新日 K／IV／當日振幅 → 每 30s 取樣策略損益進日誌。
參數見 `.env.example` 末段。

- **`core/hurst_analyzer.py`** — 第一層。純 numpy：DFA-1 + 蒙地卡羅校準（隨機漫步＝0.5）並輸出 z 值；1 分 K 合成日盤日 K。
  ⚠️ 原 `~/Downloads/hurst_analyzer.py` 的估計器有系統性偏誤（R/S 套在對數價格，隨機漫步得 ~0.97）、
  且 shioaji 1.5.x 沒有 `constant.Timeframe`（只有 1 分 K，ts 為奈秒），已重寫，**不要搬回舊版**。
  ⚠️ 60 根窗口的 H 雜訊約 ±0.13，文件的 0.45/0.55 門檻落在雜訊內（純隨機漫步也有 ~70% 窗口被標成趨勢/回歸）。
  看 z 值；想更準加大 `HURST_WINDOW`（250 根約 ±0.06），想更保守設 `HURST_MIN_Z`。
  真實歷史驗證（`python -m core.hurst_study study`，詳見 update.md 發現 6）：日 K 60 根的日間變動是純噪音的 2.4 倍、幾乎每天換標籤；
  `HURST_FREQ=5m`（5 分 K 近 `HURST_DAYS` 日，去季節性＋波動標準化＋置換檢定校準）和噪音一樣穩，但 93~100% 的日子判隨機漫步。
  **預設仍是 `D`**（換了會改變策略對應表與 `market_bias=2`）。1 分 K 存在 `bars_1m`；`hurst_study fetch` 另開**模擬盤**登入抓歷史（唯讀）。
- **`core/iv_monitor.py`** — 第二層。ATM IV（Black-76 反推）→ 歷史百分位 → LOW/NORMAL/HIGH。
  資料來源優先序：手動輸入 > Shioaji 自動抓（最近月、距到期 ≥7 天、ATM Call/Put 平價取遠期）> CSV 回填
  （`python -m core.iv_monitor import file.csv`）。百分位需 `IV_MIN_HISTORY` 天歷史，不夠時只依 Hurst。
- **`core/daily_summary.py`** — 整合、策略對應表、排程、策略日績效取樣；`market_state` 單例。
  CLI：`python -m core.daily_summary`（只讀 db，不連券商）。
- **`core/market_store.py`** — SQLite `data/market_state.db`（日 K 快取、IV 歷史、日誌、策略日績效）。
  **IV 歷史與手動備註無法重建**（有自動備份，見下）。sim/live 共用同一檔，日誌以 `(date, mode)` 區分。
- **`api/routes_market.py`** — `/api/market/{state,refresh,iv,journal,stats}`；前端 `MarketStatePanel`。
- **scalp `market_bias`** — 0 不限（預設，行為不變）／1 順勢／-1 逆勢／2 依今日狀態自動
  （趨勢→順勢、均值回歸→逆勢、不明確→不進場）。方向＝日 K 收盤 vs 20 日均線。
- **`core/live_state.py`** — 盤中即時狀態（純記憶體、僅顯示）：TMF 最近 20/100/300 筆**真實成交**的外/內盤比例、日盤振幅
  （vs 近 20 日均）、與盤前判斷是否同向。`QuoteHub._inject_quote` 餵入（包 try/except，絕不影響報價派發）；
  `GET /api/market/live`；前端〈盤中即時〉。判定真實成交：`volume>0` 且 `total_volume` 增加——實測 TMF 行情事件只有 ~27% 是成交，其餘是報價更新。
  買賣方向（`flow_direction`）帶遲滯：外盤占比要超過門檻 2.5 個百分點才轉向、回到門檻內才解除，避免在 60%/40% 上下閃爍（`FLOW_MARGIN`）。
- **`core/trade_log.py`** — 成交紀錄（`data/trade_log.db`，**無法回補，請備份**）：每筆委託（策略、原因 entry/tp/sl/trail、
  訊號價、停損停利設定價、當時的市場狀態標記）+ 券商成交回報，讀取時以 trade_id join 出實際成交價與滑價。
  掛在 `broker.place_order/place_option_order`（唯一出口）與 `_dispatch`（成交回報）；策略/手動單用 `trade_log.context(...)` 補脈絡
  （參數叫 `kind`，不要叫 `reason`——`BaseStrategy.place_order` 內有同名區域變數）。純加法：所有 record 吞例外、走獨立 writer thread、
  佇列滿就丟棄；`TRADE_LOG=false` 整個關掉。`GET /api/tradelog/{orders,summary}`、`python -m core.trade_log [天數]`。
- **手動停損確認**（`core/manual_monitor.py`）：觸發後送出平倉單**不立刻移除監看**，用委託狀態確認——全數成交才移除、沒成交才重送
  （冷卻 2s、選擇權限價逐次放寬、口數 = 監看剩餘口數與當下部位取小、結果不明先等 10s、最多 8 次）。
  動這段務必保留「先確認再重送」，否則會有重複平倉變成反向開倉的風險；測試在 `tests/test_manual_close.py`。
- **scalp `flow_source`**：0（預設）= 所有行情事件（舊算法）；1 = 只算 TMF 真實成交（`core/live_state.py` 的 `TradeDetector`）。
- **部位快取**（`api/routes_position.py`）：`_cache["positions"]`／`["pnl"]` 由 `positions_refresh_loop` 寫入（部位 5s、已實現損益 60s／策略執行中 300s，
  都走 worker；部位連續失敗會退避 5→10→20→40→60 秒）。**不要把 `list_profit_loss` 之類的帳務查詢調得更頻繁**——worker 單執行緒，查詢期間下單指令會排隊。
  `/api/position/meta` 的 `*_age_sec`（-1 = 從未取得）讓前端分辨「沒資料」與「沒持倉」。
- **破產機率驗證**（`core/ruin.py`、`api/routes_risk.py`、前端〈風險〉面板）：蒙地卡羅＋對稱公式＋Lundberg 上界＋bootstrap；不給勝率就用「無技巧基準勝率」＝停損÷(停利+停損)（20/60 → 75%）；
  `TradeLog.round_trips()` 把成交 FIFO 配對成來回，成交紀錄 ≥30 筆才允許用真實損益分布。純計算、不碰交易路徑。
  CLI：`python -m core.ruin --capital 51482 --tp 20 --sl 60 --win 0.65 --cost-pts 2`。
- **指標歷史**（市場指標視覺化的資料基礎）：`core/flow_store.py` 每分鐘彙總 TMF 真實成交（買／賣／不明 筆數與口數、成交價 OHLC）存 `flow_1m`，在報價進入點餵入（包 try/except、不影響派發），`RECORD_FLOW=false` 關閉；`core/indicator_history.py` 重建每日 Hurst／日 K 方向／IV 到 `indicator_daily`（date＝as-of，T 日盤前只能用 date<T 的列，回放別用到未來）、回補歷史（本機 ticks.db，或券商 `api.ticks()`——`date` 是**交易日**，夜盤＋日盤一次給；單日約 5～10 MB，流量計數有延遲所以上限用筆數估算）。CLI：`python -m core.indicator_history status|daily|flow-from-ticks|flow-fetch`。IV 歷史補不回來，只能每天累積。
- **市場指標回放與驗證**（`core/viz_replay.py`、`GET /api/market/replay`、前端〈市場指標回放〉）：Hurst 色相（連續漸變，中性帶內灰色）、IV 飽和度、外/內盤形狀；協調＝買賣力道方向與盤前預期方向（`want_direction`，與 scalp market_bias=2 同一套）一致。**買賣力道逐分鐘重現即時面板**（最近 100 筆＋60/40＋遲滯）、取各格結束時的狀態——不能拿整格的占比套 60/40（一格幾千筆，必貼近 50%）；預期報酬起點用訊號後第一筆成交價（避免買賣價差彈跳偏差）；每日指標只用 date<T 的最後一列。驗證＝同日內排列檢定＋CUSUM＋日級檢定，正向／負向對照與「不偷看未來」固定在 `tests/test_viz_replay.py`。**結論（37 天）：沒有顯著差異，不做即時版**（update.md 發現 14）。
- **硬門檻盤點與震盪量測**（`THRESHOLDS.md`、`core/threshold_study.py`）：只讀本機資料，量測各指標在硬門檻附近的切換／來回／遲滯可省多少；門檻與指標算式直接讀程式常數與策略自己的方法，不另抄。`python -m core.threshold_study`。
- **報價進入點的盤前試算隔離**（`core/quote_hub.py` 的 `PREOPEN_WINDOWS`）：08:30~08:45、14:50~15:00 的行情是試算價（三合約價差平均 189 點、最大 270 點），只讓畫面（WebSocket）看到，**不更新日高日低、不餵策略／K 棒／即時狀態／tick 落地**。靠**時段**隔離，不靠 `simtrade` 旗標——旗標若誤判，丟掉真實行情會讓策略變瞎子、停損失效；旗標（worker 已帶出）只用來記 log 核對。`FILTER_PREOPEN_QUOTES=false` 恢復舊行為。測試的報價時間常是 `time.time()`，所以 `tests/conftest.py` 預設關閉它，要測的測試自己打開。
- **逐 tick 策略只吃 TMF**（`strategies/base.py` 的 `quote_prefix`）：ma_cross／breakout／rsi／bollinger／momentum 設 `"TMF"`，價格序列、未實現損益、**停損停利檢查**都只看 TMF；scalp 與 K 棒策略維持 `None`（所有合約）。`TICK_STRATEGIES_TMF_ONLY=false` 恢復舊行為。新增逐 tick 策略時記得設 `quote_prefix`。
- **備份**（`core/backup.py`）：交易日 `BACKUP_TIME`（14:00）由 `daily_summary` 排程，用 SQLite 線上備份把 `market_state.db`／`trade_log.db` 存到
  `data/backup/日期/`（`BACKUP_DIR` 可改）；副本轉成單一獨立檔、驗證完整性、保留 `BACKUP_KEEP_DAYS` 天，只清日期命名的資料夾。手動：`python -m core.backup`。
- **tick 落地**（`core/tick_store.py`）：預設只存真實成交（約 23%）並帶 `total_volume`；`RECORD_QUOTE_UPDATES=true` 恢復全存。
  舊資料用 `python -m core.tick_store compact [--keep-days N] [--apply]` 瘦身（預設只預覽；VACUUM 要獨佔鎖，**先停服務**）。
- 測試：`python -m pytest tests -q`（需 numpy、fastapi、httpx；用裝了 shioaji 的 Python 環境）。

> `broker.kbars()` 會佔住 worker（單執行緒）、下單指令排隊，所以只在盤前/盤後用；
> `POST /api/market/refresh` 有策略執行中時預設回 409（`?force=true` 才強制）。

### event loop 鐵則
任何同步 shioaji 呼叫**不可**直接跑在 asyncio event loop 上——一旦 SDK 卡住會凍結整個服務。
一律用 `broker.acall(...)` 或 `loop.run_in_executor(...)` 丟到 executor。
（曾因 `main.py` keepalive 直接同步呼叫而整個凍住。）

---

## Shioaji 使用上限（會影響本專案的部分）

官方文件：<https://sinotrade.github.io/zh/tutor/limit/>。超限時行情查詢會**回空值**、
帳務/委託會被**暫停 1 分鐘**，持續違規會**封 IP 與 person_id**。以下挑出對本專案實際有風險的：

| 限制 | 數字 | 本專案的注意點 |
|---|---|---|
| **同一 person_id 連線數** | 最多 **5 條** | sim(8003)+live(8002) 同跑就佔 2 條；**重啟漏出孤兒 worker**：`run_live.sh` 的 cleanup() 只給 main.py 正常關閉 2 秒就 `kill -9`（凍結自動重啟也是 `kill -9`），強殺時 shioaji 子進程（worker）來不及登出，變成 PPID=1 的孤兒、**帶著券商連線一直活著、不會逾時**（2026-10-08 實測：最近 4 次停機漏 2 次，基準連線數因此是 3）；上限 5 條，滿了新 worker 就登不進去。`core/shioaji_worker.py` 的 `parent_alive()` 已修（worker 閒置時每秒檢查父進程，不在就先登出再結束；**需重啟後端才生效**，且這次重啟停掉的還是舊 worker，要檢查有沒有新孤兒）。檢查與清理見 OPERATION.md「孤兒 worker」。**唯讀歷史工具（`hurst_study fetch`、`indicator_history flow-fetch`）每次再佔 1 條**——它們登入前會先問後端（`core/broker_guard.py`），現有超過 3 條就不登入；後端顯示的連線數最多落後 2 分鐘（每 120 秒刷新）；自己另外開券商登入前，也先看 `/api/position/usage` 的 `connections`。 |
| **登入次數** | **1000 次/日** | 每次 watchdog 重啟都會 login。正常夠用，但若 session 一直不穩狂 flapping 重啟會燒額度。 |
| **委託操作** | **10 秒 250 次**（下單/改單/取消） | `scalp.py` 掃單頻率高；連反手平倉一次 tick 可能 2 單，掃太密要留意。 |
| **帳務查詢** | **5 秒 25 次**（list_positions / margin / list_trades 等） | 加總來源：keepalive(240s 一次)、`manual_monitor`(1s 一次)、`positions_refresh_loop`(部位 5s 一次、已實現損益 60s／300s 一次)、scalp 進場把關(每次進場前 2 次：部位＋委託；被擋下後 3 秒內不再查)、前端 PositionPanel(2s)、TradesPanel(3s)。目前總和遠低於上限，但**之後加輪詢或縮短間隔前先估一下總和**。 |
| **行情查詢** | **5 秒 50 次**（snapshots/ticks/kbars，盤中 ticks 另限 10 次/5s） | 即時報價走訂閱推播（QuoteHub）不算查詢；但若策略改用主動拉 kbars/snapshot 要算進來。 |
| **每日流量** | **500MB / 2GB / 10GB**（依近 30 日成交量分級，**開盤日 08:00 重置**） | 訂閱報價會吃流量。同時訂多合約、或多策略各自訂閱會放大用量——`QuoteHub` 已做集中訂閱去重，別繞過它各自 `quote.subscribe`。 |
| **報價訂閱數** | **200 個** | 本專案只訂 TMF/MXF/TXF，遠低於上限，無虞。 |

回覆儘量用較少的token完成