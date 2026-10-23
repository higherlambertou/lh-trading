# lh-trading 操作手冊

日常操作與第一次上手先看 [QUICKSTART.md](QUICKSTART.md)（含名詞與參數速查）。

## 快速開始

### 啟動正式盤（真錢交易）

```bash
nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &
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
nohup ./run_sim.sh >> /tmp/lh_sim_watchdog.log 2>&1 &
```

檢查狀態：
```bash
curl http://localhost:8003/api/health
```

### 同時啟動兩個

```bash
nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &
nohup ./run_sim.sh >> /tmp/lh_sim_watchdog.log 2>&1 &
```

---

## 信息收集系統（lh-intel，另一個 repo、另一個常駐服務）

籌碼、總經事件、全球市場、新聞的收集**不在這個系統裡**：它是獨立的 repo `~/Documents/GitHub/lh-intel`，用 launchd 常駐（`com.lh.intel`，登入後自動啟動、當掉自動重啟），**不用跟交易後端一起啟停**，交易後端重啟也不會讓它漏資料。
完整的操作手冊（啟動、狀態檢查、排錯、回補、備份還原）在 `~/Documents/GitHub/lh-intel/OPERATION.md`；最常用的：

```bash
cd ~/Documents/GitHub/lh-intel
~/anaconda3/bin/python -m intel status             # 有沒有在跑、各收集器最後一次成功、備份
~/anaconda3/bin/python -m intel restart-service    # 改了程式或 .env 之後
~/anaconda3/bin/python -m intel install-service    # 沒裝或被移除了就裝回去
tail -f data/logs/intel.log
```
- 與交易系統的關係：只有 `lh-intel/data/intel.db` 一個檔（契約見 lh-intel/SCHEMA.md），交易系統**只能唯讀**開。**〈資訊〉分頁**（`core/intel_reader.py`、`GET /api/intel/summary`）用它顯示事件倒數、籌碼、全球市場、新聞與收集器狀態；**目前交易系統沒有任何策略或風控讀它**。
  路徑：`.env` 的 `INTEL_DB`（沒設就用同一層的 `../lh-intel/data/intel.db`）。畫面寫「資訊系統沒有資料：…」＝找不到資料庫、打不開或版本不對（原因會寫在後面）；寫「資訊服務沒有心跳」＝lh-intel 沒在跑，去那邊 `python -m intel status`。
  ⚠ 面板顯示「現在已知的全部」（含事後回補的列）；**拿這些資料做研究、回測、進決策，必須照 lh-intel 的 `intel/read.py` 的 as_of 規則（回補列預設不可見），不能用 `intel_reader.py`**。
- Telegram 共用交易系統的 bot（訊息開頭標 `[intel]`）；它的 `.env` 是自己的（只有 FRED 金鑰、聯絡 email、這兩個 Telegram 鍵，沒有券商憑證）。

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

連線假死偵測的內部狀態（最近一次報價／成功回覆是幾秒前、連續錯誤幾次；update.md 發現 65）：
```bash
curl http://100.127.125.13:8002/api/health/session
```
後端判定假死時，`/api/health` 會回 `"broker_connected":"False","session":"stale","stale_reason":"…"`，app log 有一行 `券商連線假死：…`，
watchdog log 有 `判定券商連線假死(stale) → 正常關閉 PID=… 後重啟`；正式盤會推播「⚠️ 券商連線假死，正在重啟」。
**颱風假／臨時休市日**（發現 76）：日盤開盤後 watchdog 一直 `判定券商連線假死(stale)` 重啟、每次重啟後約 3 分鐘又 stale，多半是這天其實沒開盤（證交所日曆不會事先列出颱風假）。確認：`grep 今天日盤 /tmp/lh_live.log`、app log 的 `券商連線假死：日盤中 … 沒有收到任何報價`、當天 `ticks.db` 08:45 之後沒有任何報價。修好之後的行為：第一次照舊重啟，重啟後 3 分鐘仍沒報價就推播「ℹ️ 今天日盤好像沒開」並停止（`/api/health/session` 的 `closed_guess` 有原因）；如果其實有開盤，手動重啟後端即可。事後記得把當天的盤前快照作廢：`python -m core.premarket_log void 日期 --reason "颱風假，期交所休市"`。
同一種錯誤重複發生時，app log 只記第一次的完整 traceback，之後每 5 分鐘一行「（同樣的錯誤，過去 5 分鐘又發生 n 次…）」，恢復時記「恢復（先前連續失敗 n 次）」。
關閉：`.env` 設 `SESSION_WATCH=false`（重啟後端）。期交所日資料的自動更新：`data/taifex/update_state.json` 是最後成功的日期，app log 搜「期交所資料更新」；關閉 `TAIFEX_UPDATE=false`。

### 系統健康、備份的雲端副本（update.md 發現 66）

〈分析〉頁最上面的〈系統健康〉一次看：券商連線、近 7 天自動重啟、備份與雲端副本、期交所資料更新、lh-intel、磁碟與 log（API：`/api/system/health`）。

**備份的雲端副本（Google Drive）**：
1. 裝「Google Drive 電腦版」並登入，Finder 側邊欄會出現 Google Drive（路徑在 `~/Library/CloudStorage/GoogleDrive-你的帳號/我的雲端硬碟`）。
2. `.env` 加一行 `BACKUP_MIRROR_DIR="/Users/…/Library/CloudStorage/GoogleDrive-你的帳號/我的雲端硬碟/lh-trading-backup"`，重啟後端。
3. 手動跑一次確認：`python -m core.backup`（最後一行會印「雲端副本：ok → …」）。第一次 macOS 可能跳出「要存取 Google Drive 的檔案」，按允許。
之後每個交易日 14:00 本機備份完就複製一份過去（保留 14 天）；Google Drive 沒開時只略過、不影響本機備份，〈系統健康〉會變黃。
備份內容是成交紀錄、IV 歷史、日誌等資料庫；**不含** `.env`、憑證與金鑰。

### 登入自動啟動（launchd，update.md 發現 69）

