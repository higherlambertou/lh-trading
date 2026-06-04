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

```bash
# 模擬盤（port 8003，main_sim.py 強制 SIMULATION=true，綁 0.0.0.0）
nohup ./run_sim.sh  > /tmp/lh_sim_watchdog.log  2>&1 &

# 正式盤（port 8002，main.py 依 .env，綁 BIND_HOST）— 真錢，啟動前確認 .env
nohup ./run_live.sh > /tmp/lh_live_watchdog.log 2>&1 &

# 前端（port 3002）
cd frontend && npm run dev      # 開發
# 或 npm run build && npm run start   # 正式
```

watchdog 行為：等 startup（最多 120s）→ 每 15s 檢查 `/health`，
連續 2 次失敗（≈30s）判定凍結 → `kill -9` + 重啟。

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
