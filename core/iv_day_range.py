"""開盤前預期日盤振幅（update.md 發現 55，依發現 54）：前一天選擇權隱含波動（IV）加上近 20 日平均振幅，預測今天日盤的高低差。**只顯示**，不影響範圍帶、建議停損停利、策略與風控。

模型（與發現 54 相同）：ln(日盤高低差) ＝ a ＋ b_ref·ln R20 ＋ b_iv·ln(IV÷100 × F)；歷史來自 data/taifex/vrp 的每日 ATM IV（發現 27）與 TXF 日盤。區間用殘差的 16／84% 分位數（和〈時段脈絡〉既有的預期振幅同一做法）。
**即時的 IV 用 lh-intel 的官方臺指選擇權波動率指數 × 0.93**（前一個交易日的收盤，嚴格早於今天、≤ 4 個日曆天）；0.93＝官方指數與 ATM IV 在 70 個重疊日的水準比中位數（發現 53、54）；F＝前一個日盤收盤。
讀不到官方指數、太舊、歷史不夠擬合 → 不顯示（回 {"note": 原因}），不用別的數字湊。
誠實的限制（畫面也寫）：發現 54 的檢驗是「比只看 20 日均預測誤差降 15～18%」，但各年很不均（2022～2023 幾乎沒有）；只對整天的高低差有用，對收開差沒有；是範圍，不是目標價。
CLI：python -m core.iv_day_range check（描述性檢查：樣本外區間涵蓋率、官方指數替身與真 ATM IV 的差距；不是假設檢定）。"""
from __future__ import annotations

import csv
import logging
import math
import sys
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from core import intel_reader
from core.intraday_context import N_REF, SESSION_MIN, WARM, Session, ref_ranges, window_range

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
IV_DIR = ROOT / "data" / "taifex" / "vrp"
RATIO = 0.93                     # 官方臺指選擇權波動率指數 → ATM IV（發現 53、54：70 個重疊日的水準比中位數）
MAX_STALE_DAYS = 4
MIN_FIT_DAYS = 250
SPLIT = "2025-01-01"           # 只用在描述性檢查：區間涵蓋率分探索期與保留期看
VIX_SERIES = ("taifex", "vixtw.close")


@dataclass(frozen=True)
class DayModel:
    """ln(日盤高低差) ＝ a ＋ b_ref·ln R20 ＋ b_iv·x_iv，加上殘差分位數。"""
    a: float
    b_ref: float
    b_iv: float
    q16: float
    q50: float
    q84: float
    n: int


def x_of(iv_pct: Any, f: Any) -> Optional[float]:
    """ln(IV÷100 × F)（把 IV 換成以點數計的年化波動，單位與 R20 一致）；IV 或 F 不是正的有限數就 None。"""
    try:
        iv, f = float(iv_pct), float(f)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(iv) and math.isfinite(f)) or iv <= 0 or f <= 0:
        return None
    return math.log(iv / 100.0 * f)


def load_iv_history(directory: Path = IV_DIR) -> dict[str, dict[str, float]]:
    """data/taifex/vrp/iv_*.csv → {日期: {"iv", "F"}}（和 core/vrp_study.load_iv 同一份資料，只留這裡要的欄位）。"""
    out: dict[str, dict[str, float]] = {}
    for f in sorted(Path(directory).glob("iv_*.csv")):
        with f.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                out[r["date"]] = {"iv": float(r["iv"]), "F": float(r["F"])}
    return dict(sorted(out.items()))


def prev_x(dates: Sequence[str], iv: dict[str, dict[str, float]]) -> np.ndarray:
    """每個日期：嚴格早於當天的最近一個 IV 日（≤ MAX_STALE_DAYS 個日曆天）的 x_iv；沒有或太舊是 NaN。"""
    iv_dates = sorted(iv)
    out = np.full(len(dates), np.nan)
    for i, d in enumerate(dates):
        j = bisect_left(iv_dates, d)
        if j > 0 and (date.fromisoformat(d) - date.fromisoformat(iv_dates[j - 1])).days <= MAX_STALE_DAYS:
            v = x_of(iv[iv_dates[j - 1]]["iv"], iv[iv_dates[j - 1]]["F"])
            out[i] = np.nan if v is None else v
    return out


