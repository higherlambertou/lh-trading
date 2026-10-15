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
nohup ./run_sim.sh  >> /tmp/lh_sim_watchdog.log 2>&1 &

# 正式盤（port 8002，main.py 依 .env，綁 BIND_HOST）— 真錢，啟動前確認 .env
nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &

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

watchdog 行為（wall-clock 計時；`run_live.sh` 有位址檢查，`.ps1` 還沒有）：每 15s 檢查 `/health`，判斷四態——
`healthy`(200+`broker_connected:True`)／`nobroker`(200 但沒連券商＝登入失敗或斷線)／
`down`(逾時或非 200＝凍結，連 health 都拿不到 threadpool 執行緒)／**`rollover`(健康、但後端在 health 帶 `restart_wanted=rollover`：近月合約已結算，要重新登入換月；update.md 發現 38)**／**`noiface`(`BIND_HOST` 這個位址不在本機＝Tailscale 斷線：不是後端的問題，不計失敗、不殺後端；
起不來就等位址回來再啟動，並推播一次；update.md 發現 36)**。`main.py` 啟動最前面也先試綁 `BIND_HOST`（`core/bind_check.py`），位址不在就結束碼 3，不先開 worker 登入券商。
watchdog 的 log 要用 `>>` 附加：第二個 watchdog 被單例鎖擋下來時，`>` 會先把正在跑那個的 log 清空。bash 的變數後面緊接全形標點要寫 `${var}`（`tests/test_watchdog_scripts.py` 有掃）。等待（退避、檢查間隔）一律用 `isleep`（背景 sleep + wait）：bash 要等前景指令跑完才執行 trap，直接 `sleep 600` 會讓 `pkill -f run_live.sh` 被拖最長 10 分鐘、單例鎖一直在（2026-10-14 實際發生，同上測試有掃）。
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
  **平倉單一律明講 `reduce_only=True`**（停損停利、ATR 移動停損、回歸平倉、反手的平倉腿、scalp 停損）：這些呼叫都是先把 `state.position` 歸零再下單，
  不能靠部位方向推算「是不是平倉」，否則會被風控當成進場單擋掉（update.md 發現 24）；`tests/test_exit_not_blocked.py` 用 AST 檢查。反手時新進場被擋 → 回到空手，不還原成舊部位。
  風控的「當日」＝交易日（`RISK_DAY_START`，預設 15:00 換日）：當日損益 = 累計 − 換日基準（`_day_base`），**已實現損益一律走 `_add_realized()`**
  （它會先換日再記帳；策略裡不要直接 `realized_pnl +=`，`tests/test_risk_day.py` 會檢查）。損益只在記憶體，後端重啟歸零。
- **`core/backtest.py`** — 回測工具（`python -m core.backtest`，見 OPERATION.md〈回測〉）：驅動**真實的策略類別**（含真的 `place_order` 風控，只把送單換成空操作），
  樣本內／外、滾動式驗證、打亂資料對照、偵測力。參數網格內建在 `preset_grid()`；`validate_combo()` 擋參數名稱打錯（`_apply_params` 會靜默忽略未知的鍵）。
  修策略邏輯後可以重跑，看樣本外有沒有變。測試 `tests/test_backtest.py`（手算得出答案的合成資料）。
  **前端〈分析〉分頁的〈策略回測〉面板**（`frontend/components/BacktestPanel.tsx`、`frontend/lib/backtest.ts`）走 `api/routes_backtest.py`（`/api/backtest/{info,run,jobs,jobs/{id},jobs/{id}/cancel}`）：
  把 `python -m core.backtest` 放在**獨立子行程**跑（`nice -n 10`、自己的行程群組、同時只跑一個、預設 2 個平行行程、逾時 30 分鐘），**不要改成在交易行程裡跑**（CPU 密集會跟下單、報價搶 GIL）。
  工作狀態只在記憶體；`BACKTEST_API=false`／`BACKTEST_WORKERS`／`BACKTEST_TIMEOUT_SEC`。測試 `tests/test_routes_backtest.py`、`tests/test_frontend_backtest.py`（node 腳本 `tests/frontend/backtest_check.js`）。
