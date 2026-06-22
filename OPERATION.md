# lh-trading 操作手冊

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
```

### 查看最新報價

```bash
curl http://100.127.125.13:8002/api/quote/last | python3 -m json.tool
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
- 這是**正常的自動恢復機制**，不用手動干預
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
