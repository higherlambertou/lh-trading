"""預測紀錄與命中率（update.md 發現 68）：只記錄、只顯示，策略／風控／下單都不讀。

開盤前預期日盤振幅（發現 55）與夜盤預期振幅（發現 67）上線之後，唯一可信的檢驗是**往後看**：每天把預測存一筆
（第一次算出來的那一筆；資料庫觸發器擋 UPDATE／DELETE，存了不改），收盤後對照實際的高低差，算「落在 68% 區間內的比例」
（模型說的是 68%；長期偏離很多就代表模型要重新檢討）與「實際 ÷ 中位數」的中位數（偏大或偏小）。
  日盤：target＝那個交易日；預測的輸入全部早於當天開盤（前一天的臺指 VIX、近 20 日振幅），所以盤中才記到也不算偷看。
        實際＝bars_1m 那天日盤的最高 − 最低（與研究同一個定義；不滿 280 根就當資料不完整、不算）。
  夜盤：target＝夜盤開始的那個交易日（15:00 開盤）；實際＝期交所 TX 日行情裡它之後那一列盤後（盤後成交量最大的月合約）的高低差——
        隔天 15:10 期交所資料更新後才有。
只有正式盤跑（共用 market_state.db；模擬盤不重複記）；FORECAST_LOG=false 關閉。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from core.daytrade import TAIPEI

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TX_DIR = ROOT / "data" / "taifex" / "tx"
POLL_SEC = 600.0
MIN_DAY_BARS = 280
HIT_N = 20
MIN_SHOW = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_log(
  kind TEXT NOT NULL, target TEXT NOT NULL, created_at REAL NOT NULL,
  lo REAL, mid REAL, hi REAL, inputs TEXT,
  PRIMARY KEY(kind, target));
CREATE TRIGGER IF NOT EXISTS forecast_log_no_update BEFORE UPDATE ON forecast_log BEGIN SELECT RAISE(ABORT, 'forecast_log 只新增，不修改'); END;
CREATE TRIGGER IF NOT EXISTS forecast_log_no_delete BEFORE DELETE ON forecast_log BEGIN SELECT RAISE(ABORT, 'forecast_log 只新增，不刪除'); END;
CREATE TABLE IF NOT EXISTS forecast_outcome(
  kind TEXT NOT NULL, target TEXT NOT NULL, realized REAL NOT NULL, source TEXT, filled_at REAL,
  PRIMARY KEY(kind, target));
"""


def enabled() -> bool:
    return os.getenv("FORECAST_LOG", "true").lower() != "false" and os.getenv("SIMULATION", "false").lower() != "true"


def _conn(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=5)
    c.executescript(SCHEMA)
    return c


# ── 記錄（同步；在執行緒裡）────────────────────────────────────────

def log_forecast(path: Path, kind: str, target: str, expect: dict[str, float], inputs: dict[str, Any], now: Optional[float] = None) -> bool:
    """存一筆預測；同一個 (kind, target) 已經有了就不動（回 False）。"""
    if not expect or not all(k in expect for k in ("lo", "mid", "hi")):
        return False
    with closing(_conn(path)) as c:
        cur = c.execute("INSERT OR IGNORE INTO forecast_log VALUES (?,?,?,?,?,?,?)",
                        (kind, target, time.time() if now is None else now, float(expect["lo"]), float(expect["mid"]), float(expect["hi"]),
                         json.dumps(inputs, ensure_ascii=False, default=str)))
        c.commit()
        return cur.rowcount > 0


def pending(path: Path) -> list[tuple[str, str]]:
    with closing(_conn(path)) as c:
        return [(k, t) for k, t in c.execute("SELECT f.kind, f.target FROM forecast_log f LEFT JOIN forecast_outcome o ON o.kind=f.kind AND o.target=f.target "
                                               "WHERE o.target IS NULL ORDER BY f.target")]


def save_outcome(path: Path, kind: str, target: str, realized: float, source: str) -> None:
    with closing(_conn(path)) as c:
        c.execute("INSERT OR REPLACE INTO forecast_outcome VALUES (?,?,?,?,?)", (kind, target, float(realized), source, time.time()))
        c.commit()


# ── 實際的高低差 ─────────────────────────────────────────────────

def realized_day(bars: list[dict[str, Any]], target: str) -> Optional[float]:
    """bars_1m（TXF）那天日盤的最高 − 最低；不滿 MIN_DAY_BARS 根回 None。"""
    rows = [b for b in bars if b.get("date") == target and b.get("high") and b.get("low")]
    if len(rows) < MIN_DAY_BARS:
        return None
    return float(max(b["high"] for b in rows) - min(b["low"] for b in rows))


def realized_night(tx_dir: Path, prev_day: str) -> Optional[float]:
    """期交所 TX 日行情：prev_day 之後第一個盤後列（盤後成交量最大的月合約）的高低差；還沒有回 None。只讀那兩個月的檔。"""
    from core.oi_study import TX_HEADER, _iso, _num, _rows
    d0 = date.fromisoformat(prev_day)
    months = sorted({(d0.year, d0.month), ((d0 + timedelta(days=20)).year, (d0 + timedelta(days=20)).month)})
    best: dict[str, tuple[float, float, float]] = {}
    for y, m in months:
        f = tx_dir / f"tx_{y}_{m:02d}.csv"
        if not f.exists():
            continue
        try:
            rows = _rows(f, TX_HEADER)
        except (OSError, ValueError):
            continue
        for r in rows:
            if (r.get("交易時段") or "").strip() != "盤後":
                continue
            code = (r.get("到期月份(週別)") or "").strip()
            if not code.isdigit() or len(code) != 6:
                continue
            d = _iso(r["交易日期"])
            if d <= prev_day:
                continue
            vol, h, l = _num(r.get("成交量", "")), _num(r.get("最高價", "")), _num(r.get("最低價", ""))
            if not vol or vol <= 0 or h is None or l is None or h < l:
                continue
            cur = best.get(d)
            if cur is None or vol > cur[2]:
                best[d] = (h, l, vol)
    if not best:
        return None
    h, l, _ = best[min(best)]
    return float(h - l)


