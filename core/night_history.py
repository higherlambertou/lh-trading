"""夜盤 1 分 K 歷史（update.md 發現 81，研究用）：向券商抓 TXF 夜盤 1 分 K，存進 data/night_bars.db。

為什麼：bars_1m 只有日盤（hurst_study.fetch_history 用 day_session_minutes 把夜盤濾掉了），要回答「夜盤的漂移集中在哪個時段」
（core/night_window_study.py）得有夜盤的分鐘歷史。券商期貨 1 分 K 最早到 2020-03-02。
獨立登入（模擬盤、只讀行情、結束一定 logout），登入前先問後端有幾條連線（core/broker_guard.py）。
資料庫與交易系統無關：策略／風控／下單模組不得引用（tests/test_night_history.py 靜態檢查）；可重新抓，所以不進備份。

夜盤＝某天 15:00 開始、隔天 05:00 結束；date＝開始的那天（週五夜盤是週五的，K 棒落在週六凌晨）。
K 棒時間是「結束」標籤：15:01 是第一根（seq 1）、24:00（標 00:00）是 seq 540、05:00 是最後一根（seq 840）。
用法：python -m core.night_history fetch [--from 2020-03-02] [--until 2026-11-10]；python -m core.night_history status"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from core.hurst_analyzer import ts_to_local

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "night_bars.db"
CODE = "TXF"
EARLIEST = date(2020, 3, 2)                        # 券商期貨 1 分 K 最早的一天
CHUNK_DAYS = 25
NIGHT_BARS = 840                                   # 15:00～05:00 共 14 小時
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_night_1m(
    code TEXT NOT NULL, date TEXT NOT NULL, seq INTEGER NOT NULL, hhmm INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (code, date, seq)
);
"""


def night_seq(mins: int) -> Optional[int]:
    """K 棒結束時間（一天裡的第幾分鐘）→ 夜盤的第幾根（1～840）；不是夜盤回 None。"""
    if mins >= 15 * 60 + 1:
        return mins - 15 * 60                      # 15:01 → 1 … 23:59 → 539
    if mins <= 5 * 60:
        return mins + 24 * 60 - 15 * 60            # 00:00 → 540 … 05:00 → 840
    return None


def night_session_minutes(raw: Mapping[str, Sequence[float]]) -> list[dict[str, Any]]:
    """券商 1 分 K（平行陣列）→ 夜盤的列（升冪）：date（夜盤開始那天）／seq／hhmm（結束時間）／open／high／low／close／volume。價格不完整的丟掉。"""
    out: list[dict[str, Any]] = []
    rows = sorted(zip(raw["ts"], raw["open"], raw["high"], raw["low"], raw["close"], raw["volume"]), key=lambda x: x[0])
    for ts, o, h, l, c, v in rows:
        dt = ts_to_local(ts)
        mins = dt.hour * 60 + dt.minute
        seq = night_seq(mins)
        if seq is None or not (o and h and l and c):
            continue
        d = dt.date() if mins >= 15 * 60 + 1 else dt.date() - timedelta(days=1)
        out.append({"date": d.isoformat(), "seq": seq, "hhmm": dt.hour * 100 + dt.minute, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), "volume": int(v or 0)})
    return out


