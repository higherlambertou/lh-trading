"""Hurst 指數（市場狀態第一層）。純 numpy，不碰 shioaji、不做 I/O。

為什麼重寫（原 Downloads/hurst_analyzer.py 無法直接搬進來）：
  1. shioaji 1.5.x 的 kbars() 沒有 timeframe 參數、也沒有 constant.Timeframe；
     ts 是奈秒（原腳本用 unit="s"）。→ 改抓 1 分 K 自行合成日 K（aggregate_daily）。
  2. 原估計器有系統性偏誤：R/S 直接套在「對數價格」上，純隨機漫步得 ~0.97；
     方差法用去均值 std，在 60 根窗口得 ~0.14。兩者偏誤相反，平均後才碰巧像 0.55，
     而 reliable（兩法差 < 0.08）在隨機漫步上通過率 0%。
  3. 現用 DFA-1（對報酬的累積離差）：實測 60 根窗口對 AR(1) 序列的分辨力是 R/S 的 2 倍以上。
     DFA 在短序列有 ~+0.1 的偏誤，所以對「同樣長度的 iid 常態序列」做蒙地卡羅校準，
     使隨機漫步 → H=0.5，並得到 null 標準差 → z 值（統計上離隨機漫步多遠）。

⚠️ 短窗口的雜訊很大：60 根日 K 的 H 標準差約 0.13，文件的 0.45/0.55 門檻落在雜訊之內
（純隨機漫步也有 ~70% 的窗口會被標成趨勢/均值回歸）。z 值就是用來看這件事；
想更保守可設 HURST_MIN_Z，想更準可加大 HURST_WINDOW（250 根時標準差約 0.06）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

import numpy as np

TREND_TH = 0.55            # H > 此值 → 趨勢
REVERT_TH = 0.45           # H < 此值 → 均值回歸
MIN_RETURNS = 32           # 至少要這麼多筆報酬（= 33 根日 K）才估計
NULL_SIMS = 2000           # 蒙地卡羅校準的模擬次數（標準誤 ≈ 0.003）

SESSION_START_MIN = 8 * 60 + 45     # 日盤 08:45
SESSION_END_MIN = 13 * 60 + 45      # 日盤 13:45

HURST_LABEL = {
    "TREND": "趨勢",
    "REVERT": "均值回歸",
    "RANDOM": "隨機漫步",
    "UNCERTAIN": "資料不足",
}


# ── 估計器 ────────────────────────────────────────────────────────

def hurst_dfa(returns: Sequence[float], min_scale: int = 4) -> float:
    """DFA-1 指數（未校準）。輸入為報酬序列；資料不足/退化回傳 nan。"""
    r = np.asarray(returns, dtype=float)
    n = r.size
    if n < MIN_RETURNS or not np.isfinite(r).all():
        return float("nan")
    y = np.cumsum(r - r.mean())
    scales = np.unique(np.geomspace(min_scale, n // 4, 12).astype(int))
    if scales.size < 5:
        return float("nan")
    xs, ys = [], []
    for s in scales:
        k = n // s
        seg = y[: k * s].reshape(k, s)
        tc = np.arange(s, dtype=float)
        tc -= tc.mean()
        slope = (seg * tc).sum(axis=1) / (tc * tc).sum()           # 每段的線性趨勢（封閉解）
        resid = seg - seg.mean(axis=1, keepdims=True) - np.outer(slope, tc)
        f2 = float(np.mean(resid * resid))
        if f2 <= 0:
            return float("nan")
        xs.append(math.log(s))
        ys.append(0.5 * math.log(f2))
    return float(np.polyfit(xs, ys, 1)[0])


_NULL_CACHE: dict[int, tuple[float, float]] = {}


def null_stats(n: int) -> tuple[float, float]:
    """n 筆報酬的 iid 常態序列，DFA 指數的 (平均, 標準差)。固定 seed：同一 n 結果固定。"""
    hit = _NULL_CACHE.get(n)
    if hit is not None:
        return hit
    rng = np.random.default_rng(20260101 + n)
    vals = np.array([hurst_dfa(rng.standard_normal(n)) for _ in range(NULL_SIMS)])
    vals = vals[np.isfinite(vals)]
    out = (float(vals.mean()), float(vals.std(ddof=1)))
    _NULL_CACHE[n] = out
    return out


# ── 分類 ──────────────────────────────────────────────────────────

def classify_hurst(
    h: float | None,
    z: float | None = None,
    trend_th: float = TREND_TH,
    revert_th: float = REVERT_TH,
    min_z: float = 0.0,
) -> str:
    """H → TREND / REVERT / RANDOM / UNCERTAIN。
    min_z > 0 時，|z| 不足視為統計上與隨機漫步無法區分 → RANDOM。"""
    if h is None or not math.isfinite(h):
        return "UNCERTAIN"
    if min_z > 0 and (z is None or abs(z) < min_z):
        return "RANDOM"
    if h > trend_th:
        return "TREND"
    if h < revert_th:
        return "REVERT"
    return "RANDOM"


@dataclass
class HurstResult:
    value: float | None        # 校準後 H（隨機漫步 = 0.5）；None = 資料不足
    raw: float | None          # 未校準的 DFA 指數
    z: float | None            # (raw - null 平均) / null 標準差
    se: float | None           # null 標準差（該窗口長度下，純隨機漫步的 H 雜訊）
    state: str
    label: str
    window: int                # 實際使用的 K 棒數
    last_bar: str              # 最後一根日 K 的日期
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze(
    closes: Sequence[float],
    dates: Sequence[str] | None = None,
    window: int = 60,
    trend_th: float = TREND_TH,
    revert_th: float = REVERT_TH,
    min_z: float = 0.0,
) -> HurstResult:
    """用最近 window 根收盤價計算 Hurst 並分類。"""
    c = np.asarray(list(closes)[-window:], dtype=float)
    last = (list(dates)[-1] if dates else "")
    if c.size < MIN_RETURNS + 1 or not np.isfinite(c).all() or (c <= 0).any():
        return HurstResult(None, None, None, None, "UNCERTAIN", HURST_LABEL["UNCERTAIN"],
                           int(c.size), last, f"資料不足：{c.size} 根日K，至少需要 {MIN_RETURNS + 1} 根")
    rets = np.diff(np.log(c))
    raw = hurst_dfa(rets)
    if not math.isfinite(raw):
        return HurstResult(None, None, None, None, "UNCERTAIN", HURST_LABEL["UNCERTAIN"],
                           int(c.size), last, "估計失敗（價格序列退化）")
    mean0, sd0 = null_stats(len(rets))
    value = float(np.clip(0.5 + raw - mean0, 0.0, 1.0))
    z = (raw - mean0) / sd0 if sd0 > 0 else None
    state = classify_hurst(value, z, trend_th, revert_th, min_z)
    return HurstResult(round(value, 4), round(raw, 4), None if z is None else round(z, 2),
                       round(sd0, 3), state, HURST_LABEL[state], int(c.size), last)


def trend_direction(closes: Sequence[float], ma: int = 20, band: float = 0.001) -> int:
    """日 K 收盤相對 ma 日均線：+1 偏多／-1 偏空／0 貼近均線（|偏離| < band）或資料不足。
    scalp 的 market_bias 用它當「方向」。"""
    c = list(closes)
    if len(c) < ma:
        return 0
    sma = sum(c[-ma:]) / ma
    if sma <= 0:
        return 0
    dev = c[-1] / sma - 1.0
    if abs(dev) < band:
        return 0
    return 1 if dev > 0 else -1


# ── 1 分 K → 日 K ─────────────────────────────────────────────────

def ts_to_local(ts: float) -> datetime:
    """shioaji 的 K 棒 ts 是「台灣當地時間當成 UTC 的 epoch」，單位通常是奈秒。
    依數量級容錯 ns/ms/s，回傳 naive 的台灣當地時間。"""
    t = float(ts)
    if t > 1e17:
        sec = t / 1e9
    elif t > 1e14:
        sec = t / 1e6
    elif t > 1e11:
        sec = t / 1e3
    else:
        sec = t
    return datetime(1970, 1, 1) + timedelta(seconds=sec)


def aggregate_daily(kbars: Mapping[str, Sequence[float]]) -> list[dict[str, Any]]:
    """1 分 K（ts/open/high/low/close/volume 的平行陣列）→ 日盤日 K（升冪）。
    只取 08:45~13:45 的 bar：夜盤 15:00~05:00 跨日、歸屬模糊，Hurst 先不納入。"""
    days: dict[str, dict[str, Any]] = {}
    rows = sorted(zip(kbars["ts"], kbars["open"], kbars["high"], kbars["low"],
                      kbars["close"], kbars["volume"]), key=lambda x: x[0])
    for ts, o, h, l, c, v in rows:
        dt = ts_to_local(ts)
        mins = dt.hour * 60 + dt.minute
        if not (SESSION_START_MIN <= mins <= SESSION_END_MIN):
            continue
        if not (o and h and l and c):
            continue
        key = dt.strftime("%Y-%m-%d")
        d = days.get(key)
        if d is None:
            days[key] = {"date": key, "open": float(o), "high": float(h), "low": float(l),
                         "close": float(c), "volume": int(v or 0), "nbars": 1}
        else:
            d["high"] = max(d["high"], float(h))
            d["low"] = min(d["low"], float(l))
            d["close"] = float(c)
            d["volume"] += int(v or 0)
            d["nbars"] += 1
    return [days[k] for k in sorted(days)]


# ── CLI：python -m core.hurst_analyzer ────────────────────────────

def _main() -> None:  # pragma: no cover
    import os
    from dotenv import load_dotenv
    from core.market_store import MarketStore

    load_dotenv()
    window = int(os.getenv("HURST_WINDOW", "60"))
    bars = MarketStore().bars("TXF", window)
    if not bars:
        print("尚無日 K 快取：先啟動服務（會自動向券商抓 TXF 日 K），或 POST /api/market/refresh")
        return
    res = analyze([b["close"] for b in bars], [b["date"] for b in bars], window)
    print(f"Hurst 指數：{res.value}（{res.label}）  z={res.z}  雜訊±{res.se}  "
          f"窗口 {res.window} 根，最後一根 {res.last_bar}")
    if res.note:
        print(res.note)


if __name__ == "__main__":  # pragma: no cover
    _main()
