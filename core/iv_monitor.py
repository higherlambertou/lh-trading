"""IV 監控（市場狀態第二層）：ATM 隱含波動率 → 歷史百分位 → LOW / NORMAL / HIGH。

資料來源（優先序）：
  1. 手動輸入（POST /api/market/iv，單位 %），永遠優先於自動抓取
  2. Shioaji 自動抓：取最近且距到期 >= IV_MIN_DAYS 天的 TXO 月選擇權，ATM 履約價的 Call/Put，
     用賣權買權平價推遠期價 F = K + (C-P)/df，再以 Black-76 反推 IV（Call/Put 取平均）
  3. CSV 回填歷史（python -m core.iv_monitor import file.csv）：百分位需要歷史，
     不回填就要等每天累積到 IV_MIN_HISTORY 天

純函式為主，方便單元測試；只有 fetch_atm_iv 會呼叫 broker（走 broker.* 的 async 介面，
不直接碰 shioaji，符合 event loop 鐵則）。
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
from datetime import date, datetime, timedelta
from math import erf, exp, log, sqrt
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger(__name__)

LOW_PCT = 20.0             # 文件：歷史 20% 分位以下 → 市場過於平靜
HIGH_PCT = 80.0            # 文件：歷史 80% 分位以上 → 大波動已發生
LOOKBACK = 252             # 回溯天數（一年）

IV_LABEL = {"LOW": "偏低", "HIGH": "偏高", "NORMAL": "正常", "UNKNOWN": "未知"}


# ── 選擇權定價 / 反推 ─────────────────────────────────────────────

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def black76(F: float, K: float, T: float, sigma: float, r: float, is_call: bool) -> float:
    df = exp(-r * T)
    if T <= 0 or sigma <= 0:
        return df * max(F - K, 0.0) if is_call else df * max(K - F, 0.0)
    v = sigma * sqrt(T)
    d1 = (log(F / K) + 0.5 * v * v) / v
    d2 = d1 - v
    if is_call:
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def implied_vol(price: float, F: float, K: float, T: float, r: float, is_call: bool) -> float | None:
    """二分法反推 Black-76 隱含波動率（小數，0.2 = 20%）。無解（價格低於內含值/高於上限）回傳 None。"""
    if price <= 0 or T <= 0 or F <= 0 or K <= 0:
        return None
    lo, hi = 1e-4, 5.0
    f_lo = black76(F, K, T, lo, r, is_call) - price
    f_hi = black76(F, K, T, hi, r, is_call) - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if black76(F, K, T, mid, r, is_call) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-8:
            break
    return 0.5 * (lo + hi)


def atm_iv_from_quotes(call: float, put: float, K: float, T: float, r: float
                       ) -> tuple[float, float] | None:
    """ATM Call/Put 價格 → (IV %, 遠期價 F)。無解回傳 None。"""
    if call <= 0 or put <= 0 or T <= 0:
        return None
    df = exp(-r * T)
    F = K + (call - put) / df                 # 平價：C - P = df * (F - K)
    if F <= 0:
        return None
    ivs = [v for v in (implied_vol(call, F, K, T, r, True),
                       implied_vol(put, F, K, T, r, False)) if v]
    if not ivs:
        return None
    return round(100.0 * sum(ivs) / len(ivs), 2), round(F, 1)


def expiry_of(delivery_month: str) -> datetime:
    """TXO 月選擇權到期日：該月第三個週三 13:30（遇假日順延的情形忽略）。"""
    y, m = int(delivery_month[:4]), int(delivery_month[4:6])
    d = date(y, m, 1)
    d += timedelta(days=(2 - d.weekday()) % 7)        # 第一個週三
    d += timedelta(days=14)
    return datetime(d.year, d.month, d.day, 13, 30)


def pick_expiry(months: Sequence[str], now: datetime, min_days: float = 7.0) -> str | None:
    """最近且距到期 >= min_days 天的月份（避開到期週的 gamma 扭曲）。"""
    for m in sorted(set(months)):
        if (expiry_of(m) - now).total_seconds() >= min_days * 86400:
            return m
    return None


# ── 百分位 / 訊號（文件的定義）────────────────────────────────────

def iv_percentile(current: float, history: Sequence[float], lookback: int = LOOKBACK) -> float | None:
    recent = np.asarray(list(history)[:lookback], dtype=float)     # history 為新→舊
    if recent.size == 0:
        return None
    return float(np.sum(recent < current) / recent.size * 100.0)


def iv_signal(percentile: float, low: float = LOW_PCT, high: float = HIGH_PCT) -> str:
    if percentile < low:
        return "LOW"
    if percentile > high:
        return "HIGH"
    return "NORMAL"


def evaluate_iv(current: float | None, history: Sequence[float], *,
                min_history: int = 60, low: float = LOW_PCT, high: float = HIGH_PCT,
                lookback: int = LOOKBACK) -> dict[str, Any]:
    """current + 歷史（新→舊）→ {state, label, percentile, history_n, note}。"""
    n = len(history)
    base = {"history_n": n, "min_history": min_history, "percentile": None}
    if current is None:
        return {**base, "state": "UNKNOWN", "label": IV_LABEL["UNKNOWN"],
                "note": "尚無 IV：POST /api/market/iv 手動輸入，或等 Shioaji 自動抓取"}
    if n < min_history:
        return {**base, "state": "UNKNOWN", "label": f"累積中 {n}/{min_history}",
                "note": f"IV 歷史僅 {n} 天（需 {min_history} 天才算百分位）；可用 CSV 回填"}
    pct = iv_percentile(current, history, lookback)
    state = iv_signal(pct, low, high)
    return {**base, "state": state, "label": IV_LABEL[state], "percentile": round(pct, 1), "note": ""}


# ── Shioaji 自動抓 ATM IV ─────────────────────────────────────────

def _option_price(q: dict[str, Any]) -> float:
    """最近成交價優先；沒有成交才用買賣中價。"""
    close = float(q.get("close") or 0)
    if close > 0:
        return close
    bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
    return (bid + ask) / 2 if bid > 0 and ask >= bid else 0.0


async def fetch_atm_iv(broker: Any, now: datetime | None = None, *,
                       rate: float | None = None, min_days: float | None = None) -> dict[str, Any]:
    """向券商取 ATM 選擇權報價並計算 IV。失敗丟例外（呼叫端決定要不要退回手動）。"""
    now = now or datetime.now()
    rate = float(os.getenv("IV_RATE", "0.015")) if rate is None else rate
    min_days = float(os.getenv("IV_MIN_DAYS", "7")) if min_days is None else min_days

    snaps = await asyncio.wait_for(broker.snapshots(["TXF"]), timeout=10)
    under = float(snaps[0]["close"]) if snaps else 0.0
    if under <= 0:
        raise RuntimeError("取不到 TXF 現價")
    months = await asyncio.wait_for(broker.option_expiries("TXO"), timeout=15)
    month = pick_expiry(months, now, min_days)
    if not month:
        raise RuntimeError(f"找不到距到期 >= {min_days:g} 天的 TXO 月份（{months}）")
    strikes = await asyncio.wait_for(broker.option_strikes(month, "C", "TXO"), timeout=15)
    if not strikes:
        raise RuntimeError(f"{month} 無履約價")
    k = min(strikes, key=lambda s: abs(s - under))
    call = await asyncio.wait_for(broker.option_snapshot(month, k, "C", "TXO"), timeout=10)
    put = await asyncio.wait_for(broker.option_snapshot(month, k, "P", "TXO"), timeout=10)
    pc, pp = _option_price(call), _option_price(put)
    T = (expiry_of(month) - now).total_seconds() / (365 * 86400)
    res = atm_iv_from_quotes(pc, pp, float(k), T, rate)
    if res is None:
        raise RuntimeError(f"IV 反推失敗（{month} {k} call={pc} put={pp}）")
    iv, F = res
    return {"iv": iv, "strike": int(k), "month": month, "call": pc, "put": pp,
            "forward": F, "days": round(T * 365, 1), "underlying": under}


# ── CSV 回填：python -m core.iv_monitor import file.csv ───────────

def _parse_date(s: str) -> str | None:
    s = s.strip().replace("/", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def read_iv_csv(path: str) -> list[tuple[str, float]]:
    """讀 (日期, IV%) 清單。第一欄當日期；IV 欄優先找標頭含 iv/vix/波動 的，否則取第二欄。
    也接受無標頭（date,iv）。無法解析的列略過。"""
    out: list[tuple[str, float]] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return out
    col = 1
    for i, h in enumerate(rows[0]):
        if any(t in h.lower() for t in ("iv", "vix", "波動")):
            col = i
            break
    for row in rows:
        if len(row) <= col:
            continue
        d = _parse_date(row[0])
        try:
            v = float(row[col])
        except ValueError:
            continue
        if d and 0 < v < 500:
            out.append((d, v))
    return out


def import_iv_csv(path: str, store: Any | None = None) -> int:
    from core.market_store import MarketStore
    store = store or MarketStore()
    n = 0
    for d, v in read_iv_csv(path):
        if store.upsert_iv(d, v, "csv", "import"):
            n += 1
    return n


if __name__ == "__main__":  # pragma: no cover
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    if len(sys.argv) == 3 and sys.argv[1] == "import":
        print(f"已匯入 {import_iv_csv(sys.argv[2])} 筆 IV 歷史")
    else:
        print("用法：python -m core.iv_monitor import <csv>   （欄位：日期, IV%）")
