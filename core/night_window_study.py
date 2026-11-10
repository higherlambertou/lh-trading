"""夜盤的漂移集中在哪個時段（update.md 發現 81）。

判定標準在跑之前寫在 update.md 發現 81；只跑一次（結果存 data/night_window_result.json；--rerun 才重跑）。
純離線研究：策略／風控／下單模組不得引用（tests/test_night_window_study.py 靜態檢查）。
資料：core/night_history.py 抓的 TXF 夜盤 1 分 K（data/night_bars.db）；對照用 bars_1m 的日盤 1 分 K。
用法：python -m core.night_window_study run [--rerun]"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional, Sequence

import numpy as np

from core.weekly_straddle_study import mean_test

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "night_window_result.json"
PTS = 47000.0                                          # 換算成現在的點數（同發現 72）
COST = 4.0                                             # 微台來回成本 4 點（同發現 43～46）
EXPLORE_END = "2024-12-31"
HOLDOUT_START = "2025-01-01"
T_CARRY = NormalDist().inv_cdf(0.95)                   # 第一關：單尾 5%（不校正，只是挑誰帶過去）
MIN_BARS = 600                                         # 一晚至少這麼多根（滿的是 840）
MAX_JUMP = 0.02                                        # 相鄰兩根收盤價差超過 2% ＝ 資料錯誤，整晚丟掉
MIN_COVER = 0.8                                        # 一個時段要有 ≥ 80% 的根
MIN_NIGHTS = 100                                       # 保留期夜數
# 5 個固定時段：(名稱, 第一根的 seq, 最後一根的 seq, 標籤)；seq＝夜盤第幾根（15:01 ＝ 1、00:00 ＝ 540、05:00 ＝ 840）
WINDOWS: tuple[tuple[str, int, int, str], ...] = (
    ("W1", 1, 120, "15:01～17:00"),
    ("W2", 121, 390, "17:01～21:30"),
    ("W3", 391, 540, "21:31～00:00"),
    ("W4", 541, 720, "00:01～03:00"),
    ("W5", 721, 840, "03:01～05:00"),
)
Bar = tuple  # (seq, open, high, low, close, volume)


def clean_night(bars: Sequence[Bar]) -> bool:
    """品質篩選：根數夠、沒有相鄰收盤價差超過 2%。"""
    if len(bars) < MIN_BARS:
        return False
    cl = [b[4] for b in bars]
    return all(abs(b / a - 1) <= MAX_JUMP for a, b in zip(cl, cl[1:]))


def window_return(bars: Sequence[Bar], first: int, last: int) -> Optional[float]:
    """做多持有整個時段的報酬（點，比例 × 47,000）：最後一根收盤 ÷ 第一根開盤 − 1；根數不到時段的 80% 回 None。"""
    inside = [b for b in bars if first <= b[0] <= last]
    if len(inside) < MIN_COVER * (last - first + 1):
        return None
    return (inside[-1][4] / inside[0][1] - 1.0) * PTS


def night_returns(nights: dict[str, Sequence[Bar]]) -> dict[str, dict[str, float]]:
    """{夜盤日期: {時段: 報酬點數}}（通過品質篩選的夜、且該時段夠完整）。"""
    out: dict[str, dict[str, float]] = {}
    for d in sorted(nights):
        if not clean_night(nights[d]):
            continue
        row = {}
        for name, a, b, _ in WINDOWS:
            r = window_return(nights[d], a, b)
            if r is not None:
                row[name] = r
        if row:
            out[d] = row
    return out


def split_by_date(rets: dict[str, dict[str, float]], name: str, start: str, end: Optional[str] = None) -> tuple[list[str], np.ndarray]:
    ds = [d for d in sorted(rets) if d >= start and (end is None or d <= end) and name in rets[d]]
    return ds, np.array([rets[d][name] for d in ds], dtype=float)


def one_sided_t(x: np.ndarray) -> Optional[float]:
    """一晚一筆、獨立：平均 ÷ 標準誤（不用 Newey-West：夜與夜之間的重疊只有隔夜的價差，這裡的報酬都在同一晚裡）。"""
    n = len(x)
    if n < 3:
        return None
    sd = float(np.std(x, ddof=1))
    return float(np.mean(x) / (sd / n ** 0.5)) if sd > 0 else None


def explore(rets: dict[str, dict[str, float]]) -> dict[str, Any]:
    """第一關：只用探索期（日期 ≤ EXPLORE_END）。回傳每個時段的統計與帶過去的名單。"""
    out: dict[str, Any] = {}
    carried = []
    for name, _, _, label in WINDOWS:
        ds, x = split_by_date(rets, name, "0000-00-00", EXPLORE_END)
        net = x - COST
        t = one_sided_t(net)
        row = {"label": label, "n": len(x), "mean": float(np.mean(x)) if len(x) else None, "net": float(np.mean(net)) if len(x) else None, "t_net": t}
        row["carry"] = bool(len(x) >= 30 and t is not None and t >= T_CARRY)           # t ≥ 1.645 就已經是淨平均 > 0
        if row["carry"]:
            carried.append(name)
        out[name] = row
    return {"windows": out, "carried": carried}


def day_control(day_minutes: dict[str, Sequence[tuple[int, float, float]]], length: int, start: str, end: Optional[str] = None) -> Optional[float]:
    """對照：同樣長度、從日盤第一根開始的窗口的平均報酬（點）。day_minutes＝{日期: [(第幾根（從 1）, 開, 收)…]}；長度超過日盤（300）就用整個日盤。"""
    n = min(length, 300)
    xs = []
    for d in sorted(day_minutes):
        if d < start or (end is not None and d > end):
            continue
        bars = [b for b in day_minutes[d] if b[0] <= n]
        if len(bars) >= MIN_COVER * n:
            xs.append((bars[-1][2] / bars[0][1] - 1.0) * PTS)
    return float(np.mean(xs)) if xs else None


def z_for(m: int) -> float:
    return NormalDist().inv_cdf(1 - 0.05 / m)


def _halves(ds: Sequence[str], x: np.ndarray) -> tuple[Optional[float], Optional[float]]:
    """依日期中位數分成前後兩半，各自扣成本後的平均。"""
    if len(ds) < 6:
        return None, None
    mid = ds[len(ds) // 2]
    a = np.array([v for d, v in zip(ds, x) if d < mid])
    b = np.array([v for d, v in zip(ds, x) if d >= mid])
    return (float(np.mean(a) - COST) if len(a) else None), (float(np.mean(b) - COST) if len(b) else None)


def risk(ds: Sequence[str], x: np.ndarray) -> dict[str, Any]:
    if not len(x):
        return {}
    net = x - COST
    cum = np.cumsum(net)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return {"sd": float(np.std(x, ddof=1)) if len(x) > 1 else None, "p5": float(np.percentile(x, 5)), "worst": float(np.min(x)),
            "max_drawdown": float(np.max(peak - cum)), "ntd_per_point": 10.0}


def holdout(rets: dict[str, dict[str, float]], carried: Sequence[str], control: dict[str, Optional[float]]) -> dict[str, Any]:
    """第二關：保留期，只跑一次。"""
    m = len(carried)
    out: dict[str, Any] = {"m": m, "z": z_for(m) if m else None, "windows": {}}
    for name in carried:
        ds, x = split_by_date(rets, name, HOLDOUT_START)
        net = x - COST
        t = one_sided_t(net)
        h1, h2 = _halves(ds, x)
        ctl = control.get(name)
        mean = float(np.mean(x)) if len(x) else None
        ok = bool(m and t is not None and t >= z_for(m) and len(x) >= MIN_NIGHTS and (h1 or 0) > 0 and (h2 or 0) > 0 and ctl is not None and mean is not None and mean > ctl)
        out["windows"][name] = {"n": len(x), "mean": mean, "net": float(np.mean(net)) if len(x) else None, "t_net": t, "first": h1, "second": h2,
                                "day_control": ctl, "pass": ok, "risk": risk(ds, x)}
    return out


def by_year(rets: dict[str, dict[str, float]], names: Sequence[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        yrs = sorted({d[:4] for d in rets if name in rets[d]})
        out[name] = {y: {"n": sum(1 for d in rets if d[:4] == y and name in rets[d]),
                         "mean": float(np.mean([rets[d][name] for d in rets if d[:4] == y and name in rets[d]]))} for y in yrs}
    return out


def run(nights: dict[str, Sequence[Bar]], day_minutes: dict[str, Sequence[tuple[int, float, float]]]) -> dict[str, Any]:
    rets = night_returns(nights)
    ex = explore(rets)
    control = {name: day_control(day_minutes, b - a + 1, HOLDOUT_START) for name, a, b, _ in WINDOWS}
    ho = holdout(rets, ex["carried"], control)
    full = {}
    for name, _, _, label in WINDOWS:
        ds, x = split_by_date(rets, name, "0000-00-00")
        full[name] = {"label": label, "n": len(x), "mean": float(np.mean(x)) if len(x) else None, "t": one_sided_t(x)}
    dropped = len(nights) - len(rets)
    ho_dates = [d for d in rets if d >= HOLDOUT_START]
    return {"nights": len(nights), "used": len(rets), "dropped": dropped, "first": min(rets) if rets else None, "last": max(rets) if rets else None,
            "holdout_nights": len(ho_dates), "explore": ex, "holdout": ho, "full": full, "by_year": by_year(rets, ex["carried"]),
            "day_control": control,
            "verdict": ("沒有帶到第二關的時段" if not ex["carried"] else ("過關：" + "、".join(n for n, w in ho["windows"].items() if w["pass"]))
                        if any(w["pass"] for w in ho["windows"].values()) else "沒過")}


def _f(x: Optional[float], spec: str = "+.1f") -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    L = [f"夜盤時段檢定：{r['nights']} 晚、使用 {r['used']} 晚（品質篩選丟掉 {r['dropped']}）、{r['first']}～{r['last']}；保留期 {r['holdout_nights']} 晚",
         "全樣本（描述）：" + "；".join(f"{n} {v['label']} 平均 {_f(v['mean'])} 點（t {_f(v['t'], '+.1f')}、{v['n']} 晚）" for n, v in r["full"].items()),
         "第一關（探索期）：" + "；".join(f"{n} 淨 {_f(v['net'])}、t {_f(v['t_net'], '+.2f')}{' ← 帶過去' if v['carry'] else ''}" for n, v in r["explore"]["windows"].items())]
    ho = r["holdout"]
    if not ho["windows"]:
        L.append("第二關：沒有時段帶過來")
    for n, w in ho["windows"].items():
        k = w["risk"]
        L.append(f"第二關 {n}（m＝{ho['m']}，要 t ≥ {ho['z']:.2f}）：{w['n']} 晚、毛 {_f(w['mean'])}、淨 {_f(w['net'])} 點、t {_f(w['t_net'], '+.2f')}、前半 {_f(w['first'])}／後半 {_f(w['second'])}、"
                 f"日盤對照 {_f(w['day_control'])} → {'過關' if w['pass'] else '沒過'}；每晚標準差 {_f(k.get('sd'), '.0f')}、第 5 百分位 {_f(k.get('p5'), '.0f')}、最差 {_f(k.get('worst'), '.0f')}、累積最大回落 {_f(k.get('max_drawdown'), '.0f')} 點")
    for n, ys in r["by_year"].items():
        L.append(f"{n} 每年平均：" + "；".join(f"{y} {_f(v['mean'], '+.1f')}（{v['n']} 晚）" for y, v in ys.items()))
    L.append("結論：" + r["verdict"])
    return "\n".join(L)


def load_inputs() -> tuple[dict[str, list[Bar]], dict[str, list[tuple[int, float, float]]]]:  # pragma: no cover
    from core.market_store import MarketStore
    from core.night_history import NightStore
    nights = NightStore().nights()
    store = MarketStore()
    import sqlite3
    from contextlib import closing
    day: dict[str, list[tuple[int, float, float]]] = {}
    with closing(sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)) as c:
        for d, hhmm, o, cl in c.execute("SELECT date, hhmm, open, close FROM bars_1m WHERE code='TXF' ORDER BY date, hhmm"):
            h, m = divmod(int(hhmm), 100)
            day.setdefault(d, []).append((h * 60 + m - (8 * 60 + 45), float(o), float(cl)))     # 結束時間 08:46 → 第 1 根
    return nights, day


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "run":
        print(__doc__)
        return 2
    if RESULT_PATH.exists() and "--rerun" not in a:
        print("已經跑過一次（判定標準只跑一次）；結果：\n" + format_report(json.loads(RESULT_PATH.read_text(encoding="utf-8"))))
        return 0
    nights, day = load_inputs()
    r = run(nights, day)
    RESULT_PATH.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(format_report(r))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
