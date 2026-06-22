# lh-trading 前端操作手冊

## 快速開始

### 開發模式啟動（推薦）

```bash
cd frontend
npm run dev
```

訪問：http://localhost:3002

特點：
- 熱更新（改程式碼自動重新載入）
- 完整的錯誤提示
- 開發者工具可用

### 正式部署

```bash
cd frontend
npm run build
npm run start
```

訪問：http://100.127.125.13:3002

---

## 環境設定

### `.env.local` 設定（重要）

複製 `frontend/.env.example` 為 `frontend/.env.local`：

```bash
cp frontend/.env.example frontend/.env.local
```

編輯 `frontend/.env.local`，改成你的後端位址：

```
# 正式盤後端
NEXT_PUBLIC_API_URL=http://100.127.125.13:8002/api

# 模擬盤後端
NEXT_PUBLIC_SIM_URL=http://100.127.125.13:8003/api
```

⚠️ **重要**：改完 `.env.local` 後**一定要重啟前端**，新的設定才會烤進 JS bundle。

### 跨裝置訪問（手機、平板）

如果要用同一個 Tailscale tailnet 的其他裝置訪問（比如手機看 Dashboard）：

1. 把 `NEXT_PUBLIC_API_URL` 改成你的 Tailscale IP：
   ```
   NEXT_PUBLIC_API_URL=http://100.127.125.13:8002/api
   NEXT_PUBLIC_SIM_URL=http://100.127.125.13:8003/api
   ```

2. 前端開發 server 也要對外監聽：
   ```bash
   npm run dev -- -H 0.0.0.0
   ```

3. 用你的 Tailscale IP 訪問：
   ```
   http://100.127.125.13:3002
   ```

4. **記住：改完 `.env.local` 要重啟前端**，否則手機看到的還是舊位址。

---

## 功能使用

### 儀表板（Dashboard）

進入 http://localhost:3002 看到的首頁。

#### 狀態指示燈（右上角）

- 🟢 **綠色**：後端正常連線、報價活躍
- 🔴 **紅色**：後端離線或凍結
- 每 5 秒自動更新一次

#### 行情條（最上方）

- 顯示 TMF/MXF/TXF 最新報價（台指、小台、大台）
- WebSocket 實時更新（每個 tick）
- 點擊可切換顯示哪些合約

### 選擇交易模式（左上角按鈕）

**模擬盤**（藍色）：
- 不真實下單
- 用 port 8003 的後端
- 測試策略和邏輯用

**正式盤・真錢**（紅色閃爍警告）：
- ⚠️ 真實下單，真錢扣除
- 用 port 8002 的後端
- 任何操作都是實盤交易

### 策略管理面板（Strategy Panel）

**開啟策略**：
1. 選擇策略名稱（orb、vwap_revert、scalp 等）
2. 填入參數（可選，不填用預設值）
3. 點「啟動」

**參數說明**（每個策略不同）：
- `atr_trail_mult`：ATR 移動停損倍數（0=停用）
- `stop_loss_pts`：固定停損點數
- `take_profit_pts`：固定停利點數
- `daily_max_loss`：當日最大虧損額（元）
- `max_trades_per_day`：當日最大進場次數
- `trade_start_hhmm` / `trade_end_hhmm`：可開倉時段

**停止策略**：
- 點選執行中的策略，看右側的「停止」按鈕
- 停止後所有部位的停損停利照常執行（只是不再進場）

### 部位面板（Position Panel）

實時顯示當前持倉。

**數據新鮮度**（左下角黃色警告）：
- 綠色字：數據 < 30 秒，最新
- 黃色字：數據 > 30 秒（後端可能在重啟），用的是快取舊值
- 紅色字：後端完全無回應

**損益計算**：
- **未實現損益**：根據最新報價實時計算（2 秒更新一次）
- **已實現損益**：成交單累計
- 單位：元

**部位詳情**：
- 代碼、方向（多/空）、口數
- 進場價、目前價、損益

### 委託面板（Order Panel）

下單和委託管理。

**模擬盤**：
- 點「下單」直接送出
- 秒成（因為是模擬）

**正式盤・真錢**（兩段確認）：
1. 點「下單」→ 按鈕變橘色「真錢下單・3 秒內再點一次確認」
2. 在 3 秒內再點一次 → 真的送出訂單
3. 超過 3 秒沒確認 → 自動取消

**委託列表**：
- 顯示所有委託單（包括已成交、已取消）
- 成交時自動更新

### 成交面板（Trades Panel）

所有成交單的歷史記錄。

**自動更新**：
- 每 3 秒拉一次最新成交
- 下單成功時立即刷新（不用等 3 秒）

