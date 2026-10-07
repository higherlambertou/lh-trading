"""成交紀錄（SQLite）：每一筆委託與券商成交回報，附上「決策當下」的脈絡。

為什麼要有：偏差正常化檢查（滑價、停損成交價 vs 設定價）、逐筆績效按市場狀態拆分、破產機率、
停損驗證都需要「訊號價／委託價／實際成交價」；原本程式沒有保存任何委託與成交，
而這類資料只能從開始記錄的那天起累積。

這條路徑在真錢下單上，所以設計成「純加法、永不影響下單」：
  - 所有 record_* 都吞例外；寫入走獨立 writer thread（queue.put_nowait，滿了丟棄並計數，絕不阻塞）
  - 在 broker.place_order / place_option_order 這個唯一出口記錄；策略等呼叫端用 contextvars 補上脈絡
    （策略名、原因 entry/tp/sl/trail…、訊號價、參考價=停損/停利設定價）。沒設脈絡的委託照記，strategy=''
  - 成交回報（order_event）由 broker._dispatch 原樣轉進來，與委託以 trade_id 在讀取時才 join
  - 市場狀態標記：daily_summary 每次更新判斷時 set_market_tag，記錄當下的狀態（逐筆績效拆分用）
  - TRADE_LOG=false 整個關掉（所有 hook 變 no-op）

資料庫：data/trade_log.db（已在 .gitignore；無法回補，請備份）。sim/live 共用同一檔，以 mode 區分。
"""
from __future__ import annotations

import contextvars
import logging
import math
import os
import queue
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "trade_log.db"
FLUSH_INTERVAL = 1.0        # 秒
FLUSH_BATCH = 200           # 筆
QUEUE_MAX = 20_000
RETRY_CAP = 5_000           # 寫入持續失敗時，緩衝超過此數量才丟棄
IOC_UNFILLED_AFTER = 30     # IOC 單超過幾秒仍無成交/取消回報，才視為「沒成交」

_CTX: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("trade_log_ctx", default=None)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, mode TEXT NOT NULL,
  trade_id TEXT NOT NULL DEFAULT '',
  strategy TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
  contract TEXT, action TEXT, qty INTEGER,
  price_type TEXT, order_type TEXT, limit_price REAL, octype TEXT,
  signal_price REAL, ref_price REAL,
  market_state TEXT, hurst_state TEXT, iv_state TEXT, direction INTEGER, tag_date TEXT,
  status TEXT, error TEXT, latency_ms REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, mode TEXT NOT NULL,
  kind TEXT NOT NULL,
  trade_id TEXT NOT NULL DEFAULT '',
  price REAL, qty INTEGER,
  op_type TEXT, op_code TEXT, op_msg TEXT, raw_state TEXT);