正式盤 watchdog 和前端已登記成 launchd 服務，**登入 macOS 後自己起來**（重開機、停電之後不用記得手動開）。你原本的操作方式不變：
- 停止：照舊 `kill <watchdog PID>`（正常結束，不會被拉回來）；前端照舊停。手動 `nohup ./run_live.sh …` 也照舊——已經有一個在跑時服務會自己讓開。
- watchdog 本身萬一異常結束（被 kill -9、當掉），launchd 30 秒後重開；前端只在登入時啟動一次。
- 查狀態／移除：`scripts/launchd.sh status`、`scripts/launchd.sh uninstall`（不會停掉正在跑的）；改了 `deploy/launchd/*.plist` 要重新 `scripts/launchd.sh install`。
- log：watchdog 照舊 `/tmp/lh_live_watchdog.log`，前端 `/tmp/lh_frontend.log`。
- **限制**：FileVault 開著、沒有自動登入，所以重開機後要你登入才會起來；半夜自動更新的重開機擋不住——建議關掉「自動安裝 macOS 更新」（系統設定 → 一般 → 軟體更新 → 自動更新）。
- macOS 不讓 launchd 直接用 bash 讀 `~/Documents`，所以服務用 anaconda 的 Python 當啟動器（`scripts/launchd_run.py`），和 lh-intel 一樣。

### 券商 SDK 版本與退回（update.md 發現 68）

正式盤／模擬盤由 watchdog 用專案裡的 `.venv/bin/python` 啟動（shioaji 1.5.8；其他套件沿用 anaconda）。〈系統健康〉的「券商連線」那一行會寫目前的 SDK 版本。
- **退回 1.5.3**：`mv .venv .venv.off`，再重啟 watchdog（沒有 `.venv` 就用 anaconda 本身的 1.5.3）。
- **anaconda 的 1.5.3 壞掉時**：備份在 `data/backup/sdk/shioaji-1.5.3/`，把兩個資料夾複製回 `~/anaconda3/lib/python3.11/site-packages/`。1.5.3 已從 PyPI 下架，pip 裝不回來。
- **之後要再升級**：先 `.venv/bin/python -m pip install shioaji==新版`，用 `.venv/bin/python main_sim.py` 跑模擬盤，實測登入、報價、部位、掛單／取消、市價成交與成交回報，再重啟正式盤的 watchdog。

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

**v2：內外盤快照與趨勢判斷標記**（需求文件 2026-10-16 版，update.md 發現 47；要重啟後端才生效）

- **內外盤快照**：每筆進場（手動、策略都記）多記**委託送出當下**面板上那一組數字——`flow_inside`／`flow_outside`（交易所時段累計內盤／外盤口數）、`flow_net_diff`（外盤−內盤）、`flow_ratio`（外盤占比，就是面板上的比例）、
  `flow_accum_start`（「日盤 08:45 重置」或「夜盤 15:00 重置」，累計值依賴起點，要一起看）、`flow_state_time`（這組數字是什麼時候算的，和 `entry_time` 差太多代表當時的資料是舊的）、`flow_recent`（最近 20／100／300 筆與 5／15／30 分鐘的外盤占比與筆數，JSON）。
  取不到（後端剛啟動、交易所累計內外盤還沒收到）＝空值，原因在 `flow_missing_reason`，**不會是 0**。**功能啟用前下的單沒有這些欄位**（原因會寫明）。
- **趨勢判斷標籤**（手動單才能標）：〈交易〉分頁最下面〈進場判斷標記〉卡，點「多／空／盤整／無明確判斷」（沒看法就選最後一個，不要硬選），備註選填。**不擋也不延遲下單**，進場後任何時候都能補、能改（改＝新增一版，舊的留著）；
  **出場之後才補的會標成「事後標記」**（`label_after_exit`＝true，結果已知，可能被事後記憶影響——分析時要不要排除自己決定）。沒標的是空值（`label_missing_reason`＝`not_entered`）。
  用指令標也可以：`curl -X POST http://100.127.125.13:8002/api/tradelog/entries/<trade_id>/label -H 'Content-Type: application/json' -d '{"trend":"multi","note":"突破前高"}'`（`trend`＝`multi`｜`short`｜`range`｜`none`；策略單回 422）。
- `order_source`＝`manual`｜`strategy`；手動單的 `strategy_name`、`strategy_type` 是空的（不填預設策略）。CSV 多了 `order_source`、`flow_*`、`trend_*`、`label_*` 欄位。
- 只記錄，沒有任何策略或風控會讀這些欄位。樣本門檻與判斷標準請在**看結果之前**先寫下來。

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

### 選擇權試算（買方，唯讀、不下單；update.md 發現 29）

重啟後端（空手時）＋前端重 build 後，在〈分析〉分頁的〈選擇權試算〉使用；也可以直接打 API。**即時模式每次試算向券商查一次整個到期月的鏈**（5 秒內同一個月用快取），不要寫腳本輪詢（worker 單執行緒、行情查詢有額度）。

```bash
H=100.127.125.13:8002
# 即時：ATM 買跨式（delivery_month 用 /api/order/option/expiries 查得到的月份）
curl -s -X POST http://$H/api/options/calc -H 'Content-Type: application/json' \
  -d '{"kind":"straddle","delivery_month":"202610","lots":1,"budget_twd":80000}' | python3 -m json.tool | head -80
# 勒式：兩翼 |Delta| 0.25；或改用預期波動（距離＝k × 預期波動，move_pts 自己填預期波動的點數、留空＝市場隱含 1σ）
curl -s -X POST http://$H/api/options/calc -H 'Content-Type: application/json' \
  -d '{"kind":"strangle","select":"move","k":0.5,"move_pts":800,"delivery_month":"202610"}' | python3 -m json.tool | head -60
# 手動（休市、沒連券商也能用）：自己給遠期價、ATM IV（%）、距到期天數
curl -s -X POST http://$H/api/options/calc -H 'Content-Type: application/json' \
  -d '{"kind":"call","select":"delta","delta":0.4,"manual":{"F":49300,"iv":20.5,"days":9}}' | python3 -m json.tool | head -60
# 平倉試算：已買的各腳（買進價是點）；目前買價／賣價（bid／ask）選填
curl -s -X POST http://$H/api/options/close -H 'Content-Type: application/json' \
  -d '{"legs":[{"right":"C","strike":49300,"qty":1,"entry":740},{"right":"P","strike":49300,"qty":1,"entry":560}],"delivery_month":"202610"}' | python3 -m json.tool | head -80
# 鏈摘要：遠期價、ATM IV、距到期、市場隱含的預期波動，與 ATM 附近的報價
curl -s "http://$H/api/options/chain?delivery_month=202610&rows=5" | python3 -m json.tool | head -60
```

