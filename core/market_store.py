"""市場狀態資料庫（SQLite）：日 K 快取、IV 歷史、每日日誌、策略日績效。

跟 tick_store 不同：這裡有「無法重建」的資料（IV 歷史、手動填的判斷依據/備註），
data/ 雖在 .gitignore，請自行備份 data/market_state.db。

sim(8003) / live(8002) 兩個進程共用同一個 db（WAL、每次操作開短連線）：
  - bars_daily / iv_history 是市場資料，兩邊寫同樣的值，互相覆蓋無妨
    （iv_history 手動輸入優先於自動抓取）
  - journal / strategy_day 以 (date, mode) 區分帳戶（mode = sim | live）

所有方法都是同步的；在 event loop 上請用 asyncio.to_thread 呼叫。
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "market_state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_daily(
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume INTEGER, nbars INTEGER,
  PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS bars_1m(
  code TEXT NOT NULL, date TEXT NOT NULL, hhmm INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume INTEGER,
  PRIMARY KEY(code, date, hhmm));
CREATE TABLE IF NOT EXISTS iv_history(
  date TEXT PRIMARY KEY, iv REAL NOT NULL, source TEXT NOT NULL,
  detail TEXT DEFAULT '', updated_at REAL);
CREATE TABLE IF NOT EXISTS journal(
  date TEXT NOT NULL, mode TEXT NOT NULL,
  phase TEXT, computed_at REAL,
  hurst REAL, hurst_z REAL, hurst_state TEXT,
  iv REAL, iv_pct REAL, iv_state TEXT,
  direction INTEGER, market_state TEXT, strategy_hint TEXT, summary_json TEXT,
  range_ratio REAL,
  basis TEXT DEFAULT '', notes TEXT DEFAULT '',
  PRIMARY KEY(date, mode));
CREATE TABLE IF NOT EXISTS strategy_day(
  date TEXT NOT NULL, mode TEXT NOT NULL, strategy TEXT NOT NULL,
  trades INTEGER DEFAULT 0, pnl REAL DEFAULT 0,
  PRIMARY KEY(date, mode, strategy));
CREATE TABLE IF NOT EXISTS flow_1m(
  code TEXT NOT NULL, ts INTEGER NOT NULL,
  buy_n INTEGER NOT NULL, sell_n INTEGER NOT NULL, unk_n INTEGER NOT NULL,
  buy_vol INTEGER NOT NULL, sell_vol INTEGER NOT NULL, unk_vol INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, source TEXT DEFAULT 'live',
  PRIMARY KEY(code, ts));
CREATE TABLE IF NOT EXISTS indicator_daily(
  date TEXT PRIMARY KEY,
  hurst REAL, hurst_z REAL, hurst_state TEXT, direction INTEGER,
  iv REAL, iv_pct REAL, iv_state TEXT, updated_at REAL);
"""

_MARKET_COLS = ("phase", "computed_at", "hurst", "hurst_z", "hurst_state", "iv", "iv_pct",
                "iv_state", "direction", "market_state", "strategy_hint", "summary_json")


def result_label(trades: int, pnl: float) -> str:
    if not trades:
        return "未進場"
    return "獲利" if pnl > 0 else "虧損" if pnl < 0 else "持平"


class MarketStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.getenv("MARKET_DB_PATH") or DEFAULT_PATH)
        self._ready = False

    def _conn(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        if not self._ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._ready = True
        return conn

    # ── 日 K 快取 ─────────────────────────────────────────────────
    def upsert_bars(self, code: str, bars: list[dict[str, Any]]) -> None:
        if not bars:
            return
        rows = [(code, b["date"], b["open"], b["high"], b["low"], b["close"],
                 b["volume"], b.get("nbars", 0)) for b in bars]
        with closing(self._conn()) as c:
            c.executemany("INSERT OR REPLACE INTO bars_daily VALUES (?,?,?,?,?,?,?,?)", rows)
            c.commit()

    def bars(self, code: str, limit: int, before: str | None = None) -> list[dict[str, Any]]:
        """最近 limit 根日 K（升冪）；before 給定時只取「日期 < before」。"""
        sql = "SELECT * FROM bars_daily WHERE code=?"
        args: list[Any] = [code]
        if before:
            sql += " AND date < ?"
            args.append(before)
        sql += " ORDER BY date DESC LIMIT ?"
        args.append(limit)
        with closing(self._conn()) as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count_bars(self, code: str) -> int:
        with closing(self._conn()) as c:
            return c.execute("SELECT COUNT(*) FROM bars_daily WHERE code=?", (code,)).fetchone()[0]

    def last_bar_date(self, code: str) -> str | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT MAX(date) FROM bars_daily WHERE code=?", (code,)).fetchone()
        return row[0]

    # ── 日盤 1 分 K（Hurst 日內研究、停損驗證等用；日盤 300 根/天，約 2.5 萬列/百日）──
    def upsert_bars_1m(self, code: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        data = [(code, r["date"], r["hhmm"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]
        with closing(self._conn()) as c:
            c.executemany("INSERT OR REPLACE INTO bars_1m VALUES (?,?,?,?,?,?,?,?)", data)
            c.commit()

    def days_1m(self, code: str) -> int:
        with closing(self._conn()) as c:
            return c.execute("SELECT COUNT(DISTINCT date) FROM bars_1m WHERE code=?", (code,)).fetchone()[0]

    def bars_1m(self, code: str, since: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM bars_1m WHERE code=?", [code]
        if since:
            sql += " AND date >= ?"
            args.append(since)
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY date, hhmm", args).fetchall()]

    # ── 逐分鐘外/內盤成交統計（flow_store 即時寫入、indicator_history 回補）────────────
    def upsert_flow_1m(self, code: str, rows: list[dict[str, Any]], source: str = "live") -> None:
        """每列＝這個合約這一分鐘（ts＝分鐘起點的真實 epoch 秒）的買/賣/不明 筆數與口數、成交價 OHLC。
        同一分鐘已有資料時，只在新資料的成交筆數較多（較完整）時才覆蓋——live 與回補、sim 與 live 兩個進程互相寫，
        也不會把完整的一分鐘蓋成殘缺的（例如進程在那分鐘中途才啟動）。"""
        if not rows:
            return
        data = [(code, int(r["ts"]), r["buy_n"], r["sell_n"], r["unk_n"], r["buy_vol"], r["sell_vol"], r["unk_vol"],
                 r["open"], r["high"], r["low"], r["close"], r.get("source", source)) for r in rows]
        with closing(self._conn()) as c:
            c.executemany(
                """INSERT INTO flow_1m (code, ts, buy_n, sell_n, unk_n, buy_vol, sell_vol, unk_vol, open, high, low, close, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code, ts) DO UPDATE SET
                     buy_n=excluded.buy_n, sell_n=excluded.sell_n, unk_n=excluded.unk_n,
                     buy_vol=excluded.buy_vol, sell_vol=excluded.sell_vol, unk_vol=excluded.unk_vol,
                     open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, source=excluded.source
                   WHERE excluded.buy_n + excluded.sell_n + excluded.unk_n
                         > flow_1m.buy_n + flow_1m.sell_n + flow_1m.unk_n""", data)
            c.commit()

    def flow_1m(self, code: str, since_ts: float | None = None, until_ts: float | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM flow_1m WHERE code=?", [code]
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(int(since_ts))
        if until_ts is not None:
            sql += " AND ts < ?"
            args.append(int(until_ts))
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY ts", args).fetchall()]

    def flow_1m_span(self, code: str) -> dict[str, Any]:
        with closing(self._conn()) as c:
            n, lo, hi = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM flow_1m WHERE code=?", (code,)).fetchone()
        return {"n": n, "first_ts": lo, "last_ts": hi}

    def flow_days(self, code: str, min_minutes: int = 200) -> set[str]:
        """日盤（08:45~13:44）至少有 min_minutes 分鐘資料的日期（台灣當地日期）。回補時用來跳過已經抓過的日子。"""
        with closing(self._conn()) as c:
            rows = c.execute(
                """SELECT date(ts,'unixepoch','localtime') d, COUNT(*) n FROM flow_1m
                   WHERE code=? AND strftime('%H%M', ts,'unixepoch','localtime') BETWEEN '0845' AND '1344'
                   GROUP BY d""", (code,)).fetchall()
        return {r["d"] for r in rows if r["n"] >= min_minutes}

    # ── 每日指標歷史（Hurst／日K方向／IV 百分位；indicator_history 建立）──────────────
    def upsert_indicator_daily(self, rows: list[dict[str, Any]]) -> None:
        """date ＝「用到這天收盤為止的資料」算出來的判斷（as-of）。T 日盤前能用的是 date < T 的最後一列——回放時別用到未來。"""
        if not rows:
            return
        now = time.time()
        data = [(r["date"], r.get("hurst"), r.get("hurst_z"), r.get("hurst_state"), r.get("direction"),
                 r.get("iv"), r.get("iv_pct"), r.get("iv_state"), now) for r in rows]
        with closing(self._conn()) as c:
            c.executemany("INSERT OR REPLACE INTO indicator_daily VALUES (?,?,?,?,?,?,?,?,?)", data)
            c.commit()

    def indicator_daily(self, since: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM indicator_daily", []
        if since:
            sql += " WHERE date >= ?"
            args.append(since)
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY date", args).fetchall()]

    def iv_series(self) -> list[tuple[str, float]]:
        """全部 IV 歷史，舊 → 新（date, iv%）。"""
        with closing(self._conn()) as c:
            return [(r["date"], r["iv"]) for r in c.execute("SELECT date, iv FROM iv_history ORDER BY date").fetchall()]

    # ── IV 歷史（單位：%）─────────────────────────────────────────
    def upsert_iv(self, date: str, iv: float, source: str, detail: str = "") -> bool:
        """寫入某日 IV。手動輸入優先：已有手動值時，自動抓取不覆蓋。回傳是否寫入。"""
        with closing(self._conn()) as c:
            cur = c.execute(
                """INSERT INTO iv_history(date, iv, source, detail, updated_at) VALUES (?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET iv=excluded.iv, source=excluded.source,
                     detail=excluded.detail, updated_at=excluded.updated_at
                   WHERE iv_history.source != 'manual' OR excluded.source = 'manual'""",
                (date, float(iv), source, detail, time.time()),
            )
            c.commit()
            return cur.rowcount > 0

    def iv_on(self, date: str) -> dict[str, Any] | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM iv_history WHERE date=?", (date,)).fetchone()
        return dict(row) if row else None

    def iv_latest(self, upto: str, max_age_days: int = 5) -> dict[str, Any] | None:
        """日期 <= upto 的最新一筆，且不超過 max_age_days 天前。"""
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT * FROM iv_history WHERE date<=? AND date>=date(?, ?) ORDER BY date DESC LIMIT 1",
                (upto, upto, f"-{max_age_days} day"),
            ).fetchone()
        return dict(row) if row else None

    def iv_history(self, before: str, limit: int = 252) -> list[float]:
        """日期 < before 的最近 limit 筆 IV（%）。"""
        with closing(self._conn()) as c:
            rows = c.execute(
                "SELECT iv FROM iv_history WHERE date < ? ORDER BY date DESC LIMIT ?", (before, limit)
            ).fetchall()
        return [r[0] for r in rows]

    # ── 每日日誌 ──────────────────────────────────────────────────
    def upsert_journal(self, date: str, mode: str, fields: dict[str, Any]) -> None:
        """寫入市場判斷欄位；不動使用者填的 basis/notes 與 range_ratio。"""
        vals = [fields.get(k) for k in _MARKET_COLS]
        cols = ",".join(_MARKET_COLS)
        qs = ",".join("?" * len(_MARKET_COLS))
        upd = ",".join(f"{k}=excluded.{k}" for k in _MARKET_COLS)
        with closing(self._conn()) as c:
            c.execute(
                f"INSERT INTO journal(date, mode, {cols}) VALUES (?,?,{qs}) "
                f"ON CONFLICT(date, mode) DO UPDATE SET {upd}",
                [date, mode, *vals],
            )
            c.commit()

    def get_journal(self, date: str, mode: str) -> dict[str, Any] | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM journal WHERE date=? AND mode=?", (date, mode)).fetchone()
        return dict(row) if row else None

    def latest_journal(self, mode: str) -> dict[str, Any] | None:
        """最近一筆「有市場判斷」的日誌。"""
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT * FROM journal WHERE mode=? AND summary_json IS NOT NULL "
                "ORDER BY date DESC LIMIT 1", (mode,)
            ).fetchone()
        return dict(row) if row else None

    def set_range_ratio(self, date: str, mode: str, ratio: float) -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT OR IGNORE INTO journal(date, mode) VALUES (?,?)", (date, mode))
            c.execute("UPDATE journal SET range_ratio=? WHERE date=? AND mode=?", (ratio, date, mode))
            c.commit()

    def set_note(self, date: str, mode: str, basis: str | None, notes: str | None) -> bool:
        sets, args = [], []
        if basis is not None:
            sets.append("basis=?")
            args.append(basis)
        if notes is not None:
            sets.append("notes=?")
            args.append(notes)
        if not sets:
            return False
        with closing(self._conn()) as c:
            cur = c.execute(f"UPDATE journal SET {','.join(sets)} WHERE date=? AND mode=?",
                            [*args, date, mode])
            c.commit()
            return cur.rowcount > 0

    # ── 策略日績效 ────────────────────────────────────────────────
    def add_strategy_day(self, date: str, mode: str, strategy: str,
                         trades_delta: int = 0, pnl_delta: float = 0.0) -> None:
        """累加某策略當日的進場次數與已實現損益；列的存在即代表「當天有開過」。"""
        with closing(self._conn()) as c:
            c.execute("INSERT OR IGNORE INTO journal(date, mode) VALUES (?,?)", (date, mode))
            c.execute(
                """INSERT INTO strategy_day(date, mode, strategy, trades, pnl) VALUES (?,?,?,?,?)
                   ON CONFLICT(date, mode, strategy) DO UPDATE SET
                     trades = trades + excluded.trades, pnl = pnl + excluded.pnl""",
                (date, mode, strategy, trades_delta, pnl_delta),
            )
            c.commit()

    def list_journal(self, mode: str, limit: int = 30) -> list[dict[str, Any]]:
        """日誌列（新→舊），附當日各策略彙總與結果（summary_json 不回傳，要看完整判斷用 /state）。"""
        with closing(self._conn()) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM journal WHERE mode=? ORDER BY date DESC LIMIT ?", (mode, limit)
            ).fetchall()]
            days = {}
            for r in c.execute(
                "SELECT date, strategy, trades, pnl FROM strategy_day WHERE mode=?", (mode,)
            ).fetchall():
                days.setdefault(r["date"], []).append(dict(r))
        for row in rows:
            sd = days.get(row["date"], [])
            row["strategies"] = sd
            row["trades"] = sum(s["trades"] for s in sd)
            row["pnl"] = sum(s["pnl"] for s in sd)
            row["scalp_on"] = any(s["strategy"] == "scalp" for s in sd)
            row["result"] = result_label(row["trades"], row["pnl"])
            row.pop("summary_json", None)
        return rows

    # ── 統計（驗證目標）──────────────────────────────────────────
    def stats(self, mode: str, strategy: str = "scalp", big_move: float = 1.5) -> dict[str, Any]:
        """by_state：各市場狀態下 strategy 的勝率/賺賠比（只算有進場的日子）。
        by_iv：各 IV 狀態下，當日振幅 / 近 20 日平均振幅 ≥ big_move 的比例
        （回答「IV 低的日子有沒有等到大波動」）。"""
        with closing(self._conn()) as c:
            journal = [dict(r) for r in c.execute(
                "SELECT date, hurst_state, iv_state, market_state, range_ratio FROM journal WHERE mode=?",
                (mode,)).fetchall()]
            sd = {r["date"]: dict(r) for r in c.execute(
                "SELECT date, trades, pnl FROM strategy_day WHERE mode=? AND strategy=?",
                (mode, strategy)).fetchall()}

        by_state: dict[str, dict[str, Any]] = {}
        by_iv: dict[str, dict[str, Any]] = {}
        for j in journal:
            st = j["market_state"] or "—"
            ran = sd.get(j["date"])
            if ran and ran["trades"] > 0:
                g = by_state.setdefault(st, {"state": st, "days": 0, "wins": 0, "losses": 0,
                                             "win_pnl": 0.0, "loss_pnl": 0.0})
                g["days"] += 1
                if ran["pnl"] > 0:
                    g["wins"] += 1
                    g["win_pnl"] += ran["pnl"]
                elif ran["pnl"] < 0:
                    g["losses"] += 1
                    g["loss_pnl"] += ran["pnl"]
            if j["range_ratio"] is not None:
                iv_st = j["iv_state"] or "UNKNOWN"
                g = by_iv.setdefault(iv_st, {"iv_state": iv_st, "days": 0, "big": 0, "ratio_sum": 0.0})
                g["days"] += 1
                g["ratio_sum"] += j["range_ratio"]
                g["big"] += 1 if j["range_ratio"] >= big_move else 0

        out_state = []
        for g in by_state.values():
            decided = g["wins"] + g["losses"]
            avg_win = g["win_pnl"] / g["wins"] if g["wins"] else 0.0
            avg_loss = g["loss_pnl"] / g["losses"] if g["losses"] else 0.0
            out_state.append({
                "state": g["state"], "days": g["days"], "wins": g["wins"], "losses": g["losses"],
                "win_rate": round(g["wins"] / decided, 3) if decided else None,
                "avg_win": round(avg_win, 1), "avg_loss": round(avg_loss, 1),
                "payoff": round(avg_win / abs(avg_loss), 2) if avg_loss else None,
                "total_pnl": round(g["win_pnl"] + g["loss_pnl"], 1),
            })
        out_iv = [{
            "iv_state": g["iv_state"], "days": g["days"],
            "big_move_days": g["big"], "big_move_rate": round(g["big"] / g["days"], 3),
            "avg_range_ratio": round(g["ratio_sum"] / g["days"], 2),
        } for g in by_iv.values()]
        return {"strategy": strategy, "big_move": big_move,
                "by_state": sorted(out_state, key=lambda x: x["state"]),
                "by_iv": sorted(out_iv, key=lambda x: x["iv_state"])}