- **策略構想的檢驗**（`core/structure_study.py`、`core/oi_study.py`、`core.backtest --scaled`，update.md 發現 27）：離線分析，不碰交易路徑。**判定標準（檢定數量、Bonferroni 門檻）在跑之前寫進 update.md，每項只跑一次**，
  不依結果回頭改設計——這是這套檢驗可信的前提。參考振幅 R＝前 20 個時段（高－低）的平均，只用當天以前的資料（`ref_ranges`）；`--scaled` 把點數型參數改成 R 的倍數，固定版與標準化版都要用同樣的 `--warmup`。
  `core/oi_study.py`、`core/trend_study.py`（TX 日行情 2003 起，連續報酬用「兩天都有成交的最近月合約」避免展期假報酬；**第 k 期的部位只能用第 k−1 日收盤的訊號**）、`core/vrp_study.py`（選擇權歷史 ATM IV，定義重用 `core/iv_monitor`；
  標準誤以到期月為群組）的資料都是期交所公開下載（`data/taifex/`，只當資料讀、拒絕錯誤頁、請求間隔 1 秒）。`structure_study confirm` 是獨立驗證（單一單尾檢定）。**`bars_1m` 現在是 2020-03-02 起（1,611 天）**：回測與 `structure_study` 預設用全部，要重現舊報告加 `--start 2025-09-03`。
  測試用「植入已知現象＋隨機漫步上的假陽性率＋與逐筆暴力算法一致＋偷看的金絲雀」，不要只測能跑；合成資料的價格水準與雜訊結構要接近真實（否則成本占比與標準誤會失真）。
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
  **報價事件本來就帶、worker 以前丟掉的欄位**（2026-10-12 實測，update.md 發現 28）：五檔價量、交易所時段累計內外盤（`bid_side_total_vol`＝內盤）、`avg_price`（交易所 VWAP）、`underlying_price`（現貨指數，夜盤凍結）、
  交易所的時段 `high/low`；`_extract_quote` 現在轉發（五檔放在 `book`，全部附加、任何一個欄位壞掉都不能讓行情掉）。**漲跌的欄位叫 `price_chg`，沒有 `change_price`**（以前讀 `change_price` 永遠是 0，畫面漲跌永遠「—」）。
  `live_state.feed(..., quote=)` 吃這些：五檔價差／失衡（近 30 秒平均）、VWAP、基差（只在 09:00~13:30 有效）、交易所累計內外盤、開盤後 30／60 分的早盤與之後振幅（`cp`）；
  日盤高低在「後端開盤後才啟動」時改用交易所的值（自己從開盤就在跑則自己的優先，兩邊不一致會寫 warning）。WebSocket 推播**不含 `book`**（只走 `/api/market/live`）。
  另外追蹤小台、大台的真實成交方向（各合約最近 30 分鐘的 `TimedFlow`，同一套「真實成交」判定、各合約代碼各自去重）：`snapshot()["xc"]` 給近 5／15／30 分鐘的外盤**口數**占比與微台−大台的差（成交少於 10 筆不顯示）。
  用時間視窗不用「最近 N 筆」，因為各合約的成交頻率差很多；小台與大台**只**進這個比較，不碰微台的最新價、日高低、五檔與逐筆視窗。只顯示、沒驗證過差有沒有意義。
- **`core/intraday_context.py`** — 盤中時段脈絡（只顯示）：各 15 分鐘時段的典型波動倍數、開盤後 30／60 分振幅 → 之後振幅的預期範圍（迴歸，用 TXF 1 分 K 估計）、距收盤倒數、結算日提醒。
  在背景執行緒每天算一次（`IntradayContext.get` 只回快取，第一次回 None），**R20 只用當天以前**；`python -m core.intraday_context screen|show`。相對成交量與結算日效應的檢驗沒過，不做。