**成交詳情**：
- 代碼、方向、口數、成交價、時間

---

## 故障排查

### ❌ 儀表板完全黑屏

**可能原因**：
1. 前端沒啟動
2. Node.js 版本太舊
3. 沒有執行 `npm install`

**解決**：
```bash
cd frontend
rm -rf node_modules package-lock.json
npm install
npm run dev
```

### ❌ 儀表板載入，但無法看到數據

**可能原因**：
1. 後端沒啟動
2. `.env.local` 的 API URL 設錯
3. CORS 被擋

**檢查**：
```bash
# 後端有沒有開
curl http://100.127.125.13:8002/api/health

# .env.local 內容
cat frontend/.env.local | grep NEXT_PUBLIC
```

**修正**：
1. 確認後端在線：`curl http://100.127.125.13:8002/api/health`
2. 編輯 `.env.local`，改正 API URL
3. **重啟前端**（改 `.env.local` 一定要重啟）：
   ```bash
   cd frontend
   npm run dev
   ```

### ❌ 看不到報價（行情條全是 0）

**可能原因**：
1. 後端沒訂閱報價
2. WebSocket 連線失敗
3. 正式盤或模擬盤沒開

**檢查**：
```bash
# 看最新報價端點有沒有數據
curl http://100.127.125.13:8002/api/quote/last

# 看報價 WebSocket 有沒有連上（瀏覽器開發者工具 → Network → WS）
# 應該看到連線到 ws://100.127.125.13:8002/api/quote/ws
```

### ❌ 下單按鈕按不了

**可能原因**：
1. 策略沒啟動
2. 後端連線失敗
3. 保證金不足（正式盤）

**檢查**：
```bash
# 看策略有沒有啟動
curl http://100.127.125.13:8002/api/strategy | python3 -m json.tool | grep is_running

# 看保證金
curl http://100.127.125.13:8002/api/position/margin | python3 -m json.tool
```

### ⚠️ 正式盤和模擬盤切換後無法看到數據

**原因**：`.env.local` 沒改，或改完沒重啟前端

**解決**：
1. 編輯 `frontend/.env.local`，確認兩個 URL 都設對
2. **停止前端**（Ctrl+C）
3. **重新啟動前端**：`npm run dev`
4. 重新整理瀏覽器

---

## 部署到正式環境

### 步驟 1：編譯

```bash
cd frontend
npm run build
```

這會在 `frontend/.next` 產生最佳化的 bundle。

### 步驟 2：啟動

```bash
npm run start
```

訪問：http://100.127.125.13:3002

### 步驟 3：後台執行（可選）

如果要離開終端機還保持運行：

```bash
nohup npm run start > /tmp/lh_frontend.log 2>&1 &
```

監看日誌：
```bash
tail -f /tmp/lh_frontend.log
```

---

## 開發備註

### 新增頁面或元件

1. 在 `frontend/app` 或 `frontend/components` 新建檔案
2. 自動熱更新，無需重啟
3. TypeScript 會在編譯時檢查型別

### 修改 API 端點

- 所有 API 呼叫都在 `frontend/lib/api.ts`
- 改完會自動重新載入
- 確認 `.env.local` 的 `NEXT_PUBLIC_API_URL` 指向正確後端

### 修改樣式

- 用 Tailwind CSS（預設配置在 `frontend/tailwind.config.ts`）
- 改完自動重新載入

---

## 常用命令速查

```bash
# 開發（熱更新）
cd frontend && npm run dev

# 正式部署
cd frontend && npm run build && npm run start

# 後台執行正式版本
nohup npm run start > /tmp/lh_frontend.log 2>&1 &

# 檢查進程
ps aux | grep 'next\|npm'

# 殺掉前端
pkill -f 'next start\|npm run dev'

# 看日誌
tail -f /tmp/lh_frontend.log
```

---

## 常見快捷鍵

| 快捷鍵 | 功能 |
|-------|------|
| `Cmd+K` 或 `Ctrl+K` | 快速命令選單（Next.js） |
| `Cmd+Shift+L` 或 `Ctrl+Shift+L` | 切換深淺色主題 |
| `F12` | 開發者工具 |

---

## 問題排查清單

遇到前端問題時，逐一檢查：

- [ ] 後端在線？`curl http://100.127.125.13:8002/api/health`
- [ ] `.env.local` 的 API URL 對嗎？
- [ ] 改完 `.env.local` 重啟前端了嗎？
- [ ] 瀏覽器快取清掉了嗎？（Cmd+Shift+Delete）
- [ ] 前端進程真的在跑嗎？`ps aux | grep next`
- [ ] 選對交易模式了嗎？（模擬盤 vs 正式盤）
