"""價量分布（update.md 發現 77）：把逐筆成交依「價格」彙總成每個價位的量（外盤／內盤分開），算 POC 與價值區，給走勢圖畫在右側。
只顯示、不下單、不查券商；策略／風控／下單模組不得引用（tests/test_volume_profile.py 靜態檢查）。

為什麼：K 線加成交量柱只有「時間」和「量」，看不到量是落在哪個價位。把同一批逐筆成交改成依價格彙總，就能看到哪些價位成交特別多（接受）、
哪些價位很快被穿過（成交稀薄）；內外盤分色可以看出那個價位是買方還是賣方主動。
來源：ticks.db（後端記的真實成交，volume>0）。一個「時段」＝日盤（08:45～13:45）或夜盤（開始那天 15:00 ～ 隔天 05:00）。
- POC＝量最大那一格的中心；價值區＝從 POC 開始、每次往量較大的一邊加兩格，直到涵蓋 70% 的量；VAH／VAL＝價值區最上／最下一格的邊緣。
- 前一個日盤（prev）＝目前時段開始之前、最近一個已經收完的日盤（夜盤時看的是同一天剛收的日盤）；兩個時段的合約不同（換月）時用該日盤自己的近月並註明。
- 近月合約（TMFK6 這種）：每個時段取成交量最大的那一檔，不靠「第三個週三」之類的推算。
畫面用的列是最細的（每 1 點一列，前端依畫面高度合併成適合的粗細）；POC 與價值區另外用 pbin（預設 10 點）一格的粗列算。
限制（誠實）：只有成交，沒有每個價位的掛單深度歷史、也沒有成交者身分——看得到量落在哪、哪一邊主動，看不出是誰。同一天小台與大台的 POC 可能差很多，要分開看。"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from core.daytrade import TAIPEI

logger = logging.getLogger(__name__)

PREFIXES = ("TMF", "MXF", "TXF")
VA_FRAC = 0.70
DEFAULT_PBIN = 10                       # POC／價值區用的格寬（點）；和離線檢驗（發現 78）的格寬同一個量級
ROW_BIN = 1                             # 回給畫面的列用的格寬（點）：台指期價格是整數點，這就是最細的；畫面依高度再合併成適合的粗細
LOOKBACK_DAYS = 10                      # 往回找最近有成交的時段、前一個日盤，最多找幾天（連假、颱風假）
SAME_CODE_MIN = 0.20                    # 前一個日盤：目前近月那一檔的量至少是當天最大那一檔的這個比例才沿用（換月前後新月份很薄）
TTL_LIVE, TTL_DONE = 10.0, 3600.0       # 快取秒數：進行中的時段／已收完的時段
MONTH = "ABCDEFGHIJKL"                  # 期貨月份代碼：1 月 A … 12 月 L

_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


# ── 時段 ─────────────────────────────────────────────────────────

def _epoch(d: date, hh: int, mm: int, ss: int = 0) -> float:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=TAIPEI).timestamp()


def window(d: date, session: str) -> tuple[float, float]:
    """時段的 [開始, 結束) epoch 秒：日盤 d 08:45～13:45（收盤那一筆在 13:45:00 之後幾毫秒，所以到 13:45:01 前）；夜盤 d 15:00～隔天 05:00。"""
    if session == "day":
        return _epoch(d, 8, 45), _epoch(d, 13, 45, 1)
    return _epoch(d, 15, 0), _epoch(d + timedelta(days=1), 5, 0, 1)


def label(d: date, session: str) -> str:
    return f"{d.month}/{d.day} {'日盤' if session == 'day' else '夜盤'}"


def current_session(now: datetime) -> tuple[date, str]:
    """現在最該看哪個時段：15:00 以後＝今晚夜盤；08:45～15:00＝今天日盤；其他（凌晨、開盤前）＝昨晚的夜盤。"""
    now = now.astimezone(TAIPEI)
    m = now.hour * 60 + now.minute
    if m >= 15 * 60:
        return now.date(), "night"
    if m >= 8 * 60 + 45:
        return now.date(), "day"
    return now.date() - timedelta(days=1), "night"


def next_boundary(now_ts: float) -> float:
    """下一次「現在最該看的時段」會換的時間（每天 08:45 換成日盤、15:00 換成夜盤）。已收完的結果快取到這裡為止，不然 15:00 夜盤開始了畫面還停在日盤。"""
    now = datetime.fromtimestamp(now_ts, TAIPEI)
    for add in (0, 1):
        d = now.date() + timedelta(days=add)
        for hh, mm in ((8, 45), (15, 0)):
            t = _epoch(d, hh, mm)
            if t > now_ts:
                return t
    return now_ts + TTL_LIVE


def sessions_back(start: tuple[date, str], n: int = LOOKBACK_DAYS * 2) -> list[tuple[date, str]]:
    """從 start 往前（含 start）依序的時段：夜盤 d 在日盤 d 之後。"""
    out: list[tuple[date, str]] = []
    d, s = start
    while len(out) < n:
        out.append((d, s))
        if s == "night":
            s = "day"
        else:
            d, s = d - timedelta(days=1), "night"
    return out


def candidate_codes(prefix: str, d: date) -> list[str]:
    """d 那個月起連續 4 個月的合約代碼（TMF＋月份字母＋年份個位：TMFK6＝2026 年 11 月）。近月一定在裡面。"""
    out, y, m = [], d.year, d.month
    for _ in range(4):
        out.append(f"{prefix}{MONTH[m - 1]}{y % 10}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ── 價量分布與價值區（純函式）────────────────────────────────────

def value_area(vols: Sequence[float], frac: float = VA_FRAC, ref: Optional[int] = None) -> Optional[tuple[int, int, int]]:
    """(POC 的索引, 價值區下緣索引, 上緣索引)；沒有量回 None。
    POC＝量最大的一格（同量取離 ref 近的、再取較低的）；價值區＝從 POC 開始，每次比較「上面兩格」與「下面兩格」的量、往大的那邊加（同量往上），直到 ≥ frac × 總量。"""
    n = len(vols)
    total = float(sum(vols))
    if n == 0 or total <= 0:
        return None
    best = max(vols)
    poc = min((i for i, v in enumerate(vols) if v == best), key=lambda i: (abs(i - ref) if ref is not None else 0, i))
    lo = hi = poc
    acc = float(vols[poc])
    while acc < frac * total and (lo > 0 or hi < n - 1):
        up = sum(vols[hi + 1:hi + 3]) if hi < n - 1 else None
        dn = sum(vols[max(lo - 2, 0):lo]) if lo > 0 else None
        if dn is None or (up is not None and up >= dn):
            step = vols[hi + 1:hi + 3]
            hi += len(step)
        else:
            step = vols[max(lo - 2, 0):lo]
            lo -= len(step)
        acc += float(sum(step))
    return poc, lo, hi


def levels(rows: Sequence[dict[str, Any]], pbin: float, ref_price: Optional[float] = None) -> Optional[dict[str, float]]:
    """rows＝[{"price": 格的下緣, "total": 量}]（可以有空格沒列）→ {"poc": 該格中心, "vah": 價值區最上一格的上緣, "val": 最下一格的下緣}。"""
    if not rows:
        return None
    idx = {round(r["price"] / pbin): r["total"] for r in rows}
    lo_i, hi_i = min(idx), max(idx)
    vols = [idx.get(i, 0.0) for i in range(lo_i, hi_i + 1)]
    ref = None if ref_price is None else int(ref_price // pbin) - lo_i
    va = value_area(vols, VA_FRAC, ref)
    if va is None:
        return None
    poc, lo, hi = va
    return {"poc": (lo_i + poc + 0.5) * pbin, "vah": (lo_i + hi + 1) * pbin, "val": (lo_i + lo) * pbin}


def _n(x: float) -> float:
    """整數口數就回整數（JSON 比較短）；不是整數照舊。"""
    return int(x) if x == int(x) else x


def _row(price: float, buy: float, sell: float, other: float) -> dict[str, Any]:
    return {"price": price, "buy": _n(buy), "sell": _n(sell), "other": _n(other), "total": _n(buy + sell + other)}


def to_rows(raw: Sequence[tuple[int, int, float]], pbin: int) -> list[dict[str, Any]]:
    """[(格索引, tick_type, 量)] → 依價格由低到高的列：{"price": 下緣, "buy": 外盤, "sell": 內盤, "other": 無法判定, "total"}。tick_type 1＝外盤、2＝內盤。"""
    acc: dict[int, list[float]] = {}
    for b, tt, v in raw:
        a = acc.setdefault(int(b), [0.0, 0.0, 0.0])
        a[0 if tt == 1 else 1 if tt == 2 else 2] += float(v)
    return [_row(b * pbin, a[0], a[1], a[2]) for b, a in sorted(acc.items())]


def rebin_rows(rows: Sequence[dict[str, Any]], step: int) -> list[dict[str, Any]]:
    """把細的列合併成 step 點一格（下緣是 step 的倍數）；step 小於等於原本的格寬就等於原樣。"""
    acc: dict[int, list[float]] = {}
    for r in rows:
        a = acc.setdefault(int(r["price"] // step), [0.0, 0.0, 0.0])
        a[0] += r["buy"]
        a[1] += r["sell"]
        a[2] += r["other"]
    return [_row(b * step, a[0], a[1], a[2]) for b, a in sorted(acc.items())]


# ── 讀資料庫（唯讀、同步；呼叫端用 asyncio.to_thread）──────────────

def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def code_volumes(conn: sqlite3.Connection, codes: Sequence[str], t0: float, t1: float) -> list[tuple[str, float]]:
    """時段裡各合約的總量，大的在前；沒成交的合約不列。"""
    q = ",".join("?" * len(codes))
    rows = conn.execute(f"SELECT code, SUM(volume) FROM ticks WHERE code IN ({q}) AND ts>=? AND ts<? AND volume>0 GROUP BY code", (*codes, t0, t1)).fetchall()
    return sorted(((c, float(v)) for c, v in rows if v), key=lambda x: -x[1])


def read_raw(conn: sqlite3.Connection, code: str, t0: float, t1: float, pbin: int) -> list[tuple[int, int, float]]:
    return [(int(b), int(tt), float(v)) for b, tt, v in conn.execute(
        "SELECT CAST(price / ? AS INTEGER) AS b, tick_type, SUM(volume) FROM ticks WHERE code=? AND ts>=? AND ts<? AND volume>0 GROUP BY b, tick_type",
        (float(pbin), code, t0, t1))]


def _edge_prices(conn: sqlite3.Connection, code: str, t0: float, t1: float) -> dict[str, Optional[float]]:
    lo, hi = conn.execute("SELECT MIN(price), MAX(price) FROM ticks WHERE code=? AND ts>=? AND ts<? AND volume>0", (code, t0, t1)).fetchone()
    first = conn.execute("SELECT price FROM ticks WHERE code=? AND ts>=? AND ts<? AND volume>0 ORDER BY ts LIMIT 1", (code, t0, t1)).fetchone()
    last = conn.execute("SELECT price FROM ticks WHERE code=? AND ts>=? AND ts<? AND volume>0 ORDER BY ts DESC LIMIT 1", (code, t0, t1)).fetchone()
    return {"open": first[0] if first else None, "high": hi, "low": lo, "last": last[0] if last else None}


def _summary(rows: list[dict[str, Any]], pbin: int, ref: Optional[float]) -> dict[str, Any]:
    """rows 是細的列（ROW_BIN）；POC 與價值區用 pbin 點一格的粗列算（POC 在最細的一點上會被單一價位的尖峰帶著跑）。"""
    lv = levels(rebin_rows(rows, pbin), pbin, ref) or {}
    buy, sell, other = sum(r["buy"] for r in rows), sum(r["sell"] for r in rows), sum(r["other"] for r in rows)
    return {"total": buy + sell + other, "buy": buy, "sell": sell, "other": other, "poc": lv.get("poc"), "vah": lv.get("vah"), "val": lv.get("val")}


def find_session(conn: sqlite3.Connection, prefix: str, now: datetime, only: Optional[str] = None) -> Optional[tuple[date, str, list[tuple[str, float]]]]:
    """從現在最該看的時段往前，第一個有成交的時段（週末、休市日一路往前找）；only 限定日盤或夜盤。回 (日期, 時段, 各合約量)。"""
    for d, s in sessions_back(current_session(now)):
        if only and s != only:
            continue
        t0, t1 = window(d, s)
        cv = code_volumes(conn, candidate_codes(prefix, d), t0, t1)
        if cv:
            return d, s, cv
    return None


def previous_day_session(conn: sqlite3.Connection, prefix: str, before_ts: float, want_code: Optional[str], pbin: int) -> Optional[dict[str, Any]]:
    """before_ts 之前最近一個已收完的日盤的價位。want_code（目前的近月）在那天夠量就用它，否則用那天自己的近月並註明。"""
    d0 = datetime.fromtimestamp(before_ts, TAIPEI).date()
    for k in range(LOOKBACK_DAYS + 1):
        d = d0 - timedelta(days=k)
        t0, t1 = window(d, "day")
        if t1 > before_ts:
            continue
        cv = code_volumes(conn, candidate_codes(prefix, d), t0, t1)
        if not cv:
            continue
        code, note = cv[0][0], None
        if want_code and want_code != cv[0][0]:
            mine = dict(cv).get(want_code, 0.0)
            if mine >= SAME_CODE_MIN * cv[0][1]:
                code = want_code
            else:
                note = f"換月：這個日盤的近月是 {cv[0][0]}（目前是 {want_code}），價位可能差一個價差"
        rows = to_rows(read_raw(conn, code, t0, t1, ROW_BIN), ROW_BIN)
        e = _edge_prices(conn, code, t0, t1)
        s = _summary(rows, pbin, None)
        return {"date": d.isoformat(), "session": "day", "label": label(d, "day"), "code": code, "note": note, "poc": s["poc"], "vah": s["vah"], "val": s["val"],
                "total": s["total"], "high": e["high"], "low": e["low"], "last": e["last"]}
    return None


# ── 對外 ─────────────────────────────────────────────────────────

def _empty(prefix: str, session: str, pbin: int, now_ts: float, note: str) -> dict[str, Any]:
    return {"code": None, "prefix": prefix, "session": session, "date": None, "label": None, "pbin": pbin, "row_bin": ROW_BIN, "complete": False, "rows": [], "total": 0, "buy": 0, "sell": 0, "other": 0,
            "poc": None, "vah": None, "val": None, "open": None, "high": None, "low": None, "last": None, "prev": None, "note": note, "as_of": now_ts}


def compute(db_path: Path, prefix: str, session: str, pbin: int, day: Optional[date], now_ts: float) -> dict[str, Any]:
    """一個時段的價量分布＋前一個日盤的價位。session＝auto／day／night；day 沒給就用最近有成交的那一個。永遠不丟例外（資料庫不在或讀不到就回空的加原因）。"""
    now = datetime.fromtimestamp(now_ts, TAIPEI)
    try:
        with closing(_connect(db_path)) as conn:
            if day is not None:
                s = "day" if session == "auto" else session
                t0, t1 = window(day, s)
                cv = code_volumes(conn, candidate_codes(prefix, day), t0, t1)
                found = (day, s, cv) if cv else None
            else:
                found = find_session(conn, prefix, now, None if session == "auto" else session)
            if found is None:
                return _empty(prefix, session, pbin, now_ts, f"最近 {LOOKBACK_DAYS} 天沒有找到逐筆成交（資料庫剛建立、後端沒開著，或這個時段沒開盤）")
            d, s, cv = found
            t0, t1 = window(d, s)
            code = cv[0][0]
            rows = to_rows(read_raw(conn, code, t0, t1, ROW_BIN), ROW_BIN)
            e = _edge_prices(conn, code, t0, t1)
            sm = _summary(rows, pbin, None)
            prev = previous_day_session(conn, prefix, t0, code, pbin)
    except (sqlite3.Error, OSError) as ex:
        logger.warning("價量分布讀不到逐筆成交：%r", ex)
        return _empty(prefix, session, pbin, now_ts, "讀不到逐筆成交資料庫（後端還沒開始記，或資料庫正在維護）")
    return {"code": code, "prefix": prefix, "session": s, "date": d.isoformat(), "label": label(d, s), "pbin": pbin, "row_bin": ROW_BIN, "complete": t1 <= now_ts,
            "start": t0, "end": t1, "rows": rows, **sm, **e, "prev": prev, "note": None, "as_of": now_ts}


def get_profile(prefix: str, session: str = "auto", pbin: int = DEFAULT_PBIN, day: Optional[date] = None, now_ts: Optional[float] = None,
                db_path: Optional[Path] = None) -> dict[str, Any]:
    """快取過的 compute：進行中的時段 TTL_LIVE 秒、已收完的 TTL_DONE 秒（畫面每 15 秒問一次，不要每次都掃一個時段的成交）。"""
    import time
    from core import tick_store
    now_ts = time.time() if now_ts is None else now_ts
    path = Path(db_path) if db_path else tick_store.DB_PATH
    key = (str(path), prefix, session, pbin, day)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now_ts:
            return hit[1]
    res = compute(path, prefix, session, pbin, day, now_ts)
    ttl = TTL_LIVE
    if res.get("complete") and res.get("rows"):
        ttl = TTL_DONE if day is not None else min(TTL_DONE, max(next_boundary(now_ts) - now_ts, TTL_LIVE))      # 指定日期的永遠不變；「現在這個」到下一個時段開始為止
    with _lock:
        _cache[key] = (now_ts + ttl, res)
        if len(_cache) > 64:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:32]:
                _cache.pop(k, None)
    return res


def clear_cache() -> None:
    with _lock:
        _cache.clear()
