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
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from core import entry_log

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "trade_log.db"
FLUSH_INTERVAL = 1.0        # 秒
FLUSH_BATCH = 200           # 筆
QUEUE_MAX = 20_000
RECENT_MAX = 500                 # 記住最近幾筆委託的脈絡（成交推播要知道是哪個合約、買還是賣、誰下的）
RETRY_CAP = 5_000           # 寫入持續失敗時，緩衝超過此數量才丟棄
IOC_UNFILLED_AFTER = 30     # IOC 單超過幾秒仍無成交/取消回報，才視為「沒成交」
POINT_VALUE = {"TMF": 10, "MXF": 50, "TXF": 200}     # 期貨每點元數（選擇權不處理）

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
  status TEXT, error TEXT, latency_ms REAL, state_json TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, mode TEXT NOT NULL,
  kind TEXT NOT NULL,
  trade_id TEXT NOT NULL DEFAULT '',
  price REAL, qty INTEGER,
  op_type TEXT, op_code TEXT, op_msg TEXT, raw_state TEXT, deal_ts REAL, deal_seq TEXT);
CREATE INDEX IF NOT EXISTS idx_orders_trade ON orders(trade_id);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_events_trade ON events(trade_id);
"""

_ORDER_COLS = ("ts", "mode", "trade_id", "strategy", "reason", "contract", "action", "qty", "price_type",
               "order_type", "limit_price", "octype", "signal_price", "ref_price", "market_state",
               "hurst_state", "iv_state", "direction", "tag_date", "status", "error", "latency_ms", "state_json")
_EVENT_COLS = ("ts", "mode", "kind", "trade_id", "price", "qty", "op_type", "op_code", "op_msg", "raw_state", "deal_ts", "deal_seq")


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
        self._entry: entry_log.EntrySync | None = None
        self._recent: "OrderedDict[str, dict[str, Any]]" = OrderedDict()      # trade_id → 委託脈絡（成交推播用；成交回報只有 trade_id）
        self.deal_listeners: list[Callable[[dict[str, Any], dict[str, Any]], None]] = []   # 每筆成交回報：(事件, 委託脈絡)；任何例外都吞掉

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
            try:
                state_json = json.dumps(entry_log.capture(tag, ctx), ensure_ascii=False, default=str)
            except Exception as e:                      # noqa: BLE001  進場快照的擷取失敗不能擋下單，但不能靜默
                state_json = None
                entry_log.alerts.fire("capture", f"{type(e).__name__}: {e}", str(trade_id or ""))
            if trade_id:
                self._recent[str(trade_id)] = {"strategy": str(ctx.get("strategy", "")), "reason": str(ctx.get("reason", "")), "contract": str(contract),
                                               "action": str(action), "qty": int(qty)}
                while len(self._recent) > RECENT_MAX:
                    self._recent.popitem(last=False)
            self._put("order", (
                ts or time.time(), current_mode(), str(trade_id or ""), str(ctx.get("strategy", "")),
                str(ctx.get("reason", "")), str(contract), str(action), int(qty), str(price_type),
                str(order_type), float(limit_price or 0), str(octype), ctx.get("signal_price"),
                ctx.get("ref_price"), tag.get("market_state"), tag.get("hurst_state"), tag.get("iv_state"),
                tag.get("direction"), tag.get("as_of"), str(status), str(error)[:300], float(latency_ms or 0), state_json,
            ))
        except Exception:
            logger.exception("TradeLog.record_order 失敗（已忽略，不影響下單）")

    def record_event(self, ev: dict[str, Any]) -> None:
        """券商委託/成交回報（worker 的 order_event 原樣傳入）。"""
        if not self.enabled:
            return
        try:
            kind = "deal" if "Deal" in str(ev.get("state", "")) else "order"
            if kind == "deal":
                for fn in list(self.deal_listeners):                                    # 成交推播等：不能影響成交紀錄
                    try:
                        fn(ev, dict(self._recent.get(str(ev.get("trade_id") or ""), {})))
                    except Exception:  # noqa: BLE001
                        logger.exception("成交監聽失敗（已忽略）")
            self._put("event", (
                time.time(), current_mode(), kind, str(ev.get("trade_id") or ""),
                float(ev.get("price") or 0), int(ev.get("quantity") or 0),
                str(ev.get("op_type") or ""), str(ev.get("op_code") or ""),
                str(ev.get("op_msg") or "")[:300], str(ev.get("state") or ""),
                float(ev.get("deal_ts") or 0) or None, str(ev.get("deal_seq") or "") or None,
            ))
        except Exception:
            logger.exception("TradeLog.record_event 失敗（已忽略）")

    def record_positions(self, positions: list[dict[str, Any]]) -> None:
        """啟動時把券商目前的持倉交給進場紀錄對帳（功能啟用前就存在的部位，被平倉時才不會被誤判成新進場）。"""
        if not self.enabled:
            return
        try:
            self._put("reconcile", (current_mode(), [dict(p) for p in positions or []]))
        except Exception:
            logger.exception("TradeLog.record_positions 失敗（已忽略）")

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
            self._migrate(conn)
            conn.commit()
        except Exception:
            logger.exception("TradeLog 無法開啟資料庫，成交紀錄停用")
            return
        self._entry = None
        try:
            self._entry = entry_log.EntrySync(conn, current_mode())
            self._entry.init()
        except Exception as e:                          # noqa: BLE001
            entry_log.alerts.fire("init", f"{type(e).__name__}: {e}")      # 進場紀錄起不來也不能影響成交紀錄本身
            self._entry = None
        buf: list[tuple[str, tuple]] = []
        last = last_entry = time.monotonic()
        while True:
            stopping = self._stop.is_set()
            try:
                item = self._q.get(timeout=0.0 if stopping else 0.3)
                if item[0] == "reconcile":
                    if buf and self._flush(conn, buf):          # 對帳要看到最新的成交：先把佇列裡的寫進去、處理完，再比對
                        buf.clear()
                    self._entry_call(self._entry.step if self._entry else None)
                    self._entry_call(lambda it=item: self._entry.reconcile(*it[1]))
                else:
                    buf.append(item)
            except queue.Empty:
                if stopping:
                    break
            now = time.monotonic()
            if buf and (len(buf) >= FLUSH_BATCH or now - last >= FLUSH_INTERVAL):
                if self._flush(conn, buf):
                    buf.clear()
                    self._entry_call(self._entry.step if self._entry else None)
                    last_entry = now
                elif len(buf) > RETRY_CAP:
                    logger.error("TradeLog 寫入持續失敗，丟棄 %d 筆", len(buf))
                    buf.clear()
                last = now
            elif now - last_entry >= FLUSH_INTERVAL:     # 沒有新資料時也定期處理（重試失敗的成交、等委託紀錄的成交）
                self._entry_call(self._entry.step if self._entry else None)
                last_entry = now
        if buf:
            self._flush(conn, buf)
        self._entry_call(self._entry.step if self._entry else None)
        conn.close()

    def _entry_call(self, fn: Any) -> None:
        if fn is None or self._entry is None:
            return
        try:
            fn()
        except Exception as e:                          # noqa: BLE001
            entry_log.alerts.fire("sync", f"{type(e).__name__}: {e}")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """舊資料庫補欄位（CREATE TABLE IF NOT EXISTS 不會改既有的表）。
        sim、live 兩個行程共用這個檔案，可能同時啟動、同時補欄位——後到的會遇到「欄位已存在」，那不是錯誤，不能讓成交紀錄因此停用。"""
        def cols(t: str) -> set[str]:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}

        def add(table: str, col: str, ddl: str) -> None:
            if col in cols(table):
                return
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

        add("orders", "state_json", "TEXT")
        add("events", "deal_ts", "REAL")
        add("events", "deal_seq", "TEXT")

    def entry_status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, **entry_log.status(self.path, current_mode())}

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

    def round_trips(self, mode: str | None = None, since_ts: float | None = None) -> dict[str, Any]:
        """把成交（以委託為單位的均價）依 FIFO 配對成「進場→出場」的完整交易，還原實際損益。
        pts = 點數、pnl = 元（點 × 口數 × 點值）；**不含手續費與稅**（成交回報沒有這兩項）。
        只處理期貨（TMF/MXF/TXF），選擇權略過；假設紀錄開始時是空手，所以開倉在紀錄之前的部位，
        其平倉會被誤當成新開倉（長期使用影響很小，剛開始記錄的前幾筆要留意）。
        每筆交易帶：平倉單的策略/原因（tp/sl/trail…）與「開倉當時」的市場狀態標記（逐筆績效按狀態拆分用）。"""
        rows = self.orders(mode=mode, limit=1_000_000, since_ts=since_ts)
        fills = [r for r in rows if r["fill_qty"] and r["avg_fill"] and r["contract"] in POINT_VALUE]
        fills.sort(key=lambda r: (r["last_fill_ts"] or r["ts"], r["id"]))
        books: dict[tuple[str, str], deque] = {}
        trips: list[dict[str, Any]] = []
        for r in fills:
            book = books.setdefault((r["mode"], r["contract"]), deque())
            side = 1 if r["action"] == "Buy" else -1
            qty, price = int(r["fill_qty"]), float(r["avg_fill"])
            close_ts = r["last_fill_ts"] or r["ts"]
            while qty > 0 and book and book[0]["side"] != side:          # 先平掉反向的舊部位（FIFO）
                lot = book[0]
                q = min(qty, lot["qty"])
                pts = (price - lot["price"]) * lot["side"]
                trips.append({
                    "mode": r["mode"], "contract": r["contract"], "side": lot["side"], "qty": q,
                    "open_ts": lot["ts"], "ts": close_ts, "entry": lot["price"], "exit": price,
                    "pts": round(pts, 2), "pnl": round(pts * q * POINT_VALUE[r["contract"]], 1),
                    "strategy": r["strategy"], "reason": r["reason"], "open_strategy": lot["strategy"],
                    "market_state": lot["market_state"],
                })
                lot["qty"] -= q
                qty -= q
                if lot["qty"] == 0:
                    book.popleft()
            if qty > 0:                                                  # 剩下的口數是新開（或反手）的部位
                book.append({"side": side, "qty": qty, "price": price, "ts": close_ts,
                             "strategy": r["strategy"], "market_state": r["market_state"]})
        open_lots = [{"mode": k[0], "contract": k[1], "side": lot["side"], "qty": lot["qty"], "price": lot["price"]}
                     for k, book in books.items() for lot in book]
        return {"trips": trips, "open": open_lots, "n_fills": len(fills)}

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
