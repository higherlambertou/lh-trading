"""進場市場狀態紀錄（需求文件/進場市場狀態紀錄_需求文件.md，v1）。

只做一件事：**多記幾個欄位，不改變任何交易行為。** 每筆進場成交寫一筆「進場快照」（當下的 Hurst／IV 等市場狀態、策略與類型、
停損價、口數），出場成交回填損益與出場原因；供日後驗證「市場狀態指標是否真的有用」。v1 不分析、不統計、不讓 Hurst 或 IV 影響任何決策。

怎麼接到現有的成交紀錄（trade_log.db，沿用同一個資料庫，避免兩邊對不上）：
  - 委託送出時（TradeLog.record_order）把「當下已有的最新市場狀態」連同策略參數、停損價存成 orders.state_json
    （取不到的欄位記 null，並在 missing_reason 寫原因；不補 0、不沿用上一次、不補預設值）。
  - 券商成交回報（events，kind='deal'）進來後，由 EntrySync 在 trade_log 的 writer thread 裡依時間順序處理：
    以 FIFO 淨額計算把這筆成交判成「進場」（開新倉／加碼）或「出場」（平掉先前的進場），進場寫 entry_snapshots，出場寫 exit_fills。
  - trade_id = 進場那張委託的券商 trade_id（進出場共用）。成交明細（每次成交）仍在 events，不重複存。

保證（對應需求文件的驗收標準）：
  - 冪等：快照以 (mode, trade_id) 為主鍵、出場以 fill_key 為唯一鍵，全部 INSERT OR IGNORE；重啟時從「功能啟用的起點」重新依序處理一遍，
    不會重複也不會遺失。重複的成交回報（同一筆）用 deal_ts／exchange_seq 辨識並忽略。
  - 失敗不影響下單：整條路徑在 writer thread（佇列、不阻塞）；失敗會 ERROR log（含 ENTRYLOG ALERT）、累計次數、寫進 entry_log_errors，
    並由 /api/tradelog/entries/status 回報；處理到一半失敗的成交不會被跳過，下一輪重試。
  - 只新增不改寫：更正走 entry_corrections（指向原紀錄），歷史列不動。
  - 沒有任何策略或風控模組讀取這些欄位（tests/test_entry_log.py 用靜態檢查擋）。

已知限制：
  - 功能啟用前就存在的部位（沒有進場快照）：平倉會寫成「沒有對應進場」的出場列（trade_id 為空），並由 reconcile 依券商持倉補一筆 legacy 部位避免誤判成新進場。
  - 以 FIFO 淨額判斷進場／出場，與券商的實際沖銷順序可能不同（多個不同策略的部位混在一起時）。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

TAIPEI = timezone(timedelta(hours=8))
POINT_VALUE = {"TMF": 10, "MXF": 50, "TXF": 200}          # 期貨每點元數（選擇權不處理）
ORDER_WAIT_SEC = 30.0           # 成交回報先到、委託紀錄還沒寫進來時，最多等這麼久
MAX_RETRY = 5                   # 同一筆成交處理失敗幾次後放棄（寫進 entry_log_errors，不再卡住後面的）

# 策略類型：由人定義，程式不判斷（需求文件：「策略類型由策略設定檔明確標示」）。
# 依 STRATEGIES.md〈市場狀態判斷〉的既有分類；scalp 靠 market_bias 可順可逆、手動單沒有策略邏輯 → other。要改就改這裡。
STRATEGY_TYPES: dict[str, str] = {
    "ma_cross": "trend", "breakout": "trend", "momentum": "trend", "orb": "trend",
    "rsi": "mean_reversion", "bollinger": "mean_reversion", "vwap_revert": "mean_reversion",
    "scalp": "other", "manual": "other", "manual_monitor": "other",
}

EXIT_KINDS = {"tp", "sl", "trail", "revert_exit", "reverse_close", "exit"}
EXIT_REASON = {"tp": "take_profit", "sl": "stop_loss", "trail": "trail_stop", "revert_exit": "revert_target",
               "reverse_close": "reversal", "exit": "other"}
MARKET_KEYS = ("hurst_value", "hurst_z", "hurst_window", "hurst_bar_period", "hurst_noise",
               "iv_value", "iv_percentile", "state_timestamp", "state_date", "market_state")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_snapshots(
  mode TEXT NOT NULL, trade_id TEXT NOT NULL,
  contract TEXT, strategy_name TEXT, strategy_type TEXT, direction TEXT,
  order_ts REAL, entry_ts REAL, entry_time TEXT, entry_price REAL, signal_price REAL, stop_price REAL, lots INTEGER,
  hurst_value REAL, hurst_z REAL, hurst_window INTEGER, hurst_bar_period TEXT, hurst_noise REAL,
  iv_value REAL, iv_percentile REAL, state_timestamp REAL, state_date TEXT, market_state TEXT, missing_reason TEXT,
  strategy_params TEXT, created_ts REAL,
  PRIMARY KEY(mode, trade_id));
CREATE TABLE IF NOT EXISTS exit_fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fill_key TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL, trade_id TEXT NOT NULL DEFAULT '',
  exit_order_id TEXT, strategy_name TEXT, exit_ts REAL, exit_time TEXT, exit_price REAL, lots INTEGER,
  entry_price REAL, pnl_points REAL, pnl_ntd REAL, exit_reason TEXT, note TEXT, created_ts REAL);
CREATE INDEX IF NOT EXISTS idx_exit_fills_trade ON exit_fills(mode, trade_id);
CREATE TABLE IF NOT EXISTS entry_corrections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL, trade_id TEXT NOT NULL, field TEXT NOT NULL, old_value TEXT, new_value TEXT, reason TEXT, created_ts REAL);
CREATE TABLE IF NOT EXISTS entry_log_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entry_log_errors(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, where_ TEXT, event_id INTEGER, trade_id TEXT, error TEXT);
"""


