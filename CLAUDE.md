# lh-trading — 台指期自動交易系統

微型台指期貨（TMF）自動交易系統。後端 FastAPI（Python）+ 前端 Next.js，
透過永豐金 **shioaji** SDK 連線下單與接收報價。

> ⚠️ **回應一律用繁體中文。**
> ⚠️ **`.env` 內含「正式盤・真錢」憑證；`SIMULATION=false` 代表真實下單。
> 絕對不要 commit `.env` 或 `*.pfx`/`*.p12`/`*.pem`/`*.key`（已在 .gitignore）。**

---

## 進度紀錄

需求進度與更新紀錄在 [update.md](update.md)。完成任何需求（`需求文件/`）或重要更新時，同步更新它的「需求進度」表與「更新紀錄」；`需求文件/` 內的原始文件不改動。

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
- **`core/quote_hub.py`** — 報價訂閱與派發；每個 tick 用 `run_coroutine_threadsafe` 派給策略。
- **`strategies/base.py`** — 策略基底。`_go()` 進場、`_check_sl_tp()` 停損停利，
  皆有**重入防護**（先改 state 再 await，避免報價重入時重複下單 / OcType.Auto 反向疊單）。
  `start()` 會 `_sync_position_from_broker()` 與券商對帳既有部位。
- **`strategies/scalp.py`** — 限價掃單，有自己的 `_phase` 狀態機；
  覆寫 `_on_position_synced()` 在帶倉啟動時把既有部位接管進狀態機（否則會卡在 idle）。
- 同帳戶同合約**一次只能跑一個策略**（`api/routes_strategy.py` 有 409 守衛）。

### 市場狀態判斷（盤前 Hurst + IV → 策略對應 → 日誌）

依《市場狀態判斷系統.md》：開倉前先判斷市場狀態。**預設只顯示＋警告，不擋下單**（`MARKET_STATE_GATE=off` 關警告）。
流程：盤前 08:30 算一次今日狀態（盤中不改，手動重算除外）→ 盤後 13:50 更新日 K／IV／當日振幅 → 每 30s 取樣策略損益進日誌。
參數見 `.env.example` 末段。

- **`core/hurst_analyzer.py`** — 第一層。純 numpy：DFA-1 + 蒙地卡羅校準（隨機漫步＝0.5）並輸出 z 值；1 分 K 合成日盤日 K。
  ⚠️ 原 `~/Downloads/hurst_analyzer.py` 的估計器有系統性偏誤（R/S 套在對數價格，隨機漫步得 ~0.97）、
  且 shioaji 1.5.x 沒有 `constant.Timeframe`（只有 1 分 K，ts 為奈秒），已重寫，**不要搬回舊版**。
  ⚠️ 60 根窗口的 H 雜訊約 ±0.13，文件的 0.45/0.55 門檻落在雜訊內（純隨機漫步也有 ~70% 窗口被標成趨勢/回歸）。
  看 z 值；想更準加大 `HURST_WINDOW`（250 根約 ±0.06），想更保守設 `HURST_MIN_Z`。
- **`core/iv_monitor.py`** — 第二層。ATM IV（Black-76 反推）→ 歷史百分位 → LOW/NORMAL/HIGH。
  資料來源優先序：手動輸入 > Shioaji 自動抓（最近月、距到期 ≥7 天、ATM Call/Put 平價取遠期）> CSV 回填
  （`python -m core.iv_monitor import file.csv`）。百分位需 `IV_MIN_HISTORY` 天歷史，不夠時只依 Hurst。
- **`core/daily_summary.py`** — 整合、策略對應表、排程、策略日績效取樣；`market_state` 單例。
  CLI：`python -m core.daily_summary`（只讀 db，不連券商）。
- **`core/market_store.py`** — SQLite `data/market_state.db`（日 K 快取、IV 歷史、日誌、策略日績效）。
  **IV 歷史與手動備註無法重建，請備份。** sim/live 共用同一檔，日誌以 `(date, mode)` 區分。