- 報價與遠期價：遠期價與 ATM IV 由鏈上買賣平價反推（和 IV 監控同一條公式），不需要期貨報價；T＝（交割日 13:30 − 現在）／365 天，無風險利率 `IV_RATE`（預設 1.5%）。
- **休市時**（沒有買賣報價）會回 422 並建議改用手動模式；券商沒連線／查詢逾時回 503；找不到那個到期月回 404。
- 買方試算不碰下單與策略。要實際下單還是用〈手動下單〉的選擇權分頁；這裡算出的履約價要自己去那邊填。
- 測試與核對：`python -m pytest tests/test_options_calc.py tests/test_routes_options.py tests/test_frontend_options.py -q`。

### 選擇權的歷史資料與離線檢驗（update.md 發現 30；唯讀、不下單、不連券商）

```bash
python -m core.option_history status          # 歷史近價鏈有幾個月（data/taifex/opt/）
python -m core.option_history fetch           # 補齊缺的月（每月 1 次請求、間隔 1 秒；已結束的月不重抓；失敗的月重跑即可）
python -m core.option_backtest sanity         # 只看資料處理有沒有對（週期數、進場距到期、報價可用、IV 與 TX 對帳），不算報酬
python -m core.option_backtest run            # 7 個事先登記的檢定（買 ATM 跨式：出場規則 X0~X3、進場訊號 S1~S3）
python -m core.option_spread_study run        # 歷史收盤的買賣價差（時段 × 月/週 × 離平值距離 × 年份）
python -m core.hedge_sim run                  # 用台指期 1 分 K 當微台，對沖跨式的 Delta 的歷史模擬（需要 data/market_state.db 的 TXF 1 分 K）
python -m core.option_spread_log report       # 盤中取樣的價差（要先讓後端跑一陣子；OPTION_SPREAD_LOG=true、預設每 10 分鐘）
```

- 這些都是**離線分析**，不需要重啟後端；只有 `option_spread_log` 的取樣迴圈要**重啟後端**才會開始記（每次取樣向券商查一次整條鏈，排在 worker 佇列上；`OPTION_SPREAD_MINUTES` 調間隔，`OPTION_SPREAD_LOG=false` 關掉）。盤中價差無法回補，早開早累積。
- **請求要節制**：期交所網站每月 1 次、間隔 1 秒，下載到錯誤頁會記失敗不存檔；不要寫迴圈猛打。
- **判讀要小心**：每個檢定只有約 200 個（回測）或 77 個（對沖）月度週期，報告都附最小可偵測效果（MDE）；沒過關不等於證明沒有。收盤的最後最佳買賣價近年很寬（失真），出場被機械性地看成不利，所以另有『成交價 = 結算價 ± 1.5%』的敏感度。
- 測試與核對：`python -m pytest tests/test_option_history.py tests/test_option_backtest.py tests/test_option_spread_study.py tests/test_option_spread_log.py tests/test_hedge_sim.py -q`。

### 盤前跨市場資訊紀錄（只記錄、不影響交易；update.md 發現 31）

**重啟後端（空手時）才開始記**；之後每個台股交易日 08:30（`PREMARKET_TIME`）自動一筆，來源失敗的欄位 15 分鐘內每 2 分鐘重試，過了就帶著 null 寫入；後端 08:30 之後才啟動最晚補記到 13:45（資料仍只用 T 之前的，標 `late_seconds`）；**13:45 之後才啟動，當天就錯過、不補**（WARNING log＋`premarket_errors` 的 `missed`，不算失敗警示；`/status` 的 `missed_dates` 與面板會列出）。週末與國定假日重啟不會誤報（台股日曆先於時間窗檢查）。

```bash
python -m core.premarket_log show              # 今天會記什麼（只算、不寫；會真的去抓 Nasdaq／Cboe／證交所各一次）
python -m core.premarket_log run               # 現在記（只在 T ~ 13:45 之間、休市日不記；已經有了就不動）
python -m core.premarket_log status            # 有沒有啟用、記了幾筆、今天記了沒、失敗警示
python -m core.premarket_log export > premarket.csv      # CSV（UTF-8 含 BOM、時間一律台北 +08:00、缺值留空）
python -m core.premarket_log void 2026-10-14 --reason "颱風假，期交所休市"    # 事後作廢（新增一筆，不改原紀錄）
python -m core.premarket_log correct 2026-10-14 --patch '{"sox": {"value": 0.01}}' --reason "…"   # 更正（新增一筆指向原紀錄）
curl -s http://$H:8002/api/premarket/status | python3 -m json.tool
curl -s "http://$H:8002/api/premarket/preview" | python3 -m json.tool | head -60     # 如果現在記會長什麼樣（不寫入；?date=YYYY-MM-DD 指定那天）
curl -s "http://$H:8002/api/premarket/snapshots?days=30" | python3 -m json.tool | head -60
curl -s http://$H:8002/api/premarket/snapshots.csv -o premarket.csv
```

- **事後補值**（update.md 發現 35）：Nasdaq 公開 API 與 Cboe CSV 要到 08:46 之後才有前一個美股交易日的資料，所以快照常常先帶「資料還沒更新」的空值寫入（T＋15 分鐘）。後端每 10 分鐘掃最近 5 天，來源有了就補一筆 correction（只補那幾欄；原始那筆不動；畫面標「已補值」、來源旁寫「補值於 時間」；`detail.backfilled_at` 記實際補到的時間）。
  休市、異常值、本機沒有夜盤成交造成的空值補不回來，不補。沒重啟後端就沒有自動補：`python -m core.premarket_log backfill [日期]` 手動補。**要排除「事後補的」做分析**，用 `detail.backfilled` 或 `corrected_at` 篩。