def design(sessions: Sequence[Session], iv: dict[str, dict[str, float]]) -> dict[str, np.ndarray]:
    """可用的日子：日期、ln 日盤高低差（目標）、ln R20、x_iv。"""
    dates = [s.date for s in sessions]
    rng = np.array([window_range(s, 0, SESSION_MIN) for s in sessions])
    R = ref_ranges(sessions)
    x = prev_x(dates, iv)
    with np.errstate(all="ignore"):
        y, lnR = np.log(rng), np.log(R)
    ok = np.isfinite(y) & np.isfinite(lnR) & np.isfinite(x) & (rng > 0)
    return {"dates": np.array(dates)[ok], "y": y[ok], "lnR": lnR[ok], "x": x[ok]}


def fit(sessions: Sequence[Session], iv: dict[str, dict[str, float]]) -> Optional[DayModel]:
    d = design(sessions, iv)
    if len(d["y"]) < MIN_FIT_DAYS:
        return None
    X = np.column_stack([np.ones(len(d["y"])), d["lnR"], d["x"]])
    beta = np.linalg.lstsq(X, d["y"], rcond=None)[0]
    q16, q50, q84 = np.quantile(d["y"] - X @ beta, [0.16, 0.5, 0.84])
    return DayModel(float(beta[0]), float(beta[1]), float(beta[2]), float(q16), float(q50), float(q84), int(len(d["y"])))


def project(m: DayModel, r20: float, x: float) -> dict[str, float]:
    """今天日盤高低差的中位數與 68% 區間（點）。"""
    mu = m.a + m.b_ref * math.log(r20) + m.b_iv * x
    return {"lo": round(math.exp(mu + m.q16), 1), "mid": round(math.exp(mu + m.q50), 1), "hi": round(math.exp(mu + m.q84), 1)}


# ── 即時的 IV：lh-intel 的官方臺指選擇權波動率指數（唯讀；只用前一個交易日）────────────────

def live_vix(today: str, now: Optional[float] = None, path: Optional[Path] = None) -> tuple[Optional[tuple[str, float]], Optional[str]]:
    """前一個交易日（嚴格早於 today、≤ MAX_STALE_DAYS 個日曆天）的官方指數收盤 → ((日期, 值), None)；讀不到 → (None, 原因)。永遠不丟例外。"""
    import sqlite3
    import time
    from contextlib import closing
    now = time.time() if now is None else now
    path = path or intel_reader.default_path()
    if not path.exists():
        return None, "找不到 lh-intel 資料庫，沒有前一天的臺指選擇權波動率指數"
    try:
        with closing(intel_reader.connect_ro(path)) as conn:
            pts = [(r["event_date"], r["value"]) for r in intel_reader.latest_points(conn, VIX_SERIES[0], VIX_SERIES[1], now, 8) if r["value"] is not None and r["event_date"] < today]
    except sqlite3.Error as e:
        return None, f"讀不到臺指選擇權波動率指數（{type(e).__name__}）"
    except Exception as e:  # noqa: BLE001
        logger.debug("live_vix 失敗", exc_info=True)
        return None, f"讀不到臺指選擇權波動率指數（{type(e).__name__}）"
    if not pts:
        return None, "還沒有前一天的臺指選擇權波動率指數"
    d, v = max(pts)
    if (date.fromisoformat(today) - date.fromisoformat(d)).days > MAX_STALE_DAYS:
        return None, f"臺指選擇權波動率指數最新只到 {d}（太舊，不用）"
    return (d, float(v)), None


def for_today(sessions: Sequence[Session], today: str, iv: Optional[dict[str, dict[str, float]]] = None, vix: Optional[tuple[str, float]] = None,
              reason: Optional[str] = None) -> dict[str, Any]:
    """畫面用的 dict：{"expect": {lo, mid, hi}, "r20", "vix", "vix_date", "atm_equiv", "n"}，或 {"note": 原因}。sessions 是今天以前的日盤。
    iv／vix 不給就讀真的（歷史 IV 檔、lh-intel）；給了就用給的（測試與離線檢查用）。"""
    if not sessions:
        return {"note": "沒有歷史日盤"}
    hist = load_iv_history() if iv is None else iv
    model = fit(sessions, hist)
    if model is None:
        return {"note": f"歷史不夠擬合（需要 {MIN_FIT_DAYS} 個有選擇權隱含波動的日盤）"}
    if vix is None and reason is None:
        vix, reason = live_vix(today)
    if vix is None:
        return {"note": reason or "沒有前一天的臺指選擇權波動率指數"}
    rng = [window_range(s, 0, SESSION_MIN) for s in sessions]
    if len(rng) < N_REF or not all(math.isfinite(v) and v > 0 for v in rng[-N_REF:]):
        return {"note": "近 20 個日盤資料不完整，算不出參考振幅"}
    r20 = float(np.mean(rng[-N_REF:]))
    last_close = next((float(s.cl[np.flatnonzero(np.isfinite(s.cl))[-1]]) for s in reversed(sessions) if np.isfinite(s.cl).any()), None)
    x = x_of(vix[1] * RATIO, last_close)
    if x is None:
        return {"note": "算不出隱含波動（缺前一個日盤收盤）"}
    return {"expect": project(model, r20, x), "r20": round(r20, 1), "vix": round(vix[1], 2), "vix_date": vix[0], "atm_equiv": round(vix[1] * RATIO, 2), "n": model.n}


