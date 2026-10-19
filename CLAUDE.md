# lh-trading — 台指期自動交易系統

微型台指期貨（TMF）自動交易系統。後端 FastAPI（Python）+ 前端 Next.js，
透過永豐金 **shioaji** SDK 連線下單與接收報價。

> ⚠️ **回應一律用繁體中文。**
> ⚠️ **`.env` 內含「正式盤・真錢」憑證；`SIMULATION=false` 代表真實下單。
> 絕對不要 commit `.env` 或 `*.pfx`/`*.p12`/`*.pem`/`*.key`（已在 .gitignore）。**

---

## 進度紀錄

日常操作與上手步驟見 [QUICKSTART.md](QUICKSTART.md)，啟動／排錯見 [OPERATION.md](OPERATION.md)，**各模組的細節與研究結論見 [ARCHITECTURE.md](ARCHITECTURE.md)**（新增模組的細節寫那裡，不要加回這個檔案——它每次對話都會載入）。需求進度與更新紀錄在 [update.md](update.md)。完成任何需求（`需求文件/`）或重要更新時，同步更新它的「需求進度」表與「更新紀錄」；`需求文件/` 內的原始文件不改動。

---

---

## 啟動方式（用 watchdog，不要直接 `python main.py`）

shioaji 原生層在永豐 Solace session 不穩時會卡在 I/O 又不釋放 GIL，凍結整個 Python（含 asyncio event loop），`/health` 變 000。
**這是 SDK 層問題，純 Python 改不掉**，所以用外部 watchdog 監看、偵測凍結就自動重啟。重啟是安全的：啟動流程會自動對帳既有部位、清掉殘留委託。

**Linux / macOS（家機部署）**
```bash
# 模擬盤（port 8003，main_sim.py 強制 SIMULATION=true，綁 SIM_BIND_HOST／BIND_HOST／127.0.0.1，不再是 0.0.0.0）
nohup ./run_sim.sh  >> /tmp/lh_sim_watchdog.log 2>&1 &

# 正式盤（port 8002，main.py 依 .env，綁 BIND_HOST）— 真錢，啟動前確認 .env
nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &

# 前端（port 3002）
cd frontend && npm run dev      # 開發；正式用 npm run build && npm run start
```
**Windows（PowerShell，開發機）**：`.sh` 用了 `lsof`／`/tmp`／`trap`，改用 `run_sim.ps1`／`run_live.ps1`（`powershell -ExecutionPolicy Bypass -File .\run_live.ps1`；Ctrl+C 停止；log 在 `%TEMP%\lh_*.out.log`／`.err.log`，每次重啟覆寫）。長期掛機正式交易請用 Linux／macOS 家機的 `.sh`；`.ps1` 沒有位址檢查與換月。

**watchdog 要點**（完整行為見 ARCHITECTURE.md〈watchdog〉）：每 15s 看 `/health`，五態——`healthy`／`nobroker`（200 但沒連券商）／`down`（逾時＝凍結）／`noiface`（`BIND_HOST` 不在本機＝Tailscale 斷線，不殺後端）／`rollover`（後端要求換月重啟，正常關閉後重啟）。
連上過之後 `nobroker`／`down` 連續 2 次（≈30s）就 kill -9 並重啟。**腳本鐵則**：log 用 `>>`（`>` 會清空正在跑那個的 log）；變數後面緊接全形標點要寫 `${var}`；
長等待一律用 `isleep`（背景 sleep＋wait，否則 `pkill` 會被拖最長 10 分鐘）；`main.py` 啟動前先試綁 `BIND_HOST`（`core/bind_check.py`）。測試 `tests/test_watchdog_scripts.py`。

**log**：watchdog `/tmp/lh_{sim,live}_watchdog.log`，應用程式 `/tmp/lh_{sim,live}.log`。**停止**：`kill <watchdog_pid>`（會一併關掉子進程；只 kill 子進程它會拉回來）。
**凍結時抓堆疊**：`kill -USR1 <pid>`（main_sim.py 已掛 faulthandler）。排錯與「誰關的」判斷見 OPERATION.md〈常見狀況〉。

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