class NightStore:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=10)

    def upsert(self, code: str, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        data = [(code, r["date"], r["seq"], r["hhmm"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]
        with closing(self._conn()) as c:
            c.executemany("INSERT OR REPLACE INTO bars_night_1m VALUES (?,?,?,?,?,?,?,?,?)", data)
            c.commit()

    def nights(self, code: str = CODE, start: Optional[str] = None, end: Optional[str] = None) -> dict[str, list[tuple[int, float, float, float, float, int]]]:
        """{夜盤日期: [(seq, open, high, low, close, volume)…]}（seq 升冪）。start／end 含。"""
        sql = "SELECT date, seq, open, high, low, close, volume FROM bars_night_1m WHERE code=?"
        args: list[Any] = [code]
        if start:
            sql += " AND date>=?"
            args.append(start)
        if end:
            sql += " AND date<=?"
            args.append(end)
        out: dict[str, list[tuple[int, float, float, float, float, int]]] = {}
        with closing(self._conn()) as c:
            for d, seq, o, h, l, cl, v in c.execute(sql + " ORDER BY date, seq", args):
                out.setdefault(d, []).append((seq, o, h, l, cl, v))
        return out

    def coverage(self, code: str = CODE) -> list[tuple[str, int, int]]:
        """[(年, 夜盤數, K 棒數)]。"""
        with closing(self._conn()) as c:
            return [(y, n, b) for y, n, b in c.execute(
                "SELECT substr(date,1,4), COUNT(DISTINCT date), COUNT(*) FROM bars_night_1m WHERE code=? GROUP BY 1 ORDER BY 1", (code,))]


def fetch_history(start: date = EARLIEST, until: Optional[date] = None, store: Optional[NightStore] = None, pause: float = 1.0,
                  log: Any = print) -> dict[str, int]:
    """向券商抓夜盤 1 分 K（分段，每段 25 天），一段一段寫進資料庫（中斷了已寫的保留；重跑同一範圍是冪等的）。
    用 simulation=True 登入（只讀行情、不下單、不動到正式盤連線），結束一定 logout。回傳 {"chunks", "failed", "rows"}。"""
    from core.broker_guard import room_for_login
    ok, why = room_for_login()                    # 登入「之前」先問後端有幾條券商連線，太多就不登入
    if not ok:
        raise RuntimeError(why)
    log(why)
    import shioaji as sj
    from dotenv import load_dotenv
    load_dotenv()
    store = store or NightStore()
    start = max(start, EARLIEST)
    end = (until or date.today()) + timedelta(days=1)          # 最後一個夜盤的凌晨在隔天
    api = sj.Shioaji(simulation=True)
    api.login(api_key=os.environ["SHIOAJI_API_KEY"], secret_key=os.environ["SHIOAJI_SECRET_KEY"], contracts_timeout=15000)
    stats = {"chunks": 0, "failed": 0, "rows": 0}
    try:
        contract = getattr(getattr(api.Contracts.Futures, CODE), CODE + "R1")
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
            kb = None
            for attempt in (1, 2):
                try:
                    kb = api.kbars(contract=contract, start=cursor.isoformat(), end=chunk_end.isoformat(), timeout=45000)
                    break
                except Exception as e:                                  # 券商資料伺服器偶發逾時：重試一次
                    log(f"  {cursor}~{chunk_end} 第 {attempt} 次失敗：{e!r}")
            if kb is None:
                stats["failed"] += 1
            else:
                raw = {"ts": [int(x) for x in kb.ts], "open": list(kb.Open), "high": list(kb.High),
                       "low": list(kb.Low), "close": list(kb.Close), "volume": list(kb.Volume)}
                rows = night_session_minutes(raw)
                store.upsert(CODE, rows)
                stats["chunks"] += 1
                stats["rows"] += len(rows)
                log(f"  {cursor} ~ {chunk_end}: 夜盤 {len({r['date'] for r in rows})} 晚、{len(rows)} 根")
            cursor = chunk_end + timedelta(days=1)
            if pause:
                time.sleep(pause)
        return stats
    finally:
        try:
            api.logout()
        except Exception:
            pass


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "status"
    if cmd == "status":
        cov = NightStore().coverage()
        if not cov:
            print("還沒有資料：python -m core.night_history fetch")
        for y, n, b in cov:
            print(f"{y}: {n} 晚、{b} 根（平均每晚 {b / n:.0f} 根；滿的是 {NIGHT_BARS}）")
        return 0
    if cmd == "fetch":
        opts = dict(zip(argv[::2], argv[1::2]))
        start = date.fromisoformat(opts["--from"]) if "--from" in opts else EARLIEST
        until = date.fromisoformat(opts["--until"]) if "--until" in opts else None
        t0 = time.time()
        try:
            s = fetch_history(start, until)
        except RuntimeError as e:                                       # 連線數保護：不登入
            print(f"不登入：{e}")
            return 2
        print(f"完成：{s['chunks']} 段、失敗 {s['failed']} 段、{s['rows']} 根（{time.time() - t0:.0f} 秒）")
        return 0 if not s["failed"] else 1
    print("用法：python -m core.night_history status | fetch [--from YYYY-MM-DD] [--until YYYY-MM-DD]")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