- **`core/trade_log.py`** — 成交紀錄（`data/trade_log.db`，**無法回補，請備份**）：每筆委託（策略、原因 entry/tp/sl/trail、
  訊號價、停損停利設定價、當時的市場狀態標記）+ 券商成交回報，讀取時以 trade_id join 出實際成交價與滑價。
  掛在 `broker.place_order/place_option_order`（唯一出口）與 `_dispatch`（成交回報）；策略/手動單用 `trade_log.context(...)` 補脈絡
  （參數叫 `kind`，不要叫 `reason`——`BaseStrategy.place_order` 內有同名區域變數）。純加法：所有 record 吞例外、走獨立 writer thread、
  佇列滿就丟棄；`TRADE_LOG=false` 整個關掉。`GET /api/tradelog/{orders,summary}`、`python -m core.trade_log [天數]`。
- **進場市場狀態紀錄**（`core/entry_log.py`，需求文件《進場市場狀態紀錄》v1，update.md 發現 25）：**只多記欄位，不改任何交易行為**，為日後驗證 Hurst／IV 有沒有用累積資料。
  委託送出時 `TradeLog.record_order` 把「當下已有的最新市場狀態」（`daily_summary._set_summary` 掛進 `set_market_tag` 的 `snapshot`）＋停損價＋策略參數存成 `orders.state_json`
  （取不到記 null＋`missing_reason`，不補 0）；成交回報進來後 `EntrySync`（只在 trade_log 的 writer thread 裡跑）以 **FIFO 淨額**把每筆成交判成進場（寫 `entry_snapshots`）
  或出場（寫 `exit_fills`，帶損益與出場原因）。冪等：全部 `INSERT OR IGNORE`，重啟時從 `start_event_id:<mode>` 重放；重複送達的同一筆成交用券商的 `deal_ts`／`exchange_seq` 辨識。
  sim／live 是兩個行程共用 `trade_log.db`，**各自只處理自己模式的成交**。`reconcile` 在啟動時依券商持倉補「功能啟用前就存在的部位」（存在 `entry_log_meta` 的 `legacy:*`，重放時放回同一個位置）。
  失敗不擋下單：`ENTRYLOG ALERT` ERROR log＋`entry_log.alerts` 計數＋`entry_log_errors`＋`GET /api/tradelog/entries/status`。
  策略類型 `STRATEGY_TYPES` 是人工定義的表，新增策略要記得補。**沒有任何策略或風控模組可以讀這些欄位**（`tests/test_entry_log.py` 靜態檢查）。
  `GET /api/tradelog/entries`、`entries.csv`（UTF-8 含 BOM）、`entries/status`。
- **手動下單的當沖**（`core/daytrade.py`、`api/routes_order.py`、`OrderPanel`，update.md 發現 26）：期交所保證金減半只給 TX／TE／TF／MTX 的日盤，**微台 TMF 不在內**；所以只有手動下單的小台 MXF、大台 TXF 能勾當沖（倉別 `DayTrade`），
  後端擋 TMF 勾當沖與日盤（週一～五 08:45~13:45）以外勾當沖；`manual_monitor` 的平倉單沿用當沖（日盤以外退回 `Auto`）。**策略一律 `Auto`**（`tests/test_daytrade_order.py` 掃 `strategies/` 不得出現 `DayTrade`）。
  永豐沒寫當沖單的開平倉怎麼標，「兩邊都標」是常見做法、未實測，第一次先用 1 口小台確認。
- **選擇權買方試算**（`core/options_calc.py`、`api/routes_options.py`、worker `option_chain`、前端〈分析〉分頁〈選擇權試算〉，update.md 發現 29）：**純函式、唯讀、不碰下單與策略**。只做買方（最大虧損＝權利金）：買跨式／買勒式／買單向 Call、Put，履約價用 Delta 或「預期波動點數」選。
  Black-76 與 `iv_monitor` 同一套（T＝日曆天／365、`IV_RATE` 1.5%）；遠期價與 ATM IV 由鏈上買賣平價反推。`analyze` 算成本（照賣價）、損益兩平（分段線性逐段找零點）、Greeks、價格×時間／IV 的損益表、模型與歷史機率；
  `close_analysis`（平倉試算）對「全部持有／只平掉某一腳／全部平掉」各算已實現損益、**淨成本＝全部買進成本 − 已收回的錢**（＝留下的腳到期要值多少才回本＝最大總虧損）與情境表。
  資料：worker `option_chain` 一次 `api.snapshots` 查整個到期月（**只在使用者按下試算時查、5 秒快取，不要輪詢**）；手動模式用 `model_quotes` 造模擬鏈，休市也能用。口數明講 0 要擋（不能默默變 1）。