def strategy_type(name: str) -> str:
    return STRATEGY_TYPES.get(name or "", "other")


def product(code: str) -> Optional[str]:
    """合約代碼 → 商品（TMF／MXF／TXF）。'TMF'、'TMFJ6'、'TMFR1' 都是 TMF；選擇權（TXO…）與其他回 None。"""
    code = (code or "").upper()
    return next((p for p in POINT_VALUE if code.startswith(p)), None)


def iso_taipei(ts: Optional[float]) -> Optional[str]:
    """統一時區：Asia/Taipei（+08:00）的 ISO 8601；沒有時間就是 None。"""
    return datetime.fromtimestamp(ts, TAIPEI).isoformat(timespec="seconds") if ts else None


# ── 委託送出當下：擷取市場狀態 ─────────────────────────────────────

def capture(tag: dict[str, Any] | None, ctx: dict[str, Any] | None) -> dict[str, Any]:
    """組出 orders.state_json 的內容。只取「已經有的最新值」，不重新計算；取不到就 None，並寫 missing_reason。"""
    snap = (tag or {}).get("snapshot") or {}
    ctx = ctx or {}
    market = {k: snap.get(k) for k in MARKET_KEYS}
    missing: list[str] = []
    if not snap:
        missing.append("今日市場狀態尚未產生（每個交易日 08:30 才會計算，或服務剛啟動還沒算過）")
    else:
        if snap.get("hurst_value") is None:
            missing.append("Hurst：" + (snap.get("hurst_note") or "資料不足"))
        if snap.get("iv_percentile") is None:
            n, need = snap.get("iv_history_n"), snap.get("iv_min_history")
            missing.append("IV 百分位：" + (f"歷史不足（{n}/{need} 天）" if n is not None and need and n < need
                                           else (snap.get("iv_note") or "沒有資料")))
        if snap.get("iv_value") is None:
            missing.append("IV 值：沒有資料")
    return {"market": market, "missing_reason": "；".join(missing) or None,
            "stop_price": ctx.get("stop_price"), "strategy_params": ctx.get("strategy_params"),
            "attempt": ctx.get("attempt"), "captured_at": time.time()}


# ── 失敗通報 ───────────────────────────────────────────────────────

class Alerts:
    """寫入失敗不能阻擋下單，但也不能靜默：ERROR log、累計次數、保留最近一筆，給 /entries/status 看。"""

    def __init__(self) -> None:
        self.count = 0
        self.last: Optional[dict[str, Any]] = None

    def fire(self, where: str, error: str, trade_id: str = "", event_id: int | None = None) -> None:
        self.count += 1
        self.last = {"ts": time.time(), "where": where, "trade_id": trade_id, "event_id": event_id, "error": error[:300]}
        logger.error("ENTRYLOG ALERT [%s] trade_id=%s event_id=%s：%s（進場市場狀態紀錄失敗，不影響下單）", where, trade_id, event_id, error)