- **`api/routes_market.py`** — `/api/market/{state,refresh,iv,journal,stats}`；前端 `MarketStatePanel`。
- **scalp `market_bias`** — 0 不限（預設，行為不變）／1 順勢／-1 逆勢／2 依今日狀態自動
  （趨勢→順勢、均值回歸→逆勢、不明確→不進場）。方向＝日 K 收盤 vs 20 日均線。
- **`core/live_state.py`** — 盤中即時狀態（純記憶體、僅顯示）：TMF 最近 20/100/300 筆**真實成交**的外/內盤比例、日盤振幅
  （vs 近 20 日均）、與盤前判斷是否同向。`QuoteHub._inject_quote` 餵入（包 try/except，絕不影響報價派發）；
  `GET /api/market/live`；前端〈盤中即時〉。判定真實成交：`volume>0` 且 `total_volume` 增加——實測 TMF 行情事件只有 ~27% 是成交，其餘是報價更新。
- **`core/trade_log.py`** — 成交紀錄（`data/trade_log.db`，**無法回補，請備份**）：每筆委託（策略、原因 entry/tp/sl/trail、
  訊號價、停損停利設定價、當時的市場狀態標記）+ 券商成交回報，讀取時以 trade_id join 出實際成交價與滑價。
  掛在 `broker.place_order/place_option_order`（唯一出口）與 `_dispatch`（成交回報）；策略/手動單用 `trade_log.context(...)` 補脈絡
  （參數叫 `kind`，不要叫 `reason`——`BaseStrategy.place_order` 內有同名區域變數）。純加法：所有 record 吞例外、走獨立 writer thread、
  佇列滿就丟棄；`TRADE_LOG=false` 整個關掉。`GET /api/tradelog/{orders,summary}`、`python -m core.trade_log [天數]`。
- **手動停損確認**（`core/manual_monitor.py`）：觸發後送出平倉單**不立刻移除監看**，用委託狀態確認——全數成交才移除、沒成交才重送
  （冷卻 2s、選擇權限價逐次放寬、口數 = 監看剩餘口數與當下部位取小、結果不明先等 10s、最多 8 次）。
  動這段務必保留「先確認再重送」，否則會有重複平倉變成反向開倉的風險；測試在 `tests/test_manual_close.py`。
- **scalp `flow_source`**：0（預設）= 所有行情事件（舊算法）；1 = 只算 TMF 真實成交（`core/live_state.py` 的 `TradeDetector`）。
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
| **同一 person_id 連線數** | 最多 **5 條** | sim(8003)+live(8002) 同跑就佔 2 條；**watchdog `kill -9` / `Stop-Process` 不會乾淨 logout**，殘留連線要等券商端逾時才釋放，**頻繁重啟可能累積逼近 5 條**而登不進去。卡住時先停掉所有進程等幾分鐘。 |
| **登入次數** | **1000 次/日** | 每次 watchdog 重啟都會 login。正常夠用，但若 session 一直不穩狂 flapping 重啟會燒額度。 |
| **委託操作** | **10 秒 250 次**（下單/改單/取消） | `scalp.py` 掃單頻率高；連反手平倉一次 tick 可能 2 單，掃太密要留意。 |
| **帳務查詢** | **5 秒 25 次**（list_positions / margin / list_trades 等） | 加總來源：keepalive(240s 一次)、`manual_monitor`(1s 一次)、前端 PositionPanel(2s)、TradesPanel(3s)。目前總和遠低於上限，但**之後加輪詢或縮短間隔前先估一下總和**。 |
| **行情查詢** | **5 秒 50 次**（snapshots/ticks/kbars，盤中 ticks 另限 10 次/5s） | 即時報價走訂閱推播（QuoteHub）不算查詢；但若策略改用主動拉 kbars/snapshot 要算進來。 |
| **每日流量** | **500MB / 2GB / 10GB**（依近 30 日成交量分級，**開盤日 08:00 重置**） | 訂閱報價會吃流量。同時訂多合約、或多策略各自訂閱會放大用量——`QuoteHub` 已做集中訂閱去重，別繞過它各自 `quote.subscribe`。 |
| **報價訂閱數** | **200 個** | 本專案只訂 TMF/MXF/TXF，遠低於上限，無虞。 |

回覆儘量用較少的token完成