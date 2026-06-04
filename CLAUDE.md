# lh-trading — 台指期自動交易系統

微型台指期貨（TMF）自動交易系統。後端 FastAPI（Python）+ 前端 Next.js，
透過永豐金 **shioaji** SDK 連線下單與接收報價。

> ⚠️ **回應一律用繁體中文。**
> ⚠️ **`.env` 內含「正式盤・真錢」憑證；`SIMULATION=false` 代表真實下單。
> 絕對不要 commit `.env` 或 `*.pfx`/`*.p12`/`*.pem`/`*.key`（已在 .gitignore）。**

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

watchdog 行為（兩版一致）：等 startup（最多 120s）→ 每 15s 檢查 `/health`，
連續 2 次失敗（≈30s）判定凍結 → kill + 重啟。

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