- **選擇權的歷史資料與離線檢驗**（update.md 發現 30，全部離線、不碰交易路徑）：`core/option_history.py` 每月下載一次期交所〈選擇權每日交易行情〉、整理成近價鏈存 `data/taifex/opt/chain_YYYY_MM.csv.gz`（2010-01 起，約 13 MB；
  月選擇權最近 2 個到期月 ±12%、週選擇權距到期 ≤14 天 ±6%，一般＋盤後；`python -m core.option_history fetch|status`，請求間隔 1 秒、錯誤頁不存、失敗的月重跑補抓）。**到期日當天結算價整欄是 0**（TX 也是）：F 改用價內選擇權最後成交價反推最終結算價（散得太開就算不出）；
  舊檔沒有『契約到期日』欄位，到期日用第三個週三推算，**春節、端午、颱風順延的月份會錯**——`core/option_backtest.py` 用 TX 日行情（`data/taifex/tx/`）該契約最後有成交的日子更正（`expiry_fixes`），到期日算不出最終結算價的週期用 TX 當天收盤補（`rescue`）。
  `core/option_backtest.py`（`python -m core.option_backtest sanity|run`）驅動 7 個事先登記的檢定：每個月選擇權到期月一個週期、到期後第一個交易日買 ATM 跨式；出場 X0~X3、進場訊號 S1~S3；**訊號只用進場日前一個交易日以前的資料**（`tests` 有金絲雀）、要賣就在下一個交易日收盤照買價賣。
  `core/option_spread_study.py` 列歷史收盤價差（描述）；`core/hedge_sim.py` 用 TXF 1 分 K 當微台對沖跨式的 Delta（`bars_1m` 是連續月，**到期日最後一根要用 13:30 那根，不是 13:45**）；`core/option_spread_log.py` 盤中每 10 分鐘取樣整條鏈的價差（`OPTION_SPREAD_LOG`，需重啟後端才開始記，存 `data/option_spread.db`，無法回補）。
  **判定標準在跑之前寫進 update.md、每項只跑一次**；收盤的最後最佳買賣價近年（2024~）價差很寬，多半是失真，不要當真實進出成本。`vrp_study` 曾因沒解析 `20260223` 這種日期格式讓春節順延的月份 T 算錯（已修）。