- **畫面**：〈市場〉分頁最下方〈盤前跨市場快照〉（最新紀錄、預覽、最近 12 筆、CSV 下載；FRONTEND.md）。前端要 `npm run build` 才看得到；後端要重啟才有 `/preview` 與 `status` 的 `next_snapshot`／`missed_dates`（舊版後端畫面會寫「後端還沒有這個 API」）。**預覽每次會去抓外部來源（60 秒快取），不要寫腳本輪詢**。
- 欄位：`txf_night`（夜盤收盤 ÷ 前一個交易日日盤收盤 − 1，本機 `ticks.db`）、`sox`／`tsm`（最近一個已收盤的美股交易日的對數報酬）、`vix`（收盤值；`detail.rel20`＝相對近 20 日平均）。每個欄位都有 `value`、`source`、`source_timestamp`（資料本身的時間，UTC）、`fetched_at`、`missing_reason`、`flag`、`detail`。
- **null 不是 bug**：美股休市日隔天（`us_closed: …`）、來源失敗（`source_error: …`）、資料還沒更新（`stale_or_missing`）、後端那段時間沒開（`no_ticks`／`incomplete_session`）、異常值（`out_of_range`，原始數字在 `detail.raw_value`）。**不會用前一天的值或預設值補**。
- **來源**：預設 Nasdaq 公開 API（TSM、SOX）＋Cboe 官方 CSV（VIX）；Yahoo 這台機器回 429，要試的話 `PREMARKET_SOURCES=yahoo,nasdaq,cboe`（失敗自動換下一個）。都是免費公開端點、不是契約授權，只存檔供個人研究；改版、限流、被擋時欄位會是 null。
- **歷史預檢**（不連券商、不需要重啟）：`python -m core.premarket_study fetch`（下載日線，每個來源一次請求）→ `sanity`（只看資料處理）→ `run`（4 個事先登記的檢定；墳場紀錄寫進 `premarket_graveyard`）。判定標準在跑之前寫在 update.md 發現 31、只跑一次，**不要為了湊出結果回頭改設計**。
- 測試：`python -m pytest tests/test_premarket_log.py tests/test_premarket_study.py -q`。

### 持倉保護（手動停損落地與重啟恢復、凍結與重啟的手機通知；update.md 發現 18、32）

**需重啟後端才生效**（空手時重啟）。要手機通知就在 `.env` 設定通道（見 `.env.example`；不設定也行，只是少了推播，畫面橫幅與 log 照常）。

```bash
python -m core.notify status        # 目前設定了哪些通道（不顯示 token）
python -m core.notify chat-id       # Telegram：列出對你的 bot 傳過訊息的聊天室 id（不會送訊息；token 讀 .env，不會印出來）
python -m core.notify test          # 送一則測試訊息；收到才算設定成功（結束碼 0＝都成功、1＝有通道失敗、2＝沒設定）
python -m core.notify send "標題" "內容" [--key K --cooldown 600]
python -m core.notify watchdog --event freeze --detail down --mode live   # watchdog 內部用；手動跑可以看它會不會推播
```

- **手動停損停利落地**：登記、修改、移除都寫進 `data/manual_watches_live.json`（模擬盤 `_sim`；原子寫入）。後端（重新）啟動、券商連上後**對帳一次**再恢復：
  同一個部位（合約代碼＋方向）的口數由監看依建立先後分配、不超過部位口數；**部位已經不在的舊監看直接丟掉**（不會把舊停損掛到你重啟期間手動開的新部位上）；
  限價開倉單還掛在券商的，留著等成交。券商一直連不上（等 10 分鐘）就原封不動保留檔案、推播通知。log 找「手動停損停利監控 [id] 已恢復／不恢復」。
  **限制**：恢復的是你當初設的停損停利點數與進場價；重啟期間你若在券商 App 動過這個部位（加減碼、改價），要自己確認停損價（推播訊息會列出恢復的停損價）。
  `MANUAL_WATCH_PERSIST=false` 關閉落地、`MANUAL_WATCH_STATE` 指定檔案。
- **策略不會自動恢復**（自動重啟會下真單，我們沒做）：重啟後有微台持倉、沒有手動停損監控 → 視為沒有保護。
- **沒有停損保護的持倉**：〈部位〉面板標題下方的黃色橫幅（`GET /api/position/meta` 的 `unprotected`；沒有策略在跑、也沒有「停損點數 > 0 且已看到部位」的手動監看覆蓋的口數）。只有停利、沒有停損的監看不算保護。
- **推播什麼時候送**：① 後端啟動、對帳完、**有持倉**時一則報告（恢復了什麼和停損價，或哪些持倉沒有保護）；② 連不上券商、停損監控沒恢復；
  ③ watchdog 判定凍結／斷線／進程消失、要重啟之前，**且最後已知有持倉**（後端每 5 秒把持倉寫進 `data/position_state_live.json`，watchdog 讀它，不必問已凍結的後端）。
  沒有持倉就不打擾；同樣的事 10 分鐘內只推一次（冷卻記在 `data/notify_cooldown.json`，跨進程有效）；推播失敗會把冷卻還回去、下一次事件再試。
  `run_live.sh`／`run_live.ps1` 都有掛（`.ps1` 沒在 Windows 上實測）；模擬盤不推播。
- **設定 Telegram（建議）**，用跑後端的那個 Python（`/Users/lambert/anaconda3/bin/python3.11`），在 repo 根目錄：
  ① @BotFather 建立 bot，拿到 token；② 在 Telegram **先對你的 bot 傳一則訊息**（隨便，例如 `/start`——bot 不能主動找你）；③ 把 `NOTIFY_TELEGRAM_TOKEN=…` 寫進 `.env`（**token 只放 `.env`，不要貼到聊天或 commit**）；
  ④ `python -m core.notify chat-id` → 印出聊天室 id，把 `NOTIFY_TELEGRAM_CHAT_ID=…` 也寫進 `.env`；⑤ `python -m core.notify test` → 手機收到「推播測試」才算成功。
  **不用重啟**：watchdog 每次呼叫都重新讀 `.env`；後端下次（重新）啟動時讀到設定，之後的啟動報告就會推播。