> 模擬盤固定 port 8003；綁 `SIM_BIND_HOST` → `BIND_HOST`（同一個 Tailscale 位址）→ `127.0.0.1`，**不再是 `0.0.0.0`**（後端沒有身分驗證，發現 41）；不受 `PORT` 影響。位址不在本機時 `main_sim.py` 啟動前就結束。

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

## 架構重點（只留不能犯的規則；每個模組的細節、資料來源、研究結論在 [ARCHITECTURE.md](ARCHITECTURE.md)，動到之前先讀）

### 交易路徑與下單
- **入口**：`main.py` 正式盤（依 `.env` 綁 `BIND_HOST:PORT`）；`main_sim.py` 強制 `SIMULATION=true`／`DEV=false`、綁 `SIM_BIND_HOST`→`BIND_HOST`→`127.0.0.1` 的 8003（不再是 `0.0.0.0`）。
- **`core/broker.py`**：用 `call`／`acall`（重連安全；`acall` 丟 executor）；登入有硬性逾時（`LOGIN_TIMEOUT` 25s）。**下單的合約解析仍是 R1，換月不要去動它**（真錢路徑、離線驗證不了）。
- **委託回報**（`extract_order_event`／`to_mapping`）：shioaji 1.5.x 的回報是 `OrderEventDict`，**不是 dict 子類**，不要用 `isinstance(msg, dict)`；解析壞了 scalp 會認不出自己的成交而重複進場（update.md 發現 21）。
- **`strategies/base.py`**：`_go()`／`_check_sl_tp()` **先改 state 再 await**（重入防護）；**平倉單一律 `reduce_only=True`**（不能靠部位方向推算，`tests/test_exit_not_blocked.py` 用 AST 檢查）；
  已實現損益一律走 `_add_realized()`（不要直接 `realized_pnl +=`，`tests/test_risk_day.py` 會檢查）；風控的「當日」＝交易日（`RISK_DAY_START` 15:00 換日）；損益只在記憶體，重啟歸零；`start()` 會與券商對帳。
- **`strategies/scalp.py`**：`_phase` 狀態機，鐵則同 base（先改 state 再 await，await 之後要重新確認狀態）；`max_qty` 是持倉上限，進場前 `_entry_gate()` 向券商查；`_on_position_synced()` 接管帶倉啟動。測試 `tests/test_scalp_state.py`。
- **同帳戶同合約一次只能跑一個策略**（409 守衛）；停止策略時有持倉（策略或券商帳上）回 409，`?force=true` 才強制；`stop_all()`（關機）不經過檢查。
- **逐 tick 策略只吃 TMF**（`quote_prefix`）：新增逐 tick 策略要設；scalp 與 K 棒策略維持 `None`。
- **報價進入點的盤前試算隔離**（`PREOPEN_WINDOWS`）：靠**時段**、不靠 `simtrade` 旗標（旗標誤判會讓策略變瞎子）；`tests/conftest.py` 預設關閉。
- **手動下單的當沖**：TMF 不能勾當沖（保證金減半只給 TX／TE／TF／MTX 日盤）；**策略一律 `Auto`**（`tests/test_daytrade_order.py` 掃 `strategies/`）。
- **手動停損確認**：觸發後不立刻移除監看，用委託狀態確認再重送——**務必保留「先確認再重送」**，否則可能重複平倉變成反向開倉（`tests/test_manual_close.py`）。
- **持倉保護**：手動停損監控落地（`data/manual_watches_<mode>.json`）、重啟後與券商部位對帳恢復（`reconcile`：部位不在的舊監看一律丟掉）；**策略不自動恢復**；新增 `ManualWatch` 欄位要決定進不進 `_SAVED_FIELDS`。
- **換月**（`core/rollover.py`，發現 38）：後端偵測訂閱的近月已結算 → `/api/health` 帶 `restart_wanted` → watchdog 正常關閉重啟；只在合約檔更新後的時間點試，四次不成就通知放棄。`ROLLOVER_RESTART=false` 關閉。
- **帳務查詢頻率**（`positions_refresh_loop` 等）：worker 單執行緒，查詢期間下單會排隊；**加輪詢或縮短間隔前先估總和**（見下方上限表）。`broker.kbars()` 只在盤前／盤後用。

### 通知
- 一律走 `notify()`（背景執行緒、永不丟例外、有冷卻 key、模擬盤不推播）；交易路徑上再包 try/except，**通知壞了不能影響平倉**；訊息**不放帳號與權益**（連「占權益 %」也不行）。
- **測試絕不能碰真實通道**：`tests/conftest.py` 已設 `NOTIFY_SKIP_DOTENV=1` 並把 `_http_post`／`_http_get` 換成會失敗的替身（發現 34 的事故）；`tests/test_notify.py` 守著。
- **指標類通知**（波動放大／合約到期／盤前摘要，發現 37、39、40）：只通知、不動委託、**不接進場訊號**（買賣力道、Hurst 標籤的檢驗沒過）。

### 資料與記錄（純加法，不影響交易）
- trade_log、進場市場狀態紀錄、盤前跨市場資訊紀錄、flow_1m、ticks 落地：**寫入失敗不擋交易**；**沒有任何策略或風控模組可以讀這些欄位**（`tests/test_entry_log.py`、`tests/test_premarket_log.py` 靜態檢查；只通知的 `daily_brief.py`、`reminders.py` 是明列的例外）。
- **信息收集（籌碼、總經事件、全球市場、新聞）在另一個 repo `lh-intel`**（`~/Documents/GitHub/lh-intel`，獨立常駐服務、獨立 `.env`；操作見它的 `OPERATION.md`）：交易系統**只能唯讀**它的 `data/intel.db`：〈資訊〉分頁用 `core/intel_reader.py` 顯示「現在已知的全部」（只顯示）；**研究、回測、進決策的程式必須照它的 `intel/read.py` 的 as_of 規則讀（回補列預設不可見），不能用 `intel_reader.py`**。**目前沒有任何策略或風控讀它**，要讀之前先登記驗證標準。
- **無法回補、請備份**：`data/trade_log.db`、IV 歷史與手動備註（`market_state.db`）、`data/option_spread.db`、`data/signals.db`（影子訊號的前向資料）（備份 `core/backup.py`，交易日 14:00；`option_spread.db` 沒進備份）。`tick_store compact` 的 VACUUM 要先停服務。
- **`bars_1m` 的 hhmm 是 K 棒「結束」時間，`flow_1m` 的 ts 是分鐘「起點」**，序號差 1（`tests/test_vol_watch.py` 有對照）；`flow-fetch` 只補整天缺的日子。
- 盤前紀錄：資料時間戳晚於 T 的拒絕、美股休市日 null＋原因（不沿用前一天）、更正＝新增 correction（資料庫觸發器擋 UPDATE／DELETE）。

### 顯示與分析（純函式、不碰下單）
- **市場狀態**（Hurst＋IV）預設只顯示＋警告（`MARKET_STATE_GATE=off` 關警告）。**不要把 `hurst_analyzer` 搬回 `~/Downloads` 舊版**（R/S 估計器偏誤）；60 根窗口的 H 雜訊約 ±0.13，看 z 值。
- **建議停損停利**：σ 定義必須與 `stop_calibration.trailing_sigma` 一致；`stop_calibration.json` 是事先登記、跑一次的表，**不要為了數字好看重算**（重算先重新登記）；**勝率不是優勢**——進場沒有資訊時每筆期望值＝−成本。
- **機率範圍帶**（發現 48）：σ 與建議停損停利共用同一個定義；`range_calibration.json` 是事先登記、跑一次的表（樣本外 44／48 格過），**不要為了數字好看重算**；只說會走多遠、不說方向，**不要當進場訊號**；策略／風控／下單模組不得引用（`tests/test_range_suggest.py` 靜態檢查）。
- **選擇權試算**：只做買方；`option_chain` 只在按下試算時查、5 秒快取，**不要輪詢**；口數 0 要擋。
- **回測**放獨立子行程（`nice -n 10`、同時只跑一個），**不要改成在交易行程裡跑**（CPU 密集會搶下單與報價的 GIL）。
- 其餘（盤中即時狀態、盤中脈絡、破產機率、指標回放、硬門檻、選擇權歷史與離線檢驗）見 ARCHITECTURE.md。