- **盤前跨市場資訊紀錄**（`core/premarket_log.py`、`api/routes_premarket.py`、`core/premarket_study.py`，需求文件《盤前跨市場資訊紀錄與驗證》，update.md 發現 31）：每個台股交易日 T（`PREMARKET_TIME`，預設 08:30）一筆快照——台指期夜盤報酬（基準，本機 `ticks.db`）、費半、台積電 ADR、VIX。
  **只記錄，不影響任何交易決策**：沒有任何策略或風控模組讀這些欄位（`tests/test_premarket_log.py` 靜態檢查＋啟用前後重播訂單完全相同）；寫入失敗只警示（ERROR log、計數、`premarket_errors`、備援檔 `data/premarket/premarket_errors.jsonl`），不擋任何事。存在 `trade_log.db`（`premarket_snapshots`，資料庫觸發器擋 UPDATE／DELETE：更正＝新增 correction、作廢＝新增 void）。
  規則：資料時間戳晚於 T 的一律拒絕；美股收盤用 `zoneinfo` 算（夏令 04:00／冬令 05:00 台北，提早收盤日 13:00 ET，VIX 晚 15 分鐘）；**美股休市日該欄位 null＋原因、不沿用前一天**（週一用上週五）；缺值與來源失敗一律 null＋原因、沒有預設值；異常值只標記（`PREMARKET_RANGE_*`，費半、TSM 預設 ±25%）；每個交易日恰好一筆（冪等，部分唯一索引）。
  **來源**：Yahoo 對這台機器回 429，預設用 Nasdaq 公開 API（TSM、SOX）＋Cboe 官方 CSV（VIX）（`PREMARKET_SOURCES`；Yahoo 轉接器沒對真實 Yahoo 驗證過；失敗轉移、429 後暫停 6 小時）。**Cboe 的 VIX 歷史在美股休市日也有值（環球交易時段），只用開市日的列**。台股休市用證交所公開日曆（快取；查不到退回『平日』並標 `fallback_weekday`），颱風假事後 `python -m core.premarket_log void 日期 --reason …`。
  **面板與預覽**：前端〈市場〉分頁〈盤前跨市場快照〉（`PremarketPanel.tsx`、`lib/premarket.ts`；`tests/test_frontend_premarket.py` 用後端現算的資料渲染）。`GET /api/premarket/preview` 是『如果現在記』的試算：不寫入、60 秒快取、**會去抓外部來源，不要輪詢**（面板只在載入、按鈕、後端從失敗恢復時各呼叫一次）。
  `run_once` 的檢查順序固定：已記過 → **台股日曆（休市就 skipped）** → T 之前 → 13:45 之後；**日曆一定先於時間窗**，否則週末／假日重啟會誤報錯過。錯過只記 WARNING＋`premarket_errors`（`missed`），不算失敗警示；證交所日曆抓不到時 10 分鐘內不重試。
  **事後補值**（update.md 發現 35）：免費來源（Nasdaq、Cboe）在 T＝08:30 還沒有前一天的美股資料（實測 12:16 才有），所以美股欄位常先是 `stale_or_missing` 空值；`premarket_log_loop` 每 10 分鐘掃最近 5 天，來源有了就補一筆 correction（`backfill_once`，更正說明「自動補值（來源較晚更新）」，`detail.backfilled_at`）。
  **`add_correction` 以「目前的有效內容」（最新一筆更正，沒有就是原始）為底再套 patch**，連續更正才會累積；補值只補 `US_KEYS` 的可重試空值。測試的預設資料庫在 `tests/conftest.py` 換成暫存的（沒傳 `store` 的呼叫不能碰真的 `trade_log.db`）。
  **歷史預檢**（`python -m core.premarket_study fetch|sanity|run`，不連券商）：TX 日行情的**盤後列的交易日期＝它之後那個日盤的交易日**；對齊與特徵直接用 `premarket_log` 的函式（同一套定義）；判定標準事先寫在 update.md、只跑一次。結論：VIX（相對近 20 日平均）對日盤波動有增量，費半沒有；只決定要不要繼續記錄，不決定上線。