alerts = Alerts()


# ── 同步引擎（只在 trade_log 的 writer thread 裡使用）─────────────────

def strong_id(row: dict[str, Any]) -> bool:
    """券商給的資訊夠不夠辨識「同一筆成交」：交易所序號非空，或成交時間帶小數（不只到秒）。
    實測的 FuturesDeal 回報 exchange_seq 是空字串；成交時間若只到秒，同一秒內相同價量的兩筆部分成交就分不出來。"""
    ts = row.get("deal_ts")
    return bool(row.get("deal_seq")) or (bool(ts) and ts != int(ts))


def deal_key(row: dict[str, Any]) -> str:
    """同一筆成交的識別。識別資訊夠強才用它（可辨識重複送達的同一筆）；不夠就當成不同的成交——
    寧可多記也不要把兩筆相同價量的部分成交併成一筆（另有「累計成交不超過委託口數」擋真正的重複，見 EntrySync.step）。"""
    if strong_id(row):
        return f"{row['mode']}|{row['trade_id']}|{row['price']}|{row['qty']}|{row.get('deal_ts') or ''}|{row.get('deal_seq') or ''}"
    return f"row:{row['id']}"


class EntrySync:
    """mode：只處理這個模式（'sim'／'live'）的成交回報。sim、live 是兩個行程、共用同一個 trade_log.db，
    各自的 writer thread 各管各的；不給（None）就處理全部（測試用）。"""

    def __init__(self, conn: sqlite3.Connection, mode: str | None = None) -> None:
        self.conn = conn
        self.mode = mode
        self.book: dict[tuple[str, str], deque] = {}
        self.seen: set[str] = set()
        self.last_id = 0
        self.start_id = 0
        self._fails: dict[int, int] = {}
        self._orders: dict[tuple[str, str], Optional[dict[str, Any]]] = {}
        self._filled: dict[tuple[str, str], int] = {}                    # 每張委託已計入的成交口數（不能超過委託口數）
        self._legacy: list[tuple[int, str, str, dict[str, Any]]] = []    # 重放時待注入的舊部位：(after_event_id, mode, 商品, lot)

    def _meta(self, key: str) -> str:
        return f"{key}:{self.mode}" if self.mode else key

    # ── 起點與重放 ────────────────────────────────────────────────
    def init(self) -> None:
        self.conn.executescript(SCHEMA)
        row = self.conn.execute("SELECT value FROM entry_log_meta WHERE key=?", (self._meta("start_event_id"),)).fetchone()
        if row is None:
            # 功能第一次啟用：從現有的最後一筆成交回報之後開始。不回補歷史（當時的市場狀態無法確定就不補）
            mx = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            self.conn.execute("INSERT OR IGNORE INTO entry_log_meta(key, value) VALUES(?, ?)", (self._meta("start_event_id"), str(mx)))
            self.conn.execute("INSERT OR IGNORE INTO entry_log_meta(key, value) VALUES(?, ?)", (self._meta("started_ts"), str(time.time())))
            self.conn.commit()
            self.start_id = mx
        else:
            self.start_id = int(row[0])
        self.replay()

    def replay(self) -> None:
        """重啟時從起點重新依序處理一遍：重建 FIFO 部位簿，並補上任何漏寫的列（全部 INSERT OR IGNORE，不會重複）。
        reconcile 補的舊部位有存下來（含補的當下處理到哪一筆），重放時在同一個位置重新放回去，結果才會和原本一樣。"""
        self.book.clear()
        self.seen.clear()
        self._orders.clear()
        self._fails.clear()
        self._filled.clear()
        self.last_id = self.start_id
        self._legacy = self._load_legacy()
        self.step(now=time.time() + 10 * ORDER_WAIT_SEC)          # 重放時不等委託紀錄：歷史資料找不到委託就是找不到

    def _load_legacy(self) -> list[tuple[int, str, str, dict[str, Any]]]:
        out = []
        for key, value in self.conn.execute("SELECT key, value FROM entry_log_meta WHERE key LIKE 'legacy:%'").fetchall():
            try:
                _, mode, prod, after = key.split(":", 3)
                if self.mode and mode != self.mode:
                    continue
                out.append((int(after), mode, prod, {**json.loads(value), "trade_id": "", "legacy": True}))
            except (ValueError, TypeError):
                logger.warning("進場紀錄：看不懂的舊部位紀錄 %s，已略過", key)
        return sorted(out, key=lambda x: x[0])

    def _inject_legacy(self, before_id: int) -> None:
        """把「補的當下處理到的事件編號 < before_id」的舊部位放回部位簿最前面（比任何新進場都舊）。"""
        while self._legacy and self._legacy[0][0] < before_id:
            _, mode, prod, lot = self._legacy.pop(0)
            self.book.setdefault((mode, prod), deque()).appendleft(lot)

    # ── 逐筆處理 ──────────────────────────────────────────────────
    def step(self, now: float | None = None) -> int:
        now = now or time.time()
        sql, args = "SELECT id, ts, mode, trade_id, price, qty, deal_ts, deal_seq FROM events WHERE kind='deal' AND id > ?", [self.last_id]
        if self.mode:
            sql += " AND mode=?"
            args.append(self.mode)
        rows = self.conn.execute(sql + " ORDER BY id", args).fetchall()
        done = 0
        for r in rows:
            ev = dict(zip(("id", "ts", "mode", "trade_id", "price", "qty", "deal_ts", "deal_seq"), r))
            self._inject_legacy(ev["id"])
            if not ev["trade_id"]:
                self.last_id = ev["id"]                                  # 沒有 trade_id 無從對應（解析壞掉那段時期的殘留）
                continue
            key = deal_key(ev)
            if key in self.seen:
                self.last_id = ev["id"]
                continue
            od = self._order(ev["mode"], ev["trade_id"])
            if od is None:
                if now - ev["ts"] < ORDER_WAIT_SEC:
                    break                                                # 委託紀錄可能還在佇列裡，下一輪再試
                self._error("orphan_deal", ev, "有成交回報但找不到對應的委託紀錄")
                self.last_id = ev["id"]
                continue
            fk = (ev["mode"], ev["trade_id"])
            filled, cap = self._filled.get(fk, 0), int(od.get("qty") or 0)
            extra = 0
            if cap > 0 and filled + int(ev["qty"]) > cap:
                # 同一張委託累計成交不可能超過委託口數：多出來的一定是重複送達（或回報有誤），不計入部位
                extra = filled + int(ev["qty"]) - cap
                ev = {**ev, "qty": cap - filled}
            if int(ev["qty"]) <= 0:
                self._error("overfill", ev, f"這張委託已成交 {filled}/{cap} 口，又收到 {extra} 口的成交回報，當成重複送達略過")
                self.seen.add(key)
                self.last_id = ev["id"]
                continue
            saved = {k: deque(dict(lot) for lot in v) for k, v in self.book.items()}
            try:
                with self.conn:                                          # 一筆成交的所有寫入同一個交易
                    self._apply(ev, od)
                self._filled[fk] = filled + int(ev["qty"])
                if extra:
                    self._error("overfill", ev, f"累計成交會超過委託 {cap} 口，多出的 {extra} 口當成重複送達略過")
            except Exception as e:                                       # noqa: BLE001
                self.book = saved                                        # 資料庫已回滾，記憶體的部位簿也要退回去，重試才不會多算一口
                n = self._fails[ev["id"]] = self._fails.get(ev["id"], 0) + 1
                alerts.fire("apply", f"{type(e).__name__}: {e}（第 {n} 次）", ev["trade_id"], ev["id"])
                if n < MAX_RETRY:
                    break                                                # 不跳過、不往後做（順序有關），下一輪重試
                self._error("giveup", ev, f"重試 {MAX_RETRY} 次仍失敗，已略過：{type(e).__name__}: {e}")
            self.seen.add(key)
            self.last_id = ev["id"]
            done += 1
        self._inject_legacy(self.last_id + 1)
        return done

    def _order(self, mode: str, trade_id: str) -> Optional[dict[str, Any]]:
        k = (mode, trade_id)
        if self._orders.get(k) is None:
            r = self.conn.execute(
                "SELECT ts, strategy, reason, contract, action, qty, signal_price, state_json FROM orders "
                "WHERE mode=? AND trade_id=? ORDER BY id LIMIT 1", k).fetchone()
            self._orders[k] = None if r is None else dict(zip(
                ("ts", "strategy", "reason", "contract", "action", "qty", "signal_price", "state_json"), r))
        return self._orders[k]

    def _error(self, where: str, ev: dict[str, Any], msg: str) -> None:
        """同一筆成交回報的同一種問題只通報一次：重啟時會從起點重放，不能每次重啟都對舊的問題再響一次。"""
        try:
            if self.conn.execute("SELECT 1 FROM entry_log_errors WHERE where_=? AND event_id=?", (where, ev["id"])).fetchone():
                logger.info("進場紀錄：成交回報 %s 的 %s 問題先前已通報過：%s", ev["id"], where, msg)
                return
        except Exception:                                                # noqa: BLE001
            pass                                                         # 查不到就當沒通報過（寧可重複通報）
        alerts.fire(where, msg, ev["trade_id"], ev["id"])
        try:
            with self.conn:
                self.conn.execute("INSERT INTO entry_log_errors(ts, where_, event_id, trade_id, error) VALUES(?,?,?,?,?)",
                                  (time.time(), where, ev["id"], ev["trade_id"], msg[:500]))
        except Exception:                                                # noqa: BLE001
            logger.exception("無法寫入 entry_log_errors")

    # ── 一筆成交：判成進場或出場 ──────────────────────────────────
    def _apply(self, ev: dict[str, Any], od: dict[str, Any]) -> None:
        prod = product(od["contract"])
        if prod is None:
            return                                                       # 選擇權等不處理
        side = 1 if od["action"] == "Buy" else -1
        book = self.book.setdefault((ev["mode"], prod), deque())
        qty, price = int(ev["qty"]), float(ev["price"])
        reason = od["reason"] or ""
        label = self._exit_label(od)
        n = 0
        while qty > 0 and book and book[0]["side"] != side:               # 先平掉反向的舊部位（FIFO）
            lot = book[0]
            if lot["qty"] <= 0:                                          # 不該有 0 口的部位；有就丟掉，否則 min() 算出 0 口、迴圈永遠不前進
                book.popleft()
                continue
            q = min(qty, lot["qty"])
            self._insert_exit(ev, od, lot, q, price, prod, self._relay(label, ev, lot), n)
            lot["qty"] -= q
            qty -= q
            n += 1
            if lot["qty"] == 0:
                book.popleft()
        if qty > 0:
            if reason in EXIT_KINDS:
                # 明確的平倉單卻沒有可平的部位（部位在開始記錄前就存在，或進場成交回報遺失）：不能憑空開一筆新進場
                self._insert_exit(ev, od, None, qty, price, prod, label, n,
                                  note="沒有對應的進場快照（部位在開始記錄前就存在，或進場成交回報遺失）")
            else:
                book.append({"trade_id": ev["trade_id"], "side": side, "qty": qty, "price": price, "legacy": False})
                self._insert_snapshot(ev, od, side, price)

    def _relay(self, label: str, ev: dict[str, Any], lot: dict[str, Any]) -> str:
        """停損單送出後沒成交（或送單失敗），之後又送了一張才出場 → stop_loss_relay。
        不靠策略自己數第幾次：看這張出場單之前、這筆進場之後，同一個策略有沒有送過別張原因是 sl 的單（送單失敗的 trade_id 是空的，也算）。"""
        if label != "stop_loss" or not lot.get("trade_id"):
            return label
        entry = self._order(ev["mode"], lot["trade_id"])
        if entry is None:
            return label
        n = self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE mode=? AND strategy=? AND reason='sl' AND ts>=? AND trade_id!=? "
            "AND id < (SELECT MIN(id) FROM orders WHERE mode=? AND trade_id=?)",
            (ev["mode"], entry["strategy"], entry["ts"], ev["trade_id"], ev["mode"], ev["trade_id"])).fetchone()[0]
        return "stop_loss_relay" if n else label

    @staticmethod
    def _exit_label(od: dict[str, Any]) -> str:
        reason, strategy = od["reason"] or "", od["strategy"] or ""
        if reason in EXIT_REASON:
            label = EXIT_REASON[reason]
            try:
                attempt = (json.loads(od["state_json"]).get("attempt") if od.get("state_json") else None) or 0
            except (ValueError, TypeError):
                attempt = 0
            return "stop_loss_relay" if label == "stop_loss" and int(attempt) >= 2 else label   # 停損單沒一次成交、重送後才出場
        return "manual" if strategy == "manual" else "other"

    def _insert_exit(self, ev: dict[str, Any], od: dict[str, Any], lot: Optional[dict[str, Any]], q: int, price: float,
                     contract: str, label: str, n: int, note: str = "") -> None:
        legacy = lot is None or lot.get("legacy", False)
        entry_price = lot["price"] if lot else None
        pts = (price - entry_price) * lot["side"] if lot else None
        if lot and lot.get("legacy"):
            note = "部位在開始記錄前就存在，進場價取自當時的券商持倉均價"
        trade_id = "" if legacy else lot["trade_id"]
        ts = ev.get("deal_ts") or ev["ts"]
        self.conn.execute(
            "INSERT OR IGNORE INTO exit_fills(fill_key, mode, trade_id, exit_order_id, strategy_name, exit_ts, exit_time, exit_price, "
            "lots, entry_price, pnl_points, pnl_ntd, exit_reason, note, created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{deal_key(ev)}|{trade_id or 'x'}|{n}", ev["mode"], trade_id, ev["trade_id"], od["strategy"], ts, iso_taipei(ts), price, q,
             entry_price, None if pts is None else round(pts, 4), None if pts is None else round(pts * q * POINT_VALUE[contract], 2),
             label, note, time.time()))

    def _insert_snapshot(self, ev: dict[str, Any], od: dict[str, Any], side: int, price: float) -> None:
        try:
            st = json.loads(od["state_json"]) if od.get("state_json") else None
        except (ValueError, TypeError):
            st = None
        market = (st or {}).get("market") or {}
        missing = (st or {}).get("missing_reason") if st else "這張進場委託沒有記到市場狀態（功能啟用前下的單，或記錄失敗）"
        params = (st or {}).get("strategy_params")
        ts = ev.get("deal_ts") or ev["ts"]
        self.conn.execute(
            "INSERT OR IGNORE INTO entry_snapshots(mode, trade_id, contract, strategy_name, strategy_type, direction, order_ts, entry_ts, "
            "entry_time, entry_price, signal_price, stop_price, lots, hurst_value, hurst_z, hurst_window, hurst_bar_period, hurst_noise, "
            "iv_value, iv_percentile, state_timestamp, state_date, market_state, missing_reason, strategy_params, created_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["mode"], ev["trade_id"], od["contract"], od["strategy"], strategy_type(od["strategy"]), "long" if side > 0 else "short",
             od["ts"], ts, iso_taipei(ts), price, od["signal_price"], (st or {}).get("stop_price"), od["qty"],
             market.get("hurst_value"), market.get("hurst_z"), market.get("hurst_window"), market.get("hurst_bar_period"),
             market.get("hurst_noise"), market.get("iv_value"), market.get("iv_percentile"), market.get("state_timestamp"),
             market.get("state_date"), market.get("market_state"), missing,
             json.dumps(params, ensure_ascii=False) if params is not None else None, time.time()))

    # ── 與券商持倉對帳 ────────────────────────────────────────────
    def reconcile(self, mode: str, positions: list[dict[str, Any]]) -> None:
        """啟動時把券商實際持倉和部位簿比對：簿上沒有、券商有的（功能啟用前就存在的部位）補一筆 legacy 部位，
        否則它被平倉時會被誤判成一筆新進場。簿上有、數量對不上的只通報，不去調整。"""
        broker_net: dict[str, tuple[int, float]] = {}
        for p in positions or []:
            contract = product(str(p.get("code", "")))
            if not contract:
                continue
            q = int(p.get("quantity", 0) or 0) * (1 if "Buy" in str(p.get("direction", "")) else -1)
            net, _ = broker_net.get(contract, (0, 0.0))
            broker_net[contract] = (net + q, float(p.get("price", 0) or 0))
        for contract in POINT_VALUE:
            net, avg = broker_net.get(contract, (0, 0.0))
            book = self.book.setdefault((mode, contract), deque())
            have = sum(lot["side"] * lot["qty"] for lot in book)
            if net == have:
                continue
            if have == 0:
                lot = {"side": 1 if net > 0 else -1, "qty": abs(net), "price": avg}
                book.appendleft({**lot, "trade_id": "", "legacy": True})
                try:                                                     # 存下來：重啟重放時要在同一個位置放回去，否則手動平倉會被誤判成新進場
                    with self.conn:
                        self.conn.execute("INSERT OR IGNORE INTO entry_log_meta(key, value) VALUES(?, ?)",
                                          (f"legacy:{mode}:{contract}:{self.last_id}", json.dumps(lot)))
                except Exception as e:                                   # noqa: BLE001
                    alerts.fire("reconcile", f"舊部位紀錄存不進去：{type(e).__name__}: {e}")
                logger.info("進場紀錄：%s %s 券商有 %+d 口、紀錄簿是空的 → 補一筆 legacy 部位（進場價取券商均價 %.0f）", mode, contract, net, avg)
            else:
                alerts.fire("reconcile", f"{mode} {contract} 券商持倉 {net:+d} 口，但紀錄簿算出 {have:+d} 口（成交回報可能有遺失）")