- **現在會通知什麼**（全部只通知、不動委託；`python -m core.notify test` 測通道）：

  | 事件 | 什麼時候 | 優先序 |
  |---|---|---|
  | 後端啟動報告 | 重啟、對帳完、**有持倉**：恢復了哪些停損（含停損價）／哪些持倉沒保護 | 有沒保護的＝緊急 |
  | 連不上券商 | 重啟後 10 分鐘仍連不上、停損監控沒恢復 | 緊急 |
  | 凍結／斷線／進程消失 | watchdog 要重啟之前、且最後已知有持倉 | 緊急 |
  | 🛑停損／🎯停利觸發 | 手動停損停利監控送出第一張平倉單（進場價、現價、點數） | 停損＝緊急 |
  | ✅平倉完成 | 平倉單全數成交、監看移除 | 一般 |
  | ⚠️平倉單沒成交 | 平倉單從第 3 次起每次重送 | 一般 |
  | 🚨停損平不掉 | 平倉單送了 8 次仍未平倉、系統放棄自動重送 | 緊急 |
  | 保證金風險 | 券商回報追繳（緊急）；權益總值 ÷ 維持保證金 < 120%（緊急）、< 150%（警告）；回到安全範圍推播「已解除」；沒有持倉不判斷 | 警告／緊急 |
  | ✅成交 | 手動單成交（`NOTIFY_FILLS=manual` 預設；`all` 連策略；`off` 關閉）；停損停利的平倉單用上面的訊息、不重複 | 一般 |
  | ⏰當沖倉位還沒平 | 13:15、13:25（週一～五）：勾了當沖的倉位還開著；期貨商通常在收盤前約 15 分鐘強制沖銷，以期貨商規定為準 | 緊急 |
  | ⚠️波動放大 | 連續交易時段內（日盤、夜盤，開始後 30 分鐘不判斷），最近 30 分鐘 σ ≥ 同時段近 20 個時段中位數的 2 倍（`VOL_ALERT_RATIO`）；冷卻 10 分鐘（`VOL_ALERT_COOLDOWN_MIN`；日盤平均每個日盤 1.2 則、極端行情日會連發十幾則）；**空手也發**（發現 39）：講現在的雜訊範圍與平衡型停利停損點數（平常是多少）；有期貨持倉就多帶「離停損還有幾點＝雜訊範圍的幾倍、被掃到的機率」 | 一般 |
  | 📅合約到期 | 持有的期貨／月選擇權：最後交易日（第三個週三，休市順延）前 1～3 天 09:00 後、當天 08:50 後、當天 12:30 後（還剩多久，緊急）各一則 | 一般／緊急 |
  | 📅今晚 21:30 美國 CPI／非農、明天凌晨 FOMC | 三種事件**公布前提醒一次，不管有沒有持倉**（事件的美國當地日期當天台北 12:30 起；FOMC 前一天 12:30 起）。有會留到晚上的持倉（不含當沖）就列出來、優先順序較高；先推沒持倉的版本、之後才開倉會再補一則「你現在有持倉」（重啟後不補）。內容只講事件日分析（發現 50）的事實：**CPI**＝後第一個日盤開盤跳空約平常 1.6 倍（中位數 154 點 vs 100 點）、日盤振幅沒放大；**非農**＝沒有確認會放大；**FOMC**＝日盤振幅沒放大、跳空 2021～2024 年約 1.6～2.4 倍但 2025 年起 1.05～1.18 倍，沒確認；都不是方向預測。`EVENT_REMINDER=false` 關閉；事件時間讀 lh-intel（10 分鐘快取），讀不到就不提醒 | 一般（有持倉＝高） |
  | 📋盤前摘要 | 交易日 08:35（`DAILY_BRIEF_TIME`）一則：夜盤報酬、市場狀態、R20、典型 σ 與平衡型停利停損點數；是尺度、不是進場訊號 | 一般 |

  設定（`.env`）：`NOTIFY_FILLS`、`NOTIFY_MARGIN_WARN`（預設 150）、`NOTIFY_MARGIN_URGENT`（預設 120）、`NOTIFY_SIM`（預設不在模擬盤推播）、`VOL_WATCH`／`VOL_ALERT_RATIO`（預設 2.0）、`DAILY_BRIEF`／`DAILY_BRIEF_TIME`（預設 0835）。同一件事有冷卻（警告 30 分鐘、緊急 10 分鐘、其他 1 小時），推播失敗會把冷卻還回去。
  **限制**：當沖提醒認得「手動下單勾了當沖、後端之後沒重啟」或「有掛停損停利的當沖部位」；券商的持倉資料不帶倉別。你自己在券商 App 平掉部位時，不會有「平倉完成」。
  **指標類通知的限制**（update.md 發現 37）：波動放大只看期貨、夜盤基準只有累積中的夜盤時段（同一分鐘不到 10 個時段就不判斷）、時段開始後 30 分鐘內算不出；到期提醒不認得週選擇權的代碼；**換月靠後端偵測＋watchdog 重啟**（見下面〈換月〉）；沒有 watchdog 的話結算後要自己重啟後端。
  **測試不能碰真實通道**：`tests/conftest.py` 設 `NOTIFY_SKIP_DOTENV=1`、把 `core.notify._http_post`／`_http_get` 換成會直接失敗的替身（update.md 發現 34 的事故）；要測真函式用 `nt.REAL_HTTP_POST`。
- **通道**：Telegram bot（私人、免費，建議）、ntfy（主題名稱就是密碼：用一長串隨機字或自架）、自訂 webhook。訊息只放合約、方向、口數與停損點數，不放帳號與權益；token／網址不會出現在 log。
  **推播是把這些資訊送到第三方服務**，通道由你自己設定，沒設定就不會送。
- 測試：`python -m pytest tests/test_notify.py tests/test_protection.py tests/test_manual_restore.py tests/test_startup_protection.py tests/test_frontend_protection.py -q`。

### 機率範圍帶（只顯示、不碰下單；update.md 發現 48）