- **持倉保護**（update.md 發現 18、32；`core/protection.py`、`core/notify.py`、`core/startup_protection.py`、`core/manual_monitor.py` 的落地與 `reconcile`）：停損停利以前只活在進程記憶體，凍結／重啟（watchdog 常態）就消失。
  ① **手動停損監控落地**（`data/manual_watches_<mode>.json`，原子寫入，登記／修改／移除都存；測試預設關閉＝`state_path=None`，只有 `setup()` 才指定）；② **重啟後對帳恢復**：`main.py` 啟動時建一個 `protect_after_start()` 背景工作（自己等券商連上，最多 10 分鐘），用 `reconcile()` 把落地的監看和**券商現在的部位**對帳——
  口數依建立先後分配、不超過部位、方向與代碼要對得上、**部位不在的舊監看一律丟掉（絕不能把舊停損掛到新部位）**，限價開倉單還掛著才留；連不上就保留檔案不動；③ **策略不自動恢復**（會下真單，刻意不做）；④ 〈部位〉橫幅＝`protection.unprotected()`（只有停損點數 > 0 且 `seen` 的監看、或策略在跑的微台算保護）；
  ⑤ **推播**（`core/notify.py`，Telegram／ntfy／webhook，只用 urllib、背景執行緒、永不丟例外、秘密不進 log、冷卻跨進程記在 `data/notify_cooldown.json`、全部失敗會還冷卻）：啟動報告（有持倉才送）、連不上券商、watchdog 判定凍結／進程消失且**最後已知有持倉**
  （後端每 5 秒寫 `data/position_state_<mode>.json`，watchdog 讀它；`python -m core.notify watchdog` 由 `run_live.sh`／`.ps1` 在 kill 之前背景呼叫，不能拖慢重啟）。**這些全部不下單**。新增會動 `ManualWatch` 的欄位時，記得決定要不要加進 `_SAVED_FIELDS`（暫時狀態不要存）。
  **通知的種類與規則**（update.md 發現 34；全部只通知、不動委託）：除了上面的啟動報告／凍結，還有手動停損停利事件（`manual_monitor._note`：觸發／完成／第 3 次起重送／放棄）、保證金風險（`core/margin_watch.py`，掛在 `cache_refresh_loop`）、
  手動單成交（`core/fill_notify.py`，掛在 `TradeLog.deal_listeners`；成交回報只有 trade_id，合約與買賣方向靠 `TradeLog._recent` 記的委託脈絡）、當沖倉位 13:15／13:25 提醒（`core/reminders.py`＋`daytrade.note_open`）。`core/notify.notify()` 在 `SIMULATION=true` 時不推播（`NOTIFY_SIM=true` 例外）。
  新增通知一律走 `notify()`（背景執行緒、永不丟例外、有冷卻 key）；在交易路徑上呼叫要再包 try/except，**通知壞了不能影響平倉**。
  **測試絕不能碰真實通道**：`.env` 有真的 Telegram token 之後，任何會載入 `.env`、或呼叫真的 `_http_post`／`_http_get` 的測試都會把假警示送到使用者手機（發現 34 的事故）——`tests/conftest.py` 已設 `NOTIFY_SKIP_DOTENV=1`、把兩個 HTTP 函式換成會直接失敗的替身；`tests/test_notify.py` 有測試守住它。
  測試的暫存目錄在 macOS 要 `fs.realpathSync`（`/var` 是 `/private/var` 的連結，否則 node 會把同一個模組載入兩份）。
- **依波動的建議停損停利**（update.md 發現 33；`core/stop_calibration.py`、`core/stop_calibration.json`、`core/stop_suggest.py`、`GET /api/market/suggest`、前端〈委託〉面板的 `StopSuggestCard`）：**純顯示、不碰下單**。
  σ＝進場前最近 30 個 1 分鐘收盤價變動的樣本標準差（至少 25 個有效）——**`stop_calibration.trailing_sigma` 與 `stop_suggest.sigma_from_closes` 定義必須一致**（`tests/test_stop_suggest.py` 有一致性測試），距離一律用 k·σ√T。
  `core/stop_calibration.json` 是**事先登記、跑一次**的實測表（1,609 個日盤的 TXF 1 分 K、39 晚夜盤的 TMF `flow_1m`）：被雜訊掃到的機率（肥尾：3σ 實測 1.3%、常態公式 0.3%）、沒有技巧時 T 分鐘內停利先到／停損先到／都沒到；**不要為了數字好看重算，重算先重新登記**。
  **勝率不是優勢**：進場沒有資訊時每筆期望值＝−成本（停止定理），與停損停利怎麼放無關；基準勝率＝停損÷(停利＋停損)（持有到碰到一邊為止）；要靠時機多贏成本÷(停利＋停損)個百分點才打平。有時間限制時已結算勝率偏向近的那邊，**不要拿公式值當 T 分鐘內的勝率**。
  執行時 σ 來自 `flow_1m`（TMF 真實成交，與 TXF 的 σ 比值中位數 0.988）；端點只讀本機資料庫與記憶體、不查券商，成交太少就不給建議。新增持有時間或 k 倍數要同步 `stop_calibration` 的網格與 JSON。
