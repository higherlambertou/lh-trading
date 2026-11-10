"""三組便宜的日資料檢定（update.md 發現 82）：賣權買權比（PCR）、結算日效應、期現基差有沒有統計上的根據。

判定標準在跑之前寫在 update.md 發現 82；只跑一次（結果存 data/daily_indicator_result.json；--rerun 才重跑）。
純離線的**描述性歷史研究**：PCR 與加權指數的歷史是 lh-intel 的回補列（as_of 規則在決策讀取看不到），所以這裡直接讀 intel.db 的最新版數字，
假設它們是官方在 event_date 收盤後公布、事後不修正；策略／風控／下單模組不得引用（tests/test_daily_indicator_study.py 靜態檢查），
也不能因為這個研究就讓它們去讀這些序列。不使用 core/intel_reader（那是給〈資訊〉面板顯示用的）。
資料：期交所 TX 日行情（data/taifex/tx/）、lh-intel 的 intel.db（唯讀）、core/night_history.py 抓的夜盤 1 分 K。
用法：python -m core.daily_indicator_study run [--rerun]"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from core.drift_timing_study import Day, load_tx
from core.weekly_straddle_study import slope_test

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "daily_indicator_result.json"
PTS = 47000.0                                          # 換算成現在的點數（同發現 72）
WARM = 250                                             # z 分數：前 250 個有值的交易日
START = "2006-01-02"                                   # 樣本從這天起（需要 250 天暖機；PCR 2004 起）
NW_LAG = 10
N_TESTS = 8
Z = NormalDist().inv_cdf(1 - 0.05 / (2 * N_TESTS))      # ≈ 2.73：雙尾、Bonferroni
MIN_N = 200                                            # 斜率類檢定的最少天數
MIN_EVENTS = 100                                       # 結算日類的最少事件數
NIGHT17_FIRST, NIGHT17_LAST = 121, 840                 # 夜盤 17:01～05:00（第 121～840 根）
TESTS: tuple[tuple[str, str, str], ...] = (
    ("T1", "slope", "PCR 成交量比 z → 當晚 17:01～05:00 報酬"),
    ("T2", "slope", "PCR 成交量比 z → 下一個日盤報酬"),
    ("T3", "slope", "PCR 未平倉比 z → 當晚 17:01～05:00 報酬"),
    ("T4", "slope", "PCR 未平倉比 z → 下一個日盤報酬"),
    ("T5", "dummy", "月結算日的日盤報酬"),
    ("T6", "dummy", "結算日前一個交易日的日盤報酬"),
    ("T7", "dummy", "結算日後一個交易日的日盤報酬"),
    ("T8", "slope", "期現基差 z → 下一個日盤報酬"),
)


def day_returns(days: Mapping[str, Day]) -> dict[str, float]:
    """日期 → 日盤開盤到收盤的報酬（點，比例 × 47,000；用當天日盤成交量最大的月合約）。"""
    out = {}
    for d, v in days.items():
        o, c = v.open.get(v.front), v.close.get(v.front)
        if o and c and o > 0 and c > 0:
            out[d] = (c / o - 1.0) * PTS
    return out


def settlement_dates(days: Mapping[str, Day]) -> list[str]:
    """月結算日＝TX 月合約在資料裡最後出現的那一天。最新那個合約還沒到期（資料最後一天它還在）所以不算。"""
    last: dict[str, str] = {}
    for d in sorted(days):
        for code in days[d].open:
            last[code] = d
    end = max(days) if days else ""
    return sorted({d for d in last.values() if d < end})


def zscores(series: Mapping[str, float], dates: Sequence[str], warm: int = WARM) -> dict[str, float]:
    """依日期順序：今天的值對「前 warm 個有值的日子」（**不含今天**）的 z 分數；前面不夠 warm 個或沒有變異就沒有。"""
    out: dict[str, float] = {}
    hist: list[float] = []
    for d in dates:
        v = series.get(d)
        if v is None or not np.isfinite(v):
            continue
        if len(hist) >= warm:
            w = np.array(hist[-warm:], dtype=float)
            sd = float(w.std(ddof=1))
            if sd > 0:
                out[d] = (v - float(w.mean())) / sd
        hist.append(float(v))
    return out


def night17_returns(nights: Mapping[str, Sequence[tuple]]) -> dict[str, float]:
    """夜盤日期 → 17:01～隔天 05:00 的報酬（點）；通過發現 81 的品質篩選、根數 ≥ 80% 才算。"""
    from core.night_window_study import clean_night, window_return
    out = {}
    for d, bars in nights.items():
        if clean_night(bars):
            r = window_return(bars, NIGHT17_FIRST, NIGHT17_LAST)
            if r is not None:
                out[d] = r
    return out


def build(days: Mapping[str, Day], pcr_vol: Mapping[str, float], pcr_oi: Mapping[str, float], taiex: Mapping[str, float],
          night17: Mapping[str, float], start: str = START, warm: int = WARM) -> dict[str, dict[str, Any]]:
    """每個檢定的（日期, x, y）。x＝z 分數或結算日虛擬變數。T 的資訊都是 T 日盤收盤後才有，目標是 T 之後的時段。"""
    dates = sorted(days)
    nxt = {a: b for a, b in zip(dates, dates[1:])}
    prv = {b: a for a, b in zip(dates, dates[1:])}
    D = day_returns(days)
    basis = {}
    for d in dates:
        v, t = days[d], taiex.get(d)
        c = v.close.get(v.front)
        if c and t and t > 0:
            basis[d] = (c - t) / t
    z = {"vol": zscores(pcr_vol, dates, warm), "oi": zscores(pcr_oi, dates, warm), "basis": zscores(basis, dates, warm)}

    def slope_xy(zs: Mapping[str, float], target: Mapping[str, float], shift: bool) -> dict[str, Any]:
        ds, xs, ys = [], [], []
        for d in dates:
            if d < start or d not in zs:
                continue
            key = nxt.get(d) if shift else d
            if key is not None and key in target:
                ds.append(d)
                xs.append(zs[d])
                ys.append(target[key])
        return {"dates": ds, "x": np.array(xs, dtype=float), "y": np.array(ys, dtype=float)}

    out = {"T1": slope_xy(z["vol"], night17, False), "T2": slope_xy(z["vol"], D, True),
           "T3": slope_xy(z["oi"], night17, False), "T4": slope_xy(z["oi"], D, True), "T8": slope_xy(z["basis"], D, True)}
    sett = settlement_dates(days)
    flags = {"T5": set(sett), "T6": {p for s in sett if (p := prv.get(s))}, "T7": {n for s in sett if (n := nxt.get(s))}}
    ds = [d for d in dates if d >= start and d in D]
    for k, fl in flags.items():
        out[k] = {"dates": ds, "x": np.array([1.0 if d in fl else 0.0 for d in ds]), "y": np.array([D[d] for d in ds], dtype=float)}
    return out


def _ols_slope(y: np.ndarray, x: np.ndarray) -> Optional[float]:
    if len(y) < 4:
        return None
    xc = x - x.mean()
    sxx = float(xc @ xc)
    return float(xc @ (y - y.mean())) / sxx if sxx > 0 else None


def check(t: Mapping[str, Any], kind: str) -> dict[str, Any]:
    ds, x, y = t["dates"], t["x"], t["y"]
    n = len(y)
    res = slope_test(y, x, NW_LAG) if n >= 4 else {"n": n, "b": None, "t": None}
    h = n // 2
    first, second = (_ols_slope(y[:h], x[:h]), _ols_slope(y[h:], x[h:])) if n >= 8 else (None, None)
    events = int((x > 0).sum()) if kind == "dummy" else None
    size_ok = (events >= MIN_EVENTS) if kind == "dummy" else n >= MIN_N
    b = res["b"]
    same = bool(b is not None and first is not None and second is not None and np.sign(first) == np.sign(second) == np.sign(b))
    out: dict[str, Any] = {"n": n, "events": events, "b": b, "t": res["t"], "first": first, "second": second, "same_sign": same,
                           "first_date": ds[0] if len(ds) else None, "last_date": ds[-1] if len(ds) else None}
    out["pass"] = bool(res["t"] is not None and abs(res["t"]) >= Z and size_ok and same)
    if kind == "slope" and n >= 25:
        lo, hi = np.percentile(x, 20), np.percentile(x, 80)
        out["quintile_spread"] = float(y[x >= hi].mean() - y[x <= lo].mean())
    return out


def run(days: Mapping[str, Day], pcr_vol: Mapping[str, float], pcr_oi: Mapping[str, float], taiex: Mapping[str, float],
        night17: Mapping[str, float]) -> dict[str, Any]:
    built = build(days, pcr_vol, pcr_oi, taiex, night17)
    kinds = {k: kd for k, kd, _ in TESTS}
    res = {k: check(built[k], kinds[k]) for k, _, _ in TESTS}
    return {"z": Z, "tests": res, "labels": {k: lab for k, _, lab in TESTS},
            "verdict": {k: ("過關" if r["pass"] else "沒過") for k, r in res.items()},
            "passed": [k for k, r in res.items() if r["pass"]]}


# ── 報告 ──────────────────────────────────────────────────────────

def _f(x: Optional[float], spec: str = "+.2f") -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    L = [f"日資料小檢定（8 個、雙尾、Bonferroni → |t| ≥ {r['z']:.2f}；點數＝比例 × {PTS:,.0f}）"]
    for k, t in r["tests"].items():
        eff = f"每 1 個標準差 {_f(t['b'], '+.1f')} 點" if t["events"] is None else f"事件 {t['events']} 個、比其他日子 {_f(t['b'], '+.1f')} 點"
        spread = f"、最高 20% − 最低 20% {_f(t.get('quintile_spread'), '+.1f')} 點" if "quintile_spread" in t else ""
        L.append(f"{k} {r['labels'][k]}：n {t['n']}（{t['first_date']}～{t['last_date']}）{eff}{spread}、t {_f(t['t'], '+.2f')}、前半 {_f(t['first'], '+.1f')}／後半 {_f(t['second'], '+.1f')} → {r['verdict'][k]}")
    L.append("結論：" + ("過關：" + "、".join(r["passed"]) if r["passed"] else "8 個檢定沒有一個過關"))
    return "\n".join(L)


# ── 讀資料 ────────────────────────────────────────────────────────

def intel_db_path() -> Path:
    env = os.getenv("INTEL_DB")
    return Path(env).expanduser() if env else ROOT.parent / "lh-intel" / "data" / "intel.db"


def read_series(path: Path, source: str, series: str) -> dict[str, float]:
    """intel.db 的一條日序列（event_date → 最新版的值；缺值的略過）。唯讀。"""
    out: dict[str, float] = {}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as c:
        for d, v in c.execute("SELECT event_date, value FROM observations WHERE source=? AND series=? AND value IS NOT NULL AND event_date IS NOT NULL ORDER BY event_date, revision", (source, series)):
            out[d] = float(v)                                      # 同一天多版：後面的 revision 蓋過前面的
    return out


def load_inputs() -> tuple[dict[str, Day], dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:  # pragma: no cover
    from core.night_history import NightStore
    path = intel_db_path()
    return (load_tx(), read_series(path, "taifex", "pcr.vol_ratio_pct"), read_series(path, "taifex", "pcr.oi_ratio_pct"),
            read_series(path, "twse", "taiex.close"), night17_returns(NightStore().nights()))


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "run":
        print(__doc__)
        return 2
    if RESULT_PATH.exists() and "--rerun" not in a:
        print("已經跑過一次（判定標準只跑一次）；結果：\n" + format_report(json.loads(RESULT_PATH.read_text(encoding="utf-8"))))
        return 0
    r = run(*load_inputs())
    RESULT_PATH.write_text(json.dumps(r, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(format_report(r))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