**需重啟後端＋前端 build**。回答「現在的價格，15 分鐘後有 80% 的機率落在哪裡」：以最新成交價為中心、用最近 30 分鐘的 σ（和〈建議停損停利〉同一個）乘上歷史實測的倍數，不是模擬。
- **兩種範圍**：**落點**＝時間到時價格落在這個範圍內的機率（50／80／95%）；**期間**＝這段時間內最高、最低都沒超出這個範圍的機率（停損停利碰不碰到看這個，比落點寬）。時間有 5／15／30／60 分鐘，日盤再加**到收盤**（剩 15~270 分鐘才有）。
- **在哪看**：〈交易〉分頁〈建議停損停利〉卡裡的〈機率範圍〉（跟著選的持有時間）；走勢圖的〈範圍帶〉按鈕（預設關，開了可選時間與落點／期間，圖上畫 50／80／95% 的上下線）。
- **怎麼讀「⚠不穩」與「?」**：這一格在樣本外驗證沒過（實際涵蓋率和名目差超過 3 個百分點），或前後半的倍數差超過 10%——範圍可能偏窄，要多留一點。目前沒過的是到收盤剩 15~60 分的**期間** 50%／80%。
- **已知的偏差**：接近收盤（約 12:15 之後）用最近 30 分鐘 σ 外推會偏窄（80% 範圍實際只涵蓋 73~79%）；夜盤只有 43 晚的實測，一律標「樣本少」。
- **只說會走多遠，不說往哪個方向**；沒有驗證過「衝出範圍就順勢」有沒有用，不要當進場訊號。
- `curl http://100.127.125.13:8002/api/market/range | python3 -m json.tool`：一次回五組；成交太少（最近 30 分鐘有效變動 < 25）或沒有成交價時只回說明（`note`），不給數字；`price_age_sec`／`stale` 看最新成交價有多舊。
- 表是 `core/range_calibration.json`（只讀；**不要為了數字好看重算**，重算先在 update.md 重新登記標準再跑 `python -m core.range_calibration run --write`）。

### 依波動的建議停損停利（只顯示、不碰下單；update.md 發現 33）

**需重啟後端＋前端 build**。〈委託〉面板（期貨模式）的停損停利欄下方有〈建議停損停利（依波動）〉卡：選持有時間（5／15／30／60 分鐘），它用**最近 30 分鐘 1 分鐘收盤價變動的標準差 σ**（點／分鐘）算雜訊範圍 σ√T，給三組型態：
高勝率型（停利 0.5σ√T／停損 2σ√T）、平衡（1／1.5）、高賺賠比型（1.5／1）——三組的停利＋停損一樣，所以「要比沒有技巧的勝率多贏幾個百分點才打平成本」（＝成本÷(停利＋停損)）一樣：**改比例只是把勝率換成賺賠比，期望值不變**。
每組還有停損被雜訊掃到的機率（`core/stop_calibration.json` 的實測表）、單口停損虧多少（元、占權益 %）、單筆風險限額下最多幾口、（日盤 09:15 之後）停利是否比今天預期剩餘振幅的上緣還遠。「帶入」會把停損停利填進上面的欄位（還要自己按下單）。

```bash
curl -s "http://$H:8002/api/market/suggest?contract=TMF&horizon=15" | python3 -m json.tool | head -60   # contract：TMF／MXF／TXF；horizon：5／15／30／60
python -m core.stop_calibration check-source    # TMF 真實成交的 σ 與 TXF 1 分 K 的 σ 一致嗎（中位數要在 0.9～1.1）
python -m core.stop_calibration run             # 重算校準表並印報告（不加 --write 不會覆蓋 JSON）
```
- **〈口數提示〉**（下單面板，口數／停損欄位下方；update.md 發現 51；`GET /api/risk/size?contract=TMF&stop_pts=30&qty=2`）：填了停損點數就顯示「最多幾口」與哪個上限在管（單筆風險 `SUGGEST_RISK_PCT`、總風險餘額 `SIZE_TOTAL_RISK_PCT`、口數上限 `SIZE_LOT_CAP`），以及你填的口數的風險是權益的幾 %。每口風險含滑價緩衝（`SIZE_SLIP_PTS`，預設 2 點）。
  **只提醒、不擋單**。已佔用風險只算手動停損監控裡有停損的期貨部位；策略單、沒設停損的部位、選擇權不計入，畫面會列出來（此時總風險只是下限）。權益取不到或超過 5 分鐘沒更新、停損沒填＝不給口數。算出不足 1 口就是 0 口、不補到 1 口，並寫要多近的停損或多少權益才夠。後端沒重啟會寫「後端還沒有這個 API」。
- **參數**（`.env`，見 `.env.example`）：`SUGGEST_COST_PTS`（每筆來回成本，預設 4 點是**假設**，請改成自己的手續費＋稅＋滑價）、`SUGGEST_RISK_PCT`（單筆風險占權益上限，預設 1）。
- **資料不夠就不給建議**：最近 30 分鐘有效的 1 分鐘變動少於 25 個（成交太少、休市）會寫原因、不給數字。σ 來自 `flow_1m`（TMF 真實成交的 1 分 K，後端記錄；重啟後端期間沒記到的分鐘就是缺值）。**重啟後要等缺口滑出 30 分鐘窗口（約 30 分鐘）才算得出來**，波動放大提醒也一樣；這種短缺口 `flow-fetch` 不會補。
- **校準表是一次性的測量**：`core/stop_calibration.json` 是事先登記（update.md 發現 33）、跑一次的結果，**不要為了讓數字好看重算**；要重算（例如累積更多夜盤）請先重新登記標準。夜盤只有約 39 晚，一律標「樣本少」。
- **這不是進場訊號**：進場沒有資訊時每筆期望值＝−成本，與停損停利怎麼放無關；高勝率型只是用大的風險換小的獲利。**在目前的波動下，把停損放在雜訊外，1 口微台就是權益的 1～2%**（卡片會直接寫「1 口就超過 1% 風險」）。
- 測試：`python -m pytest tests/test_stop_calibration.py tests/test_stop_suggest.py tests/test_frontend_stop_suggest.py -q`。

### 影子訊號（「成交量放大＋突然漲跌 → 順勢」只記錄、不下單；update.md 發現 43）