# ── 讀取／匯出（任何執行緒，自己的連線）─────────────────────────────

CSV_FIELDS = (
    "trade_id", "mode", "contract", "strategy_name", "strategy_type", "direction", "entry_time", "entry_price", "entry_price_avg",
    "order_lots", "filled_lots", "signal_price", "stop_price",
    "hurst_value", "hurst_z", "hurst_window", "hurst_bar_period", "hurst_noise", "iv_value", "iv_percentile",
    "state_timestamp", "state_age_sec", "market_state", "missing_reason", "strategy_params",
    "status", "exit_lots", "exit_time", "exit_price_avg", "pnl_points", "pnl_ntd", "exit_reason", "corrections")


def fetch_entries(path: Path | str, mode: str | None = None, since_ts: float | None = None, limit: int = 10_000) -> list[dict[str, Any]]:
    """一筆交易一列（進場快照 + 出場彙總），新 → 舊。進場均價與成交口數從成交明細（events）算，出場損益從 exit_fills 加總。"""
    path = Path(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as c:
        c.row_factory = sqlite3.Row
        try:
            sql, args = "SELECT * FROM entry_snapshots WHERE 1=1", []
            if mode:
                sql += " AND mode=?"
                args.append(mode)
            if since_ts is not None:
                sql += " AND entry_ts>=?"
                args.append(since_ts)
            snaps = [dict(r) for r in c.execute(sql + " ORDER BY entry_ts DESC, rowid DESC LIMIT ?", [*args, limit]).fetchall()]
        except sqlite3.OperationalError:
            return []                                                     # 資料表還沒建（功能尚未啟用）
        out = []
        for s in snaps:
            fills = [dict(r) for r in c.execute(
                "SELECT id, mode, trade_id, price, qty, deal_ts, deal_seq FROM events WHERE kind='deal' AND mode=? AND trade_id=? ORDER BY id",
                (s["mode"], s["trade_id"])).fetchall()]
            uniq: dict[str, dict[str, Any]] = {}
            for f in fills:
                uniq.setdefault(deal_key(f), f)                           # 重複送達的同一筆成交只算一次
            filled = sum(int(f["qty"]) for f in uniq.values())
            avg = sum(f["price"] * int(f["qty"]) for f in uniq.values()) / filled if filled else None
            if s["lots"] and filled > s["lots"]:
                filled = int(s["lots"])                                   # 同 step()：累計成交不會超過委託口數，多的是重複送達
            exits = [dict(r) for r in c.execute(
                "SELECT * FROM exit_fills WHERE mode=? AND trade_id=? ORDER BY exit_ts, id", (s["mode"], s["trade_id"])).fetchall()]
            xl = sum(int(e["lots"]) for e in exits)
            xavg = sum(e["exit_price"] * int(e["lots"]) for e in exits) / xl if xl else None
            corr = [dict(r) for r in c.execute(
                "SELECT field, old_value, new_value, reason, created_ts FROM entry_corrections WHERE mode=? AND trade_id=? ORDER BY id",
                (s["mode"], s["trade_id"])).fetchall()]
            out.append({
                "trade_id": s["trade_id"], "mode": s["mode"], "contract": s["contract"], "strategy_name": s["strategy_name"],
                "strategy_type": s["strategy_type"], "direction": s["direction"], "entry_time": s["entry_time"],
                "entry_price": s["entry_price"], "entry_price_avg": round(avg, 4) if avg is not None else None,
                "order_lots": s["lots"], "filled_lots": filled, "signal_price": s["signal_price"], "stop_price": s["stop_price"],
                "hurst_value": s["hurst_value"], "hurst_z": s["hurst_z"], "hurst_window": s["hurst_window"],
                "hurst_bar_period": s["hurst_bar_period"], "hurst_noise": s["hurst_noise"], "iv_value": s["iv_value"],
                "iv_percentile": s["iv_percentile"], "state_timestamp": iso_taipei(s["state_timestamp"]),
                "state_age_sec": round(s["entry_ts"] - s["state_timestamp"]) if s["state_timestamp"] and s["entry_ts"] else None,
                "market_state": s["market_state"], "missing_reason": s["missing_reason"], "strategy_params": s["strategy_params"],
                "status": "closed" if filled and xl >= filled else ("partial" if xl else "open"),
                "exit_lots": xl, "exit_time": iso_taipei(exits[-1]["exit_ts"]) if exits else None,
                "exit_price_avg": round(xavg, 4) if xavg is not None else None,
                "pnl_points": round(sum(e["pnl_points"] or 0 for e in exits), 4) if exits else None,
                "pnl_ntd": round(sum(e["pnl_ntd"] or 0 for e in exits), 2) if exits else None,
                "exit_reason": ",".join(dict.fromkeys(e["exit_reason"] for e in exits)) if exits else None,
                "corrections": json.dumps(corr, ensure_ascii=False) if corr else None,
            })
        return out


def to_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_FIELDS})       # null 就是空欄，不是 0
    return buf.getvalue()