# ── 命中率（純函式＋讀資料庫）──────────────────────────────────────

def hit_rate(rows: list[tuple[float, float, float, float]], n: int = HIT_N) -> dict[str, Any]:
    """rows＝[(lo, mid, hi, 實際)]（舊 → 新）。取最近 n 筆：落在 [lo, hi] 的比例、實際 ÷ 中位數的中位數。"""
    last = rows[-n:]
    if not last:
        return {"n": 0, "inside": 0, "rate": None, "ratio_median": None}
    inside = sum(1 for lo, _, hi, r in last if lo <= r <= hi)
    ratios = sorted(r / mid for _, mid, _, r in last if mid > 0)
    med = ratios[len(ratios) // 2] if len(ratios) % 2 else (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2
    return {"n": len(last), "inside": inside, "rate": round(inside / len(last), 3), "ratio_median": round(med, 3)}


def summary(path: Path) -> dict[str, Any]:
    """{"day": hit_rate, "night": hit_rate}；永遠不丟例外（讀不到回空的）。"""
    out: dict[str, Any] = {}
    try:
        with closing(_conn(path)) as c:
            for kind in ("day", "night"):
                rows = [(lo, mid, hi, r) for lo, mid, hi, r in c.execute(
                    "SELECT f.lo, f.mid, f.hi, o.realized FROM forecast_log f JOIN forecast_outcome o ON o.kind=f.kind AND o.target=f.target "
                    "WHERE f.kind=? ORDER BY f.target", (kind,))]
                out[kind] = hit_rate(rows)
    except Exception:  # noqa: BLE001
        logger.debug("預測命中率讀不到", exc_info=True)
        out = {k: {"n": 0, "inside": 0, "rate": None, "ratio_median": None} for k in ("day", "night")}
    out["min_show"] = MIN_SHOW
    return out


# ── 背景迴圈 ────────────────────────────────────────────────────

def fill_outcomes(path: Path, bars_fn: Callable[[str], list[dict[str, Any]]], now: datetime, tx_dir: Path = TX_DIR) -> int:
    """（同步）把收盤了的預測補上實際值；回傳補了幾筆。"""
    n = 0
    today = now.astimezone(TAIPEI).date().isoformat()
    m = now.hour * 60 + now.minute
    for kind, target in pending(path):
        if kind == "day":
            if target > today or (target == today and m < 14 * 60):
                continue
            r = realized_day(bars_fn(target), target)
            src = "bars_1m"
        else:
            if target >= today:                                                         # 夜盤隔天 15:10 後期交所資料才有
                continue
            r = realized_night(tx_dir, target)
            src = "taifex_tx"
        if r is not None and r > 0:
            save_outcome(path, kind, target, r, src)
            n += 1
    return n


async def forecast_loop(path: Path, get_day: Callable[[str], Awaitable[Optional[dict[str, Any]]]], get_evening: Callable[[float], Optional[dict[str, Any]]],
                        bars_fn: Callable[[str], list[dict[str, Any]]]) -> None:
    """每 POLL_SEC 秒：平日 08:00～13:45 記今天的日盤預測；16:00～05:00 記今晚的夜盤預測；補上收盤了的實際值。"""
    if not enabled():
        logger.info("預測紀錄：%s", "模擬盤不記（由正式盤負責）" if os.getenv("SIMULATION", "false").lower() == "true" else "已關閉（FORECAST_LOG=false）")
        return
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await tick(path, datetime.now(TAIPEI), get_day, get_evening, bars_fn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("預測紀錄迴圈例外（已忽略）")


async def tick(path: Path, now: datetime, get_day: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
               get_evening: Callable[[float], Optional[dict[str, Any]]], bars_fn: Callable[[str], list[dict[str, Any]]]) -> dict[str, Any]:
    now = now.astimezone(TAIPEI)
    m, today = now.hour * 60 + now.minute, now.date().isoformat()
    done: dict[str, Any] = {}
    if now.weekday() < 5 and 8 * 60 <= m < 13 * 60 + 45:
        d = await get_day(today)
        if d and d.get("expect"):
            done["day"] = await asyncio.to_thread(log_forecast, path, "day", today, d["expect"],
                                                  {k: d.get(k) for k in ("r20", "vix", "vix_date", "atm_equiv", "n")})
    if m >= 16 * 60 or m < 5 * 60:
        ev = await asyncio.to_thread(get_evening, now.timestamp())
        nt = (ev or {}).get("night") or {}
        if nt.get("expect") and ev.get("prev_day"):
            done["night"] = await asyncio.to_thread(log_forecast, path, "night", ev["prev_day"], nt["expect"],
                                                    {k: nt.get(k) for k in ("r20n", "vix", "vix_date", "day_range", "n")})
    done["filled"] = await asyncio.to_thread(fill_outcomes, path, bars_fn, now)
    return done