**這是什麼**：你的規則——最近 5 分鐘內漲跌 ≥ 30／40／50 點、而且成交量是平常的 2 倍以上 → 順著方向。後端在微台即時資料上持續偵測，**只記錄與通知，不下單**；每個訊號之後 60 分鐘的 1 分 K 都存下來，累積成可以事後分析的前向資料（`data/signals.db`，補不回來，有備份）。
**歷史結果**：日盤 1,611 天的檢定 **0／9 過關**——毛報酬約 0（沒有延續也沒有反轉），扣成本 4 點後平均淨 ≈ −4 點；**加上停損停利（停利 30／50／80 × 停損 20／30／50，27 個檢定）也 0 個過關**：勝率隨停損停利的比例變（停利 30／停損 50 約 62%、停利 80／停損 20 約 23%），但毛報酬都是 0，期望值還是 −成本。所以通知裡每一則都會寫「歷史上沒有優勢」；它的用途是累積夜盤與之後加條件用的資料，不是進場建議。
**怎麼看**
```bash
python -m core.signal_shadow report        # 各變體、日盤／夜盤、持有 5／15／30 分鐘的累積成績（淨報酬已扣成本）
python -m core.signal_shadow recent 20     # 最近 20 個訊號
python -m core.signal_study run            # 歷史檢定的結果（只跑過一次，這裡是印出已存的結果）
python -m core.signal_study bracket        # 同一個訊號加上停損停利的歷史檢定結果（發現 44，27 個檢定 0 個過關，同樣只跑過一次）
```
也可以呼叫 `GET /api/signals/shadow`（成績與最近的訊號）。
**通知**：預設只發 50 點那個變體（日盤平均每個日盤約 2.6 則；夜盤還沒量過）；`SHADOW_NOTIFY=30,40,50` 全發、`off` 關通知（照樣記錄）。訊號那一分鐘收完 5 分鐘內才通知，重啟後補記的舊訊號不通知。
**設定（`.env`）**：`SIGNAL_SHADOW`（false＝關閉）、`SHADOW_NOTIFY`、`SHADOW_COST_PTS`（預設 4）。
**注意**：重啟後端後前 30 分鐘不會有訊號（量基準要重建、窗口要湊滿）；後端沒開著的分鐘沒有資料，那段時間的訊號會漏掉。樣本少的時候（幾十個）看到的賺賠都只是雜訊，別據此下結論。

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
nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &
```

### 完全清理 + 重啟兩個

```bash
pkill -f 'run_(live|sim).sh'
rm -rf /tmp/lh_live_watchdog.lock.d /tmp/lh_sim_watchdog.lock.d /tmp/lh_live.log /tmp/lh_sim.log
nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &
nohup ./run_sim.sh >> /tmp/lh_sim_watchdog.log 2>&1 &
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

### ⚠️ Tailscale 位址不見了（state=noiface／log 出現 `Errno 49 can't assign requested address`）

**現象**：dashboard 突然連不上、後端「自己關了」；`/tmp/lh_live.log` 出現 `[Errno 49] error while attempting to bind on address ('100.x.x.x', 8002): can't assign requested address`，或 `OSError: [Errno 49]`（websocket）。
**原因**：後端只綁 Tailscale 的位址（`BIND_HOST`）；Tailscale 斷線、重新連線、睡眠喚醒時，這個位址會暫時從本機消失。**不是後端壞了。**（2026-10-14 17:38 發生：舊版 watchdog 把沒壞的後端當成凍結殺掉，重啟又綁不上位址，退避到每 10 分鐘試一次，那段時間沒有記錄 tick／外內盤／選擇權價差。）
**現在的行為**（`run_live.sh`，update.md 發現 36）：位址不在 → 不判定故障、不殺後端；後端還在跑就讓它跑（資料照記、停損照看），起不來就先不啟動、每 5 秒看位址回來沒；推播一次「Tailscale 位址不見了」（Telegram；冷卻 30 分鐘）。
**你要做**：把 Tailscale 連回去（選單列的 Tailscale 圖示→Connect；或 `tailscale up`）。檢查：`ifconfig | grep 'inet 100\.'`、`tailscale status`。位址回來後 watchdog 自己會繼續，不用重啟。
**舊版 watchdog 還在跑的話**（2026-10-14 18:33 之前啟動的）：它在最後一檔 10 分鐘退避的 `sleep 600` 時**收不到結束訊號**——`kill`／`pkill -f run_live.sh` 要等睡完才生效，這段時間單例鎖還在、重開會被擋下來（2026-10-14 實際發生：結束訊號拖到 18:23:53 才處理）。要立刻起來：先看 `/tmp/lh_live_watchdog.log` 最後一行是『退避等待』（代表沒有後端在跑），`kill -9 <watchdog pid>`（`pgrep -f run_live.sh`），再 `nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &`（鎖是陳舊的，新腳本會接管）。新版（等待改用 `isleep`）的 `kill`／`pkill` 兩秒內就會結束。**新版 `run_live.sh` 要重啟 watchdog 才生效。**
**資料缺口**：後端沒在跑的那段時間，tick 與選擇權價差補不回來；外／內盤分鐘統計可以用券商回補：`python -m core.indicator_history flow-fetch --days 3`（登入前會先檢查連線數）。**只補「整天」缺的日子**：日盤已有 ≥200 分鐘就算這天有了（`flow_days`），幾分鐘到十幾分鐘的缺口不會補，會回「沒有需要補的日子」（2026-10-15 實測）。持倉：停損停利監控在後端停著的時候不運作——有持倉時看 Telegram 警示。

### 🔄 換月（近月合約結算後自動重啟，update.md 發現 38）

