"""機率範圍帶（update.md 發現 48）：現價 ± q·σ√T 的範圍（50%／80%／95%），q 來自實測表 core/range_calibration.json。純函式、只顯示、不碰下單與策略。

跟 core/stop_suggest.py 用同一個 σ（最近 30 個 1 分鐘收盤價變動的標準差，定義共用 stop_suggest.sigma_from_closes）與同一份「現在是日盤還是夜盤」。
回答的是「會走多遠」，**不是往哪個方向**；也不是進場訊號（沒有驗證過「衝出範圍就順勢」有沒有用，見發現 43）。
  落點範圍：T 分鐘後（時間到時）價格落在這個範圍內的機率＝層級（50／80／95%）
  期間範圍：這 T 分鐘內任何時候的最高／最低都沒超出這個範圍的機率＝層級（停損停利碰不碰到看這個，比落點範圍寬）
  到收盤：日盤才有；剩餘分鐘 R 落在 15~270 之間時，依 R 分四組各有一張表（σ√R）。
格子在樣本外驗證沒通過（涵蓋率與名目差超過容忍）或前後半差太多，標 unstable；夜盤的表只有幾十晚，標 few。"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from core.range_calibration import BUCKETS, HORIZONS, bucket_of
from core.stop_suggest import closes_from_flow, session_of, sigma_from_closes

CAL_PATH = Path(__file__).resolve().parent / "range_calibration.json"
DAY_CLOSE_MIN = 13 * 60 + 45                       # 日盤收盤（分鐘數）
STALE_SEC = 300                                    # 最新成交價超過這麼久就標「舊」
KINDS = ("term", "path")

_cache: dict[str, Any] = {"mtime": None, "table": None}


def load_table(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """讀實測表；檔案換了會重讀；沒有或壞掉回 None（畫面會寫「未校準」，不會亂給數字）。"""
    path = path or CAL_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cache["mtime"] == (str(path), mtime) and _cache["table"] is not None:
        return _cache["table"]
    try:
        t = json.loads(path.read_text(encoding="utf-8"))
        if not (isinstance(t, dict) and t.get("version") == 1 and "day" in t and "night" in t and isinstance(t.get("levels"), list)):
            return None
    except (OSError, ValueError):
        return None
    _cache["mtime"], _cache["table"] = (str(path), mtime), t
    return t


def remaining_minutes(now_ts: float, session: str) -> Optional[int]:
    """日盤收盤（13:45）前還有幾分鐘（以目前這一分鐘的起點算）；夜盤沒有「到收盤」。"""
    if session != "day":
        return None
    lt = time.localtime(now_ts)
    return DAY_CLOSE_MIN - (lt.tm_hour * 60 + lt.tm_min)


def _band(cell: dict[str, Any], levels: Sequence[float], unit: float, price: float, minutes: int, few: bool) -> dict[str, Any]:
    out = []
    for i, L in enumerate(levels):
        row: dict[str, Any] = {"p": int(round(L * 100))}
        for kind in KINDS:
            q = cell[kind][i]
            ho = (cell.get(f"holdout_ok_{kind}") or [None] * len(levels))[i]
            row[kind] = None if q is None else {"d": round(q * unit, 1), "lo": round(price - q * unit, 1), "hi": round(price + q * unit, 1)}
            row[f"unstable_{kind}"] = bool(cell[f"unstable_{kind}"][i] or ho is False)          # 前後半差太多，或樣本外驗證沒通過
        out.append(row)
    return {"minutes": int(minutes), "noise": round(unit, 1), "few": bool(few or cell.get("few")), "levels": out}


def build(price: Optional[float], sigma: Optional[float], n_changes: int, *, session: str, remaining: Optional[int], table: Optional[dict[str, Any]]) -> dict[str, Any]:
    """組出整張範圍帶：每個持有時間一組、日盤再加「到收盤」。price 或 sigma 缺、沒有實測表時只回說明，不給數字。"""
    out: dict[str, Any] = {"price": price, "session": session, "sigma": None if sigma is None else round(sigma, 2), "n_changes": n_changes,
                           "calibrated": table is not None, "remaining": remaining, "bands": {}, "note": None, "cal": None}
    if table is not None:
        h = table.get("holdout") or {}
        out["cal"] = {"generated": table.get("generated"), "day_sessions": table["day"].get("sessions"), "from": table["day"].get("from"), "to": table["day"].get("to"),
                      "night_sessions": table["night"].get("sessions"), "split": h.get("split"), "holdout_ok": h.get("ok"), "holdout_total": h.get("total"),
                      "levels": table["levels"]}
    if table is None:
        out["note"] = "沒有實測表（core/range_calibration.json），不給範圍。"
        return out
    if price is None or not (price > 0):
        out["note"] = "沒有最近的成交價，不給範圍。"
        return out
    if sigma is None or sigma <= 0:
        out["note"] = f"最近 30 分鐘成交太少（有效的 1 分鐘變動只有 {n_changes} 個，要 25 個以上），算不出波動，不給範圍。"
        return out
    sec = table.get(session) or {}
    tab = sec.get("table") or {}
    levels = table["levels"]
    for T in HORIZONS:
        cell = tab.get(str(T))
        if cell is not None:
            out["bands"][str(T)] = _band(cell, levels, sigma * math.sqrt(T), price, T, bool(sec.get("few")))
    key = bucket_of(remaining) if remaining is not None else None
    if key is not None and key in tab:
        out["bands"]["close"] = _band(tab[key], levels, sigma * math.sqrt(remaining), price, remaining, bool(sec.get("few")))
        out["bands"]["close"]["bucket"] = key
    return out


def price_of(rows: Sequence[dict[str, Any]], live_row: Optional[dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    """最新成交價與它所在分鐘的起點：進行中那一分鐘（還沒寫進資料庫）優先，否則 flow_1m 最後一根有收盤價的。"""
    for r in ([live_row] if live_row else []):
        try:
            c = float(r["close"])
            if c > 0:
                return c, float(r["ts"])
        except (KeyError, TypeError, ValueError):
            pass
    best: Optional[tuple[float, float]] = None
    for r in rows:
        try:
            c, t = float(r["close"]), float(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if c > 0 and (best is None or t > best[0]):
            best = (t, c)
    return (None, None) if best is None else (best[1], best[0])


def range_now(rows: Sequence[dict[str, Any]], now_ts: float, *, live_row: Optional[dict[str, Any]] = None, table: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """rows＝flow_1m（TMF 真實成交的 1 分 K）最近一小時；全部是純計算，不碰券商。"""
    lt = time.localtime(now_ts)
    session = session_of(lt.tm_hour, lt.tm_min, lt.tm_wday)
    sigma, n = sigma_from_closes(closes_from_flow(rows, now_ts))
    price, price_ts = price_of(rows, live_row)
    out = build(price, sigma, n, session=session, remaining=remaining_minutes(now_ts, session), table=table)
    age = None if price_ts is None else max(0, int(now_ts - price_ts))
    out.update({"as_of": now_ts, "price_ts": price_ts, "price_age_sec": age, "stale": bool(age is not None and age > STALE_SEC)})
    return out