def add_correction(path: Path | str, mode: str, trade_id: str, field: str, new_value: Any, reason: str) -> int:
    """更正走新增一列、指向原紀錄；快照與出場列本身不動。回傳更正紀錄的 id。"""
    with closing(sqlite3.connect(path, timeout=10)) as c:
        c.executescript(SCHEMA)
        row = c.execute(f"SELECT {field} FROM entry_snapshots WHERE mode=? AND trade_id=?", (mode, trade_id)).fetchone() \
            if field in {"strategy_type", "stop_price", "direction", "entry_price", "lots"} else None
        cur = c.execute("INSERT INTO entry_corrections(mode, trade_id, field, old_value, new_value, reason, created_ts) VALUES(?,?,?,?,?,?,?)",
                        (mode, trade_id, field, None if row is None else str(row[0]), str(new_value), reason, time.time()))
        c.commit()
        return int(cur.lastrowid)


def status(path: Path | str, mode: str | None = None) -> dict[str, Any]:
    """給 /api/tradelog/entries/status：資料量、尚未處理的成交回報數、通報次數。"""
    path = Path(path)
    out: dict[str, Any] = {"alerts": alerts.count, "last_alert": alerts.last, "snapshots": 0, "exits": 0, "errors_logged": 0, "pending_deals": 0}
    if not path.exists():
        return out
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as c:
            out["snapshots"] = c.execute("SELECT COUNT(*) FROM entry_snapshots").fetchone()[0]
            out["exits"] = c.execute("SELECT COUNT(*) FROM exit_fills").fetchone()[0]
            out["errors_logged"] = c.execute("SELECT COUNT(*) FROM entry_log_errors").fetchone()[0]
            start = c.execute("SELECT value FROM entry_log_meta WHERE key=?", (f"start_event_id:{mode}" if mode else "start_event_id",)).fetchone()
            out["start_event_id"] = int(start[0]) if start else None
            if start:
                out["pending_deals"] = c.execute(
                    "SELECT COUNT(*) FROM events e WHERE kind='deal' AND id>:start AND trade_id!='' AND (:mode IS NULL OR e.mode=:mode) "
                    "AND EXISTS (SELECT 1 FROM orders o WHERE o.mode=e.mode AND o.trade_id=e.trade_id AND "
                    "(UPPER(o.contract) LIKE 'TMF%' OR UPPER(o.contract) LIKE 'MXF%' OR UPPER(o.contract) LIKE 'TXF%')) "
                    "AND NOT EXISTS (SELECT 1 FROM entry_snapshots s WHERE s.mode=e.mode AND s.trade_id=e.trade_id) "
                    "AND NOT EXISTS (SELECT 1 FROM exit_fills x WHERE x.mode=e.mode AND x.exit_order_id=e.trade_id)",
                    {"start": int(start[0]), "mode": mode}).fetchone()[0]
    except sqlite3.OperationalError:
        pass
    return out