**為什麼**：後端只在啟動時把「近月」解析成實際合約並訂閱（shioaji 1.5.3 的合約檔也只在登入時下載）。跨過結算日不重啟，就停在已到期的舊合約：沒有報價、下單被拒。
**做法**：後端每 30 秒檢查訂閱的合約是不是已經過了最後交易時間（結算日 13:30）；是的話 `/api/health` 會多一個 `"restart_wanted":"rollover"`，watchdog 看到就正常關閉並重啟（重新登入＝重新下載合約檔）。
**什麼時候試**：官方文件沒寫近月何時換，只寫了合約檔每天的更新時間（07:50、08:00、14:45、17:15）。所以只在**結算日 14:46、17:16、隔天 07:51、08:01** 各試一次；四次後還是舊的就推播「🚨 換月失敗」並放棄。換成功會推播「✅ 換月完成」。
**你會看到**：Telegram 的「🔄 換月重啟：TMFJ6 已結算（第 1/4 次）」→ 約 1～2 分鐘後「✅ 換月完成」；watchdog log 有「換月：後端要求重啟…」；app log 有「近月合約解析：TMF → TMFK6」。
**沒換成功**：先看 app log 的「近月合約解析」行——如果寫的是 `TMFR1`（讀不到 `target_code`），自動換月不會動，請在結算後手動重啟（`kill <watchdog pid>` 後重開）。狀態在 `data/rollover_state_live.json`（刪掉＝從頭來）。
**關閉**：`ROLLOVER_RESTART=false`。**注意**：這是有計畫的重啟——策略不會自動回來、手動停損監控重啟後會和券商部位對帳恢復；結算後舊合約的持倉已被期交所結算，不會再有舊合約的持倉。
**沒實測**：結算當天的實際行為（R1 在哪個更新時間點換掉）沒有實測過；2026-10-21 要看 log 與通知確認。

### 🛑 後端突然停了（先看 watchdog log 是誰關的）

**看哪裡**：`tail -30 /tmp/lh_live_watchdog.log`，再對照 `/tmp/lh_live.log` 最後幾行。

| watchdog log 的那一行 | 意思 | 怎麼辦 |
|---|---|---|
| `收到結束訊號，關閉正式盤` | 有人對 watchdog 送了 TERM／INT：`pkill -f run_live.sh`、終端機按 Ctrl+C、關掉啟動它的終端機。app log 會有 `Shutting down`、`Worker 已登出`（正常關閉）。**不是當機、也不是記憶體殺的**。 | watchdog 結束後不會自己回來，要的話重新啟動（見〈啟動正式盤〉） |
| `判定故障(down／nobroker) → kill -9 … 並重啟` | 凍結或斷線，watchdog 自己重啟了 | 見下面〈服務凍結〉〈無法連線券商〉 |
| `進程已不存在（登入失敗或自行結束），重啟` | 後端自己結束，或被系統強制殺掉（記憶體不足時 macOS 用 SIGKILL，不會留下關閉訊息） | 看 app log 最後幾行；再看下一節的記憶體檢查 |
| `換月：後端要求重啟` | 近月合約結算後的有計畫重啟 | 見〈換月〉 |
| `BIND_HOST=… 不在本機` | Tailscale 位址不見 | 見〈Tailscale 位址不見了〉 |

**口訣**：log 有「收到結束訊號」＋「Worker 已登出」＝人關的；log 突然斷在中間、沒有關閉訊息＝被殺（記憶體或 kill -9）。
**停的時候有沒有風險**：`data/position_state_live.json` 是後端每 5 秒寫的最後一次持倉（`positions`、`unprotected`），空手就是空的；`data/manual_watches_live.json` 是落地的停損監看。
（2026-10-15 17:06 的實例：log 是「收到結束訊號」＋正常關閉，Terminal.app 13 秒前有使用者操作；當時 Docker 的虛擬機吃光記憶體，但那次不是記憶體造成的。）

### 🐢 機器很慢／記憶體吃緊（Docker、Chrome、VS Code）

**為什麼會影響交易系統**：Docker Desktop 的虛擬機、Chrome、VS Code 和後端共用同一台 Mac 的記憶體、CPU 與 SSD。記憶體不夠時，macOS 會把後端的頁面壓縮、換到磁碟；event loop 一被拖住，`/health` 就逾時，watchdog 會當成凍結 kill -9 並重新登入券商。
（2026-10-15 實例：Docker VM 吃到 23 GB、swap 96%、系統剩餘記憶體 35%；調小後降到 2.5 GB、swap 86%、剩餘 58%。）

**檢查（都是唯讀）**
```bash
memory_pressure | tail -1                                  # System-wide memory free percentage（經驗值：低於 20% 就吃緊）
sysctl vm.swapusage                                        # swap 用了多少（接近 total＝快沒了）
top -l 1 -o cmprs -n 10 -stats pid,command,mem,cmprs,cpu   # 誰被壓縮／換出最多（Docker 的虛擬機是 qemu-system-aarch64）
ps -axo pid,rss,etime,comm | sort -k2 -nr | head -15       # 誰的實體記憶體最大
```
**常見元兇與處理**
- **Docker Desktop**：Settings → Resources → Memory 設上限（例如 4～6 GB），用完就關。它的**虛擬磁碟**滿了（例如 Postgres 塞滿）與 Mac 的磁碟無關（`df -h /` 看的才是 Mac）、不影響後端；但同一個 VM 的**記憶體**會。
- **Chrome 的「Helper (Renderer)」很多個**：一個分頁（甚至一個網站、iframe）一個行程。Chrome 按 Shift+Esc 看哪個分頁／擴充功能最肥；設定 → 效能 → 開「記憶體節省模式」。
- **VS Code 的 plugin helper**：關掉不用的視窗與擴充功能。
**做法**：swap 持續超過八成就先關東西、再開交易系統；已經在跑的話，留意 watchdog log 有沒有出現 `down`。

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
- ⚠ 持倉的保護只恢復一半（update.md 發現 18、32）：**手動停損停利監控會落地、重啟後和券商部位對帳自動恢復**；**策略不會自動啟動**。
  當時若有部位，服務回來後第一件事是看〈部位〉（有黃色「沒有停損保護」橫幅就要處理），再重新啟動策略（會接管部位並套用它的停損停利）或手動平倉；設定好推播通道的話，凍結與重啟時手機會收到通知（見下面〈持倉保護〉）
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
nohup ./run_live.sh >> /tmp/lh_live_watchdog.log 2>&1 &
nohup ./run_sim.sh >> /tmp/lh_sim_watchdog.log 2>&1 &
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