CREATE INDEX IF NOT EXISTS idx_orders_trade ON orders(trade_id);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id);
"""

_ORDER_COLS = ("ts", "mode", "trade_id", "strategy", "reason", "contract", "action", "qty", "price_type",
               "order_type", "limit_price", "octype", "signal_price", "ref_price", "market_state",
               "hurst_state", "iv_state", "direction", "tag_date", "status", "error", "latency_ms")
_EVENT_COLS = ("ts", "mode", "kind", "trade_id", "price", "qty", "op_type", "op_code", "op_msg", "raw_state")


def current_mode() -> str:
    return "sim" if os.getenv("SIMULATION", "true").lower() == "true" else "live"


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    v = sorted(values)
    n = len(v)
    mid = v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    return {"n": n, "mean": round(sum(v) / n, 2), "median": round(mid, 2),
            "p95": round(v[max(0, math.ceil(0.95 * n) - 1)], 2), "max": round(v[-1], 2)}


class TradeLog:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.getenv("TRADE_LOG_PATH") or DEFAULT_PATH)
        self.enabled = os.getenv("TRADE_LOG", "true").lower() == "true"
        self._q: queue.Queue[tuple[str, tuple]] = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dropped = 0
        self._tag: dict[str, Any] = {}

    # ── 脈絡（呼叫端在下單前設定）────────────────────────────────
    @contextmanager
    def context(self, **fields: Any) -> Iterator[None]:
        """with trade_log.context(strategy=..., reason=..., signal_price=..., ref_price=...): await broker.place_order(...)
        contextvars 會跟著 await 走，巢狀時內層覆蓋外層同名欄位。"""
        token = _CTX.set({**(_CTX.get() or {}), **fields})
        try:
            yield
        finally:
            _CTX.reset(token)

    def set_market_tag(self, tag: dict[str, Any] | None) -> None:
        """daily_summary 更新今日判斷時呼叫：之後的委託都會標上這組市場狀態。"""
        self._tag = dict(tag or {})

    # ── 記錄（任何執行緒；永不丟例外、永不阻塞）──────────────────
    def record_order(self, *, contract: str, action: str, qty: int, price_type: str = "",
                     order_type: str = "", limit_price: float = 0.0, octype: str = "",
                     trade_id: str = "", status: str = "", error: str = "",
                     latency_ms: float = 0.0, ts: float | None = None) -> None:
        if not self.enabled:
            return
        try:
            ctx, tag = _CTX.get() or {}, self._tag
            self._put("order", (
                ts or time.time(), current_mode(), str(trade_id or ""), str(ctx.get("strategy", "")),
                str(ctx.get("reason", "")), str(contract), str(action), int(qty), str(price_type),
                str(order_type), float(limit_price or 0), str(octype), ctx.get("signal_price"),
                ctx.get("ref_price"), tag.get("market_state"), tag.get("hurst_state"), tag.get("iv_state"),
                tag.get("direction"), tag.get("as_of"), str(status), str(error)[:300], float(latency_ms or 0),
            ))
        except Exception:
            logger.exception("TradeLog.record_order 失敗（已忽略，不影響下單）")

    def record_event(self, ev: dict[str, Any]) -> None:
        """券商委託/成交回報（worker 的 order_event 原樣傳入）。"""
        if not self.enabled:
            return
        try:
            kind = "deal" if "Deal" in str(ev.get("state", "")) else "order"
            self._put("event", (
                time.time(), current_mode(), kind, str(ev.get("trade_id") or ""),
                float(ev.get("price") or 0), int(ev.get("quantity") or 0),
                str(ev.get("op_type") or ""), str(ev.get("op_code") or ""),
                str(ev.get("op_msg") or "")[:300], str(ev.get("state") or ""),
            ))
        except Exception:
            logger.exception("TradeLog.record_event 失敗（已忽略）")

    def _put(self, kind: str, row: tuple) -> None:
        try:
            self._q.put_nowait((kind, row))
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.error("TradeLog 佇列滿，累計丟棄 %d 筆", self._dropped)

    # ── 生命週期 ─────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled:
            logger.info("TradeLog 停用（TRADE_LOG=false）")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="trade-log", daemon=True)
        self._thread.start()
        logger.info("TradeLog 已啟動 → %s", self.path)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        self._thread = None

    # ── writer thread ────────────────────────────────────────────
    def _run(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        except Exception:
            logger.exception("TradeLog 無法開啟資料庫，成交紀錄停用")
            return
        buf: list[tuple[str, tuple]] = []
        last = time.monotonic()
        while True:
            stopping = self._stop.is_set()
            try:
                buf.append(self._q.get(timeout=0.0 if stopping else 0.3))
            except queue.Empty:
                if stopping:
                    break
            now = time.monotonic()
            if buf and (len(buf) >= FLUSH_BATCH or now - last >= FLUSH_INTERVAL):
                if self._flush(conn, buf):
                    buf.clear()
                elif len(buf) > RETRY_CAP:
                    logger.error("TradeLog 寫入持續失敗，丟棄 %d 筆", len(buf))
                    buf.clear()
                last = now
        if buf:
            self._flush(conn, buf)
        conn.close()

    @staticmethod
    def _flush(conn: sqlite3.Connection, buf: list[tuple[str, tuple]]) -> bool:
        try:
            orders = [r for k, r in buf if k == "order"]
            events = [r for k, r in buf if k == "event"]
            if orders:
                conn.executemany(
                    f"INSERT INTO orders({','.join(_ORDER_COLS)}) VALUES ({','.join('?' * len(_ORDER_COLS))})", orders)
            if events:
                conn.executemany(
                    f"INSERT INTO events({','.join(_EVENT_COLS)}) VALUES ({','.join('?' * len(_EVENT_COLS))})", events)
            conn.commit()
            return True
        except Exception as e:
            logger.error("TradeLog 寫入失敗（%d 筆，稍後重試）: %s", len(buf), e)
            try:
                conn.rollback()
            except Exception:
                pass
            return False

    # ── 讀取（任何執行緒，自己的連線）────────────────────────────
    def orders(self, mode: str | None = None, limit: int = 100, since_ts: float | None = None,
               strategy: str | None = None) -> list[dict[str, Any]]:
        """委託（新→舊）+ 成交彙總。slip_* 為「對我方不利為正」的點數：
        Buy = 成交價 - 基準價；Sell = 基準價 - 成交價。基準 = 訊號價（slip_signal）／停損停利設定價（slip_ref）。"""
        if not self.path.exists():
            return []
        sql = """
        WITH f AS (
          SELECT trade_id, mode, SUM(qty) AS fill_qty, SUM(price*qty) AS fill_value,
                 MIN(ts) AS first_fill_ts, MAX(ts) AS last_fill_ts
          FROM events WHERE kind='deal' AND trade_id != '' GROUP BY trade_id, mode),
        c AS (
          SELECT trade_id, mode,
                 SUM(op_type='Cancel' AND op_code IN ('','00')) AS cancels,
                 SUM(op_code NOT IN ('','00')) AS rejects
          FROM events WHERE kind='order' AND trade_id != '' GROUP BY trade_id, mode)
        SELECT o.*, COALESCE(f.fill_qty,0) AS fill_qty, f.fill_value, f.first_fill_ts, f.last_fill_ts,
               COALESCE(c.cancels,0) AS cancels, COALESCE(c.rejects,0) AS rejects
        FROM orders o
        LEFT JOIN f ON f.trade_id=o.trade_id AND f.mode=o.mode AND o.trade_id != ''
        LEFT JOIN c ON c.trade_id=o.trade_id AND c.mode=o.mode AND o.trade_id != ''
        WHERE 1=1"""
        args: list[Any] = []
        if mode:
            sql += " AND o.mode=?"
            args.append(mode)
        if since_ts is not None:
            sql += " AND o.ts>=?"
            args.append(since_ts)
        if strategy is not None:
            sql += " AND o.strategy=?"
            args.append(strategy)
        sql += " ORDER BY o.ts DESC LIMIT ?"
        args.append(limit)
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        now = time.time()
        for r in rows:
            side = 1 if r["action"] == "Buy" else -1
            fq = r["fill_qty"] or 0
            avg = r["fill_value"] / fq if fq and r["fill_value"] else None
            r["avg_fill"] = round(avg, 2) if avg else None
            r["slip_signal"] = round((avg - r["signal_price"]) * side, 2) if avg and r["signal_price"] else None
            r["slip_ref"] = round((avg - r["ref_price"]) * side, 2) if avg and r["ref_price"] else None
            r["fill_delay_ms"] = round((r["first_fill_ts"] - r["ts"]) * 1000) if r["first_fill_ts"] else None
            r["outcome"] = self._outcome(r, fq, now)
        return rows

    @staticmethod
    def _outcome(r: dict[str, Any], fill_qty: int, now: float) -> str:
        if r["status"] in ("error", "timeout"):
            return r["status"]
        if fill_qty and fill_qty >= (r["qty"] or 0):
            return "filled"
        if fill_qty:
            return "partial"
        if r["rejects"]:
            return "rejected"
        if r["cancels"]:
            return "cancelled"
        if r["order_type"] == "IOC" and now - r["ts"] > IOC_UNFILLED_AFTER:
            return "unfilled"            # IOC 該立刻成交或取消，過了這麼久仍沒回報 → 值得查
        return "open"

    def summary(self, mode: str | None = None, since_ts: float | None = None, limit: int = 50_000) -> dict[str, Any]:
        """依 (策略, 原因) 彙總：成交結果、滑價分布（訊號價／設定價）、送單延遲。"""
        rows = self.orders(mode=mode, limit=limit, since_ts=since_ts)
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            g = groups.setdefault((r["strategy"], r["reason"]), {
                "strategy": r["strategy"], "reason": r["reason"], "n": 0, "outcomes": {},
                "_sig": [], "_ref": [], "_lat": []})
            g["n"] += 1
            g["outcomes"][r["outcome"]] = g["outcomes"].get(r["outcome"], 0) + 1
            if r["slip_signal"] is not None:
                g["_sig"].append(r["slip_signal"])
            if r["slip_ref"] is not None:
                g["_ref"].append(r["slip_ref"])
            if r["latency_ms"]:
                g["_lat"].append(r["latency_ms"])
        out = []
        for g in groups.values():
            lat = g.pop("_lat")
            out.append({**{k: v for k, v in g.items() if not k.startswith("_")},
                        "slip_signal": _stats(g["_sig"]), "slip_ref": _stats(g["_ref"]),
                        "latency_ms": round(sum(lat) / len(lat), 1) if lat else None})
        out.sort(key=lambda x: (x["strategy"], x["reason"]))
        return {"mode": mode, "since": since_ts, "orders": len(rows), "groups": out}


trade_log = TradeLog()


def _main() -> None:  # pragma: no cover
    """python -m core.trade_log [天數]：只讀資料庫，印出成交紀錄彙總（不連券商）。"""
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    s = trade_log.summary(current_mode(), time.time() - days * 86400)
    print(f"[{s['mode']}] 近 {days:g} 天，共 {s['orders']} 筆委託（滑價單位：點，正=對我方不利）")
    for g in s["groups"]:
        sig, ref = g["slip_signal"], g["slip_ref"]
        print(f"  {g['strategy'] or '(未標記)':<14s}{g['reason'] or '-':<12s}n={g['n']:<4d}{g['outcomes']}"
              + (f"  訊號價滑價 mean={sig['mean']} p95={sig['p95']} max={sig['max']}" if sig else "")
              + (f"  設定價滑價 mean={ref['mean']} p95={ref['p95']} max={ref['max']}" if ref else ""))


if __name__ == "__main__":  # pragma: no cover
    _main()