### 離線研究與測試
- **判定標準（檢定數量、門檻）在跑之前寫進 update.md，每項只跑一次**，不依結果回頭改設計；訊號只用前一日以前的資料；測試用「植入已知現象＋隨機漫步上的假陽性率＋偷看的金絲雀」，不要只測能跑。
- 測試：`python -m pytest tests -q`（用裝了 shioaji、numpy、fastapi、httpx 的 Python）；前端有 node 腳本（`tests/frontend/`）。新增或修改要寫測試；改完用變異測試確認抓得到。
- `需求文件/` 內的原始文件不改；完成需求或重要更新時同步更新 update.md 的「需求進度」與「更新紀錄」。

### event loop 鐵則
任何同步 shioaji 呼叫**不可**直接跑在 asyncio event loop 上——一旦 SDK 卡住會凍結整個服務。一律用 `broker.acall(...)` 或 `loop.run_in_executor(...)`。同步的本機 SQLite 讀取用 `asyncio.to_thread`。（曾因 `main.py` keepalive 直接同步呼叫而整個凍住。）

---

## Shioaji 使用上限（會影響本專案的部分）

官方文件：<https://sinotrade.github.io/zh/tutor/limit/>。超限時行情查詢會**回空值**、
帳務/委託會被**暫停 1 分鐘**，持續違規會**封 IP 與 person_id**。以下挑出對本專案實際有風險的：

| 限制 | 數字 | 本專案的注意點 |
|---|---|---|
| **同一 person_id 連線數** | 最多 **5 條** | sim＋live 同跑佔 2 條；**重啟可能漏出孤兒 worker**（強殺時 shioaji 子進程來不及登出，PPID=1 帶著連線一直活著）；滿了新 worker 就登不進去。`parent_alive()` 已修（需重啟後端才生效）；檢查與清理見 OPERATION.md〈孤兒 worker〉。**唯讀歷史工具（`hurst_study fetch`、`indicator_history flow-fetch`）每次再佔 1 條**，登入前會先問後端（`core/broker_guard.py`）。完整說明見 ARCHITECTURE.md。 |
| **登入次數** | **1000 次/日** | 每次 watchdog 重啟都會 login。正常夠用，但若 session 一直不穩狂 flapping 重啟會燒額度。 |
| **委託操作** | **10 秒 250 次**（下單/改單/取消） | `scalp.py` 掃單頻率高；連反手平倉一次 tick 可能 2 單，掃太密要留意。 |
| **帳務查詢** | **5 秒 25 次**（list_positions / margin / list_trades 等） | 加總來源：keepalive(240s 一次)、`manual_monitor`(1s 一次)、`positions_refresh_loop`(部位 5s 一次、已實現損益 60s／300s 一次)、scalp 進場把關(每次進場前 2 次：部位＋委託；被擋下後 3 秒內不再查)、前端 PositionPanel(2s)、TradesPanel(3s)。目前總和遠低於上限，但**之後加輪詢或縮短間隔前先估一下總和**。 |
| **行情查詢** | **5 秒 50 次**（snapshots/ticks/kbars，盤中 ticks 另限 10 次/5s） | 即時報價走訂閱推播（QuoteHub）不算查詢；但若策略改用主動拉 kbars/snapshot 要算進來。 |
| **每日流量** | **500MB / 2GB / 10GB**（依近 30 日成交量分級，**開盤日 08:00 重置**） | 訂閱報價會吃流量。同時訂多合約、或多策略各自訂閱會放大用量——`QuoteHub` 已做集中訂閱去重，別繞過它各自 `quote.subscribe`。 |
| **報價訂閱數** | **200 個** | 本專案只訂 TMF/MXF/TXF，遠低於上限，無虞。 |

回覆儘量用較少的token完成