# ── 描述性檢查（不是假設檢定）──────────────────────────────────────

def coverage(sessions: Sequence[Session], iv: dict[str, dict[str, float]]) -> Optional[dict[str, Any]]:
    """擴張視窗（前 WARM 個樣本暖機）樣本外：68% 區間實際涵蓋多少、中位數預測的平均絕對對數誤差、區間寬度（對數）；IV 模型 vs 只用 R20。"""
    d = design(sessions, iv)
    n = len(d["y"])
    if n < WARM + 60:
        return None
    one = np.ones(n)
    out: dict[str, Any] = {"n": n, "n_oos": n - WARM}
    ys = d["dates"][WARM:]
    holdout = np.array([x >= SPLIT for x in ys])
    for name, X in (("iv", np.column_stack([one, d["lnR"], d["x"]])), ("r20_only", np.column_stack([one, d["lnR"]]))):
        hit, mae, width = [], [], []
        for i in range(WARM, n):
            b = np.linalg.lstsq(X[:i], d["y"][:i], rcond=None)[0]
            q16, q50, q84 = np.quantile(d["y"][:i] - X[:i] @ b, [0.16, 0.5, 0.84])
            mu = float(X[i] @ b)
            hit.append(mu + q16 <= d["y"][i] <= mu + q84)
            mae.append(abs(d["y"][i] - (mu + q50)))
            width.append(q84 - q16)
        hit_a = np.array(hit)
        out[name] = {"coverage": float(hit_a.mean()), "coverage_explore": float(hit_a[~holdout].mean()), "coverage_holdout": float(hit_a[holdout].mean()) if holdout.any() else None,
                     "mae_log": float(np.mean(mae)), "width_log": float(np.mean(width))}
    return out


def substitute_gap(model: DayModel, sessions: Sequence[Session], iv: dict[str, dict[str, float]], vix: dict[str, float]) -> Optional[dict[str, Any]]:
    """官方指數 × RATIO 代替真的 ATM IV：在兩者都有的日子，預測日振幅中位數的差距（％）。以『該日之前』的資料算 R20，和即時用法一樣。"""
    dates = [s.date for s in sessions]
    R = ref_ranges(sessions)
    pos = {d: i for i, d in enumerate(dates)}
    diffs = []
    for d, v in vix.items():
        i = pos.get(d)
        if i is None or i + 1 >= len(dates) or d not in iv or not np.isfinite(R[i + 1]):
            continue
        f = float(sessions[i].cl[np.flatnonzero(np.isfinite(sessions[i].cl))[-1]])
        x_true, x_sub = x_of(iv[d]["iv"], iv[d]["F"]), x_of(v * RATIO, f)
        if x_true is None or x_sub is None:
            continue
        a = project(model, float(R[i + 1]), x_true)["mid"]
        b = project(model, float(R[i + 1]), x_sub)["mid"]
        diffs.append((b - a) / a * 100.0)
    if not diffs:
        return None
    arr = np.array(diffs)
    return {"n": len(arr), "mean_pct": float(arr.mean()), "median_abs_pct": float(np.median(np.abs(arr))), "p90_abs_pct": float(np.percentile(np.abs(arr), 90)), "max_abs_pct": float(np.abs(arr).max())}


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "check":
        print(__doc__)
        return 2
    import sqlite3
    from core.intraday_context import load_sessions
    from core.market_store import MarketStore
    sessions, iv = load_sessions(MarketStore(), "TXF"), load_iv_history()
    cov = coverage(sessions, iv)
    print("區間涵蓋率（擴張視窗樣本外）：", cov)
    m = fit(sessions, iv)
    print("全期模型：", m)
    c = sqlite3.connect(f"file:{intel_reader.default_path()}?mode=ro", uri=True)
    vix = {r[0]: r[1] for r in c.execute("SELECT event_date, value FROM observations WHERE source='taifex' AND series='vixtw.close' AND value IS NOT NULL")}
    print("替身差距：", substitute_gap(m, sessions, iv, vix) if m else None)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