- **指標類通知**（update.md 發現 37；全部只通知、不動委託、不放權益）：只接「風險尺度變了」與「系統看不到」，**不接進場訊號**（買賣力道、Hurst 標籤的檢驗沒過，接了只是雜訊）。
  ① **波動放大**（`core/vol_watch.py`，`VOL_WATCH`／`VOL_ALERT_RATIO`＝2.0）：連續交易時段內、最近 30 分鐘 σ ÷「同時段（距時段開始第幾分鐘）最近 20 個時段 σ 的中位數」≥ 門檻才通知，冷卻 60 分鐘；**有沒有持倉都發**（update.md 發現 39，空手時講現在的雜訊範圍與平衡型停利停損點數）；有期貨持倉時訊息另帶離停損還有幾點、是雜訊範圍（σ√30）的幾倍、被掃到的機率（`stop_calibration.json` 內插）。基準只用當天以前的時段（日盤＝TXF `bars_1m`、夜盤＝TMF `flow_1m`，同一分鐘不到 10 個時段就不判斷）；σ 與建議停損停利卡是同一個函式。**`bars_1m` 的 hhmm 是 K 棒「結束」時間（08:46～13:45）、`flow_1m` 的 ts 是分鐘「起點」，序號要差 1**（`tests/test_vol_watch.py` 有對照）。
  ② **合約到期提醒**（`core/reminders.py` 的 `check_expiry`，掛在 `reminder_loop`）：持有的期貨／月選擇權在最後交易日（第三個週三，休市用證交所日曆順延）前 1～3 天、當天 08:50、當天 12:30（緊急）各一則；週選擇權等認不得的代碼不提醒。**結算後會自動重啟換月**（見下一條〈換月〉）。
  ③ **盤前摘要**（`core/daily_brief.py`，`DAILY_BRIEF`／`DAILY_BRIEF_TIME`＝0835）：夜盤報酬、盤前市場狀態、R20、典型 σ→平衡型停利停損點數、今天是不是最後交易日；不放 VIX／費半（免費來源 12:16 才有資料）。一天一則（冷卻 key 跨重啟）、09:30 之後不補。
  三者都掛在 `main.py` 的 lifespan（`vol_watch_loop`、`reminder_loop`、`brief_loop`）。提醒之後怎麼做比較好**沒有驗證過**，訊息只列事實；頻率與判定在 update.md 發現 37。
- **換月**（`core/rollover.py`，update.md 發現 38）：worker 把近月（`TMFR1`）解析成實際合約後快取（`_contracts`）、後端只在啟動時訂閱、shioaji 1.5.3 的合約檔只在登入時下載，所以後端跨過結算日不重啟就會一直停在已到期的舊合約（沒報價、下單被拒）。做法：worker 在 `subscribe` 的回覆帶 `target_code`，`broker.resolved_codes` 記著（重連時清掉重填）；`rollover_loop` 每 30 秒檢查「訂閱的合約是否已過最後交易時間（結算日 13:30）」，是的話在 `/api/health` 帶 `restart_wanted=rollover`；watchdog（`run_live.sh`／`run_sim.sh` 的 `rollover` 態）看到就**正常關閉**（TERM，逾時才 -9，worker 才會登出）並重啟，是有計畫的重啟：不算短命、不退避，後端至少活 `ROLLOVER_MIN_UPTIME`（60 秒）、兩次之間隔 `ROLLOVER_MIN_GAP`（300 秒）。**什麼時候試**：R1 何時換月官方文件沒寫，只寫了合約檔更新時間（07:50、08:00、14:45、17:15），所以只在結算日 14:46、17:16、隔天 07:51、08:01 各試一次（試過的時間點與更早的作廢），四次後還是舊的就推播「換月失敗」、放棄；換成功推播一次。狀態存 `data/rollover_state_<mode>.json`。target_code 讀不到（拿到 `TMFR1`）就什麼都不做、只留一行警告。`ROLLOVER_RESTART=false` 關閉。**沒有實測過結算日當天的行為**，2026-10-21 要看 log 與通知確認。
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
  成交列另帶 12 個盤口欄位（`BOOK_COLS`：第一檔買賣價量、五檔總量、第一檔量變化、交易所累計內外盤、均價、現貨），是成交**之後**的盤口；`RECORD_BOOK=false` 關閉，舊資料庫自動補欄位、舊列為 NULL。
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