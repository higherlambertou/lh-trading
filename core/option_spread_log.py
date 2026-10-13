"""選擇權買賣價差的時段紀錄（update.md 發現 30）：每隔幾分鐘向券商查一次月選擇權的鏈，把「各履約價離平值多遠」的買賣價差彙總存起來。

目的：回答《選擇權波段策略》明列「需要驗證」的夜盤流動性——日盤／夜盤、一天中的哪個時段、近月／次月、ATM 還是價外，進出一趟要付多少。
只量了一個時間點（update.md 發現 29）不夠；累積幾天後 `python -m core.option_spread_log report` 就看得出差異。
（歷史的收盤價差另外用期交所的檔案量，見 core/option_spread_study；那是「每天收盤那一刻」，這裡是「盤中各個時間點」。）

純記錄、不下單、任何錯誤都只記 log、不影響其他功能。每次取樣向券商查 2 次整個到期月的鏈（各 1~2 次 api.snapshots，約 0.3 秒），預設每 10 分鐘一次，
遠低於行情查詢的額度（5 秒 50 次）。取樣時間對齊時鐘（:00、:10、:20…），不同天的同一個時段才好比。
環境變數：OPTION_SPREAD_LOG=false 關閉；OPTION_SPREAD_MINUTES 取樣間隔（預設 10）。資料：data/option_spread.db（已被 .gitignore 的 data/ 涵蓋、不在每日備份裡；可以重新累積）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import statistics
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from core import options_calc as oc

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "option_spread.db"
DEFAULT_MINUTES = 10
EXPIRIES = 2                                  # 近月與次月
GUARD_SEC = 60                                # 剛開盤的前 60 秒不取樣（試撮與第一批報價很亂，不代表「開著的」流動性）
BUCKETS = (("ATM", 0.005), ("NEAR", 0.02), ("MID", 0.05), ("FAR", 0.09))      # 履約價離遠期價的比例上限（含）
BUCKET_TEXT = {"ATM": "平值 ±0.5%", "NEAR": "0.5~2%", "MID": "2~5%", "FAR": "5~9%", "STRADDLE": "ATM 跨式"}
COLUMNS = ("ts", "session", "expiry", "dte", "F", "atm_iv", "bucket", "n_two", "n_total", "med_pts", "med_pct", "p90_pct")


def sampling_session(now: datetime) -> Optional[str]:
    """現在能不能取樣、是哪個時段：day（週一～五 08:45~13:45）、night（15:00~隔天 05:00；週五夜盤延伸到週六凌晨，週日沒有夜盤）。
    開盤後 GUARD_SEC 秒內、休市、週末都回 None。不含國定假日（沒有交易日曆；假日取不到報價時取樣會自己失敗並略過）。"""
    wd, hm = now.weekday(), now.hour * 60 + now.minute
    sec = hm * 60 + now.second
    if wd < 5 and 8 * 60 + 45 <= hm <= 13 * 60 + 45 and sec >= (8 * 60 + 45) * 60 + GUARD_SEC:
        return "day"
    if wd < 5 and hm >= 15 * 60 and sec >= 15 * 3600 + GUARD_SEC:
        return "night"
    if hm < 5 * 60 and 1 <= wd <= 5:                    # 週二～週六凌晨：前一天（週一～五）的夜盤
        return "night"
    return None


def next_boundary(now: datetime, minutes: int) -> datetime:
    """下一個對齊時鐘的取樣時間（例如間隔 10 分鐘 → :00、:10、:20…）；剛好在邊界上就等下一個。"""
    base = now.replace(second=0, microsecond=0)
    nxt = base + timedelta(minutes=minutes - (base.hour * 60 + base.minute) % minutes)
    return nxt


# ── 彙總一條鏈 ────────────────────────────────────────────────────

def summarize_chain(chain: dict[str, Any]) -> list[dict[str, Any]]:
    """選擇權鏈（oc.build_chain 的結果）→ 各「離平值多遠」桶的價差彙總，加上 ATM 跨式（同一履約價買賣權價差相加）。
    只算買賣兩邊都有報價（bid>0 且 ask>=bid）的檔；n_total＝這個桶裡有列出的檔（買權、賣權各算一檔）。"""
    F = chain["F"]
    rows: dict[str, dict[str, list]] = {b: {"pts": [], "pct": [], "total": []} for b, _ in BUCKETS}
    for row in chain["rows"]:
        dist = abs(row["strike"] - F) / F
        bucket = next((b for b, hi in BUCKETS if dist <= hi), None)
        if bucket is None:
            continue
        for side in ("call", "put"):
            q = row[side]
            if q is None:
                continue
            rows[bucket]["total"].append(1)
            if q["spread"] is not None and q["mid"] > 0:
                rows[bucket]["pts"].append(q["spread"])
                rows[bucket]["pct"].append(q["spread"] / q["mid"])
    out = []
    for b, _ in BUCKETS:
        pts, pct = rows[b]["pts"], rows[b]["pct"]
        out.append({"bucket": b, "n_two": len(pts), "n_total": len(rows[b]["total"]),
                    "med_pts": round(statistics.median(pts), 2) if pts else None,
                    "med_pct": round(statistics.median(pct), 4) if pct else None,
                    "p90_pct": round(sorted(pct)[min(int(0.9 * len(pct)), len(pct) - 1)], 4) if pct else None})
    both = [r for r in chain["rows"] if r["call"] and r["put"] and r["call"]["spread"] is not None and r["put"]["spread"] is not None
            and r["call"]["mid"] > 0 and r["put"]["mid"] > 0]
    if both:
        r = min(both, key=lambda x: abs(x["strike"] - F))
        sp, mid = r["call"]["spread"] + r["put"]["spread"], r["call"]["mid"] + r["put"]["mid"]
        out.append({"bucket": "STRADDLE", "n_two": 1, "n_total": 1, "med_pts": round(sp, 2), "med_pct": round(sp / mid, 4), "p90_pct": round(sp / mid, 4)})
    return out


# ── 儲存 ──────────────────────────────────────────────────────────

class SpreadStore:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self._ready = False

    def _conn(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        if not self._ready:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS option_spread (
                ts INTEGER NOT NULL, session TEXT NOT NULL, expiry TEXT NOT NULL, dte REAL, F REAL, atm_iv REAL, bucket TEXT NOT NULL,
                n_two INTEGER, n_total INTEGER, med_pts REAL, med_pct REAL, p90_pct REAL, PRIMARY KEY (ts, expiry, bucket))""")
            c.commit()
            self._ready = True
        return c

    def insert(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with closing(self._conn()) as c:
            c.executemany(f"INSERT OR REPLACE INTO option_spread ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                          [tuple(r[k] for k in COLUMNS) for r in rows])
            c.commit()
        return len(rows)

    def rows(self, since_ts: int = 0) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute("SELECT * FROM option_spread WHERE ts >= ? ORDER BY ts, expiry, bucket", (since_ts,)).fetchall()]

    def count(self) -> int:
        with closing(self._conn()) as c:
            return c.execute("SELECT COUNT(*) FROM option_spread").fetchone()[0]


def snapshot_rows(chain: dict[str, Any], expiry: str, session: str, ts: int) -> list[dict[str, Any]]:
    base = {"ts": ts, "session": session, "expiry": expiry, "dte": round(chain["T"] * oc.YEAR_DAYS, 3), "F": round(chain["F"], 1),
            "atm_iv": round(chain["atm_iv"] * 100, 2) if chain["atm_iv"] else None}
    return [{**base, **s} for s in summarize_chain(chain)]


# ── 取樣 ──────────────────────────────────────────────────────────

async def sample_once(broker: Any, under: Optional[float], store: SpreadStore, now: datetime, session: str, category: str = "TXO") -> int:
    """取一次樣：近月與次月各查一整條鏈、彙總、存起來。回傳存了幾列。單一到期月失敗不影響另一個。"""
    from api.routes_options import parse_expiry          # 延後匯入：這個模組本身不依賴 FastAPI
    months = sorted(m for m in await broker.option_expiries(category) if m.isdigit() and len(m) == 6)
    ts, saved, taken = int(now.timestamp()), 0, 0
    for m in months:
        if taken >= EXPIRIES:
            break
        try:
            raw = await broker.option_chain(m, category, center=under)
            expiry = parse_expiry(raw.get("delivery_date", ""), m)
            T = (expiry - now).total_seconds() / (oc.YEAR_DAYS * 86400)
            if T <= 0:
                continue                                   # 已經到期的月份（到期當天收盤後還列著）不算，也不佔名額
            chain = oc.build_chain(raw.get("quotes") or [], T, hint=under)
            saved += store.insert(snapshot_rows(chain, m, session, ts))
            taken += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:                             # noqa: BLE001
            logger.warning("選擇權價差取樣失敗（%s）：%s", m, e)
            taken += 1                                     # 失敗也佔名額：不然近月壞掉會一路試到很遠的月份
    return saved


async def spread_log_loop(broker: Any, quote_hub: Any, minutes: Optional[int] = None, store: Optional[SpreadStore] = None,
                          now_fn: Callable[[], datetime] = datetime.now, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
    """背景迴圈（main.py 啟動）：每到取樣時間，在交易時段內取一次樣。任何例外只記 log，不會結束迴圈。"""
    minutes = minutes or int(os.getenv("OPTION_SPREAD_MINUTES", str(DEFAULT_MINUTES)))
    store = store or SpreadStore()
    logger.info("選擇權價差紀錄已啟動：每 %d 分鐘一次 → %s", minutes, store.path)
    while True:
        now = now_fn()
        await sleep(max((next_boundary(now, minutes) - now).total_seconds() + 1.0, 1.0))      # +1 秒：睡醒時一定已經過了邊界
        now = now_fn()
        session = sampling_session(now)
        if not session:
            continue
        try:
            await sample_once(broker, quote_hub.last_price_by_prefix("TXF"), store, now, session)
        except asyncio.CancelledError:
            raise
        except RuntimeError as e:                          # 券商還沒連線之類：不是錯，下一輪再試
            logger.debug("選擇權價差取樣略過：%s", e)
        except Exception:                                  # noqa: BLE001
            logger.warning("選擇權價差取樣失敗", exc_info=True)


# ── 報表 ──────────────────────────────────────────────────────────

def report(rows: list[dict[str, Any]]) -> str:
    """依「時段 × 離平值多遠」彙總：各取樣時間點的中位價差（佔中價 %）再取中位數；另外列日盤／夜盤各小時的 ATM 跨式價差。"""
    if not rows:
        return "還沒有資料（後端要開著、而且在交易時段內才會取樣）"
    by: dict[tuple[str, str], list[float]] = {}
    pts: dict[tuple[str, str], list[float]] = {}
    hourly: dict[tuple[str, int], list[float]] = {}
    days = {time.strftime("%Y-%m-%d", time.localtime(r["ts"])) for r in rows}
    for r in rows:
        if r["med_pct"] is None:
            continue
        by.setdefault((r["session"], r["bucket"]), []).append(r["med_pct"])
        pts.setdefault((r["session"], r["bucket"]), []).append(r["med_pts"])
        if r["bucket"] == "STRADDLE":
            hourly.setdefault((r["session"], time.localtime(r["ts"]).tm_hour), []).append(r["med_pct"])
    lines = [f"取樣 {len({r['ts'] for r in rows})} 次、{len(days)} 天（{min(days)} ~ {max(days)}）", "",
             "時段   離平值        取樣數  中位價差(點)  中位價差(佔中價)"]
    for sess in ("day", "night"):
        for b in ("STRADDLE", "ATM", "NEAR", "MID", "FAR"):
            v = by.get((sess, b))
            if v:
                lines.append(f"{'日盤' if sess == 'day' else '夜盤'}   {BUCKET_TEXT[b]:<10}  {len(v):>6}  {statistics.median(pts[(sess, b)]):>10.1f}  {statistics.median(v) * 100:>14.1f}%")
    lines += ["", "ATM 跨式買賣價差（佔中價）依小時："]
    for (sess, h), v in sorted(hourly.items(), key=lambda x: (x[0][0] != "day", (x[0][1] + 24 - 15) % 24 if x[0][0] == "night" else x[0][1])):
        lines.append(f"  {'日盤' if sess == 'day' else '夜盤'} {h:02d} 點：{statistics.median(v) * 100:.1f}%（{len(v)} 次）")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="python -m core.option_spread_log", description="選擇權買賣價差的時段紀錄")
    ap.add_argument("what", choices=("report", "count"))
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--days", type=int, default=0, help="只看最近幾天（預設全部）")
    a = ap.parse_args(argv)
    st = SpreadStore(a.db)
    if a.what == "count":
        print(st.count())
        return 0
    since = int(time.time() - a.days * 86400) if a.days else 0
    print(report(st.rows(since)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
