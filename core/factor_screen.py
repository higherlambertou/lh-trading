"""在既有資料裡找進場因子（update.md 發現 45）：探索期篩選＋保留期只驗一次。離線分析，不碰交易路徑。

判定標準在跑之前寫進 update.md。資料＝bars_1m（TXF 日盤 1 分 K）；探索期 2020-03-02～2024-12-31、保留期 2025-01-01 以後（探索時不看）。
決策時點：第 14、29、…、284 分鐘收完時；因子只用到那一分鐘（含）以前與前一天的資料；進場＝下一分鐘開盤、持有 H 分鐘。
用法：python -m core.factor_screen run（結果存 data/factor_screen_result.json，只跑一次）"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional, Sequence

import numpy as np

from core import signal_rules as sr
from core.signal_study import N_MIN, Panel, build_panel, cluster_t

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "factor_screen_result.json"
SPLIT = "2025-01-01"
POINTS_M = tuple(range(14, 285, 15))          # 09:00、09:15、…、13:30
HORIZONS = (15, 30)
FACTORS = ("r5", "r15", "r_open", "gap", "range_pos", "vwap_dev", "vol15", "rv15", "prev_ret", "prev_pos", "d_prev_high", "d_prev_low")
T1 = 3.08                                      # 雙尾 α＝0.05、Bonferroni m＝24
COST_PTS = 4.0
QUANT = 0.2


def _base(x: np.ndarray, n_base: int = 20, min_base: int = 10) -> np.ndarray:
    return sr.baseline(x, n_base, min_base)


def features(p: Panel) -> dict[str, np.ndarray]:
    """每個因子：(天數, 300) 的陣列，第 m 欄只用到第 m 分鐘（含）以前與前一天的資料。"""
    D = len(p.dates)
    c, o, h, lo, v = p.c, p.o, p.h, p.l, p.v
    f: dict[str, np.ndarray] = {k: np.full((D, N_MIN), np.nan) for k in FACTORS}
    with np.errstate(all="ignore"):
        f["r5"][:, 5:] = c[:, 5:] / c[:, :-5] - 1
        f["r15"][:, 15:] = c[:, 15:] / c[:, :-15] - 1
        f["r_open"] = c / o[:, [0]] - 1
        hi = np.fmax.accumulate(h, axis=1)
        low = np.fmin.accumulate(lo, axis=1)
        f["range_pos"] = (c - low) / (hi - low)
        tp = (h + lo + c) / 3
        vv = np.where(np.isfinite(v), v, 0.0)
        f["vwap_dev"] = c / (np.cumsum(np.where(np.isfinite(tp), tp, 0.0) * vv, axis=1) / np.cumsum(vv, axis=1)) - 1
        w15 = np.vstack([sr.rolling_sum(v[d], 15) for d in range(D)]) if D else np.empty((0, N_MIN))
        f["vol15"] = w15 / _base(w15)
        dc = np.diff(c, axis=1, prepend=np.nan)
        rv = np.full((D, N_MIN), np.nan)
        for m in range(15, N_MIN):
            rv[:, m] = np.nanstd(dc[:, m - 14:m + 1], axis=1, ddof=1)
        f["rv15"] = rv / _base(rv)
        pc, ph, pl = np.full(D, np.nan), np.full(D, np.nan), np.full(D, np.nan)
        ppc = np.full(D, np.nan)
        pc[1:], ph[1:], pl[1:] = c[:-1, -1], np.nanmax(h[:-1], axis=1), np.nanmin(lo[:-1], axis=1)
        ppc[2:] = c[:-2, -1]
        f["gap"] = np.repeat((o[:, 0] / pc - 1)[:, None], N_MIN, axis=1)
        f["prev_ret"] = np.repeat((pc / ppc - 1)[:, None], N_MIN, axis=1)
        f["prev_pos"] = np.repeat(((pc - pl) / (ph - pl))[:, None], N_MIN, axis=1)
        f["d_prev_high"] = c / ph[:, None] - 1
        f["d_prev_low"] = c / pl[:, None] - 1
    return f


def samples(p: Panel, f: dict[str, np.ndarray], h: int) -> tuple[np.ndarray, dict[str, np.ndarray], np.ndarray]:
    """(日期序號, 各因子值, 報酬)；只取決策時點且出場在收盤前、進出場價都有的。"""
    ds, ms = [], []
    for m in POINTS_M:
        if m + h <= N_MIN - 1:
            for d in range(len(p.dates)):
                ds.append(d)
                ms.append(m)
    ds, ms = np.array(ds, int), np.array(ms, int)
    y = p.c[ds, ms + h] / p.o[ds, ms + 1] - 1 if len(ds) else np.array([])
    x = {k: f[k][ds, ms] for k in FACTORS}
    return ds, x, y


def _rank(x: np.ndarray) -> np.ndarray:
    """百分位（−0.5～+0.5）；NaN 維持 NaN。"""
    out = np.full(len(x), np.nan)
    ok = np.isfinite(x)
    if ok.sum() > 1:
        r = x[ok].argsort().argsort()
        out[ok] = r / (ok.sum() - 1) - 0.5
    return out


def slope_t(x: np.ndarray, y: np.ndarray, g: np.ndarray) -> tuple[float, float]:
    """y 對 x 的斜率與依分群的 t（x 已置中）。"""
    ok = np.isfinite(x) & np.isfinite(y)
    x, y, g = x[ok], y[ok], g[ok]
    if len(x) < 10:
        return float("nan"), float("nan")
    xc = x - x.mean()
    b = float((xc * (y - y.mean())).sum() / (xc ** 2).sum())
    e = (y - y.mean() - b * xc) * xc
    s2 = sum(float(e[g == k].sum()) ** 2 for k in np.unique(g))
    se = s2 ** 0.5 / (xc ** 2).sum()
    return b, (b / se if se > 0 else float("nan"))


def trade(x: np.ndarray, y: np.ndarray, lo_cut: float, hi_cut: float, sign: int) -> np.ndarray:
    """因子 ≥ hi_cut 做 sign 方向、≤ lo_cut 做反方向；回傳每筆的毛報酬（比例）。"""
    ok = np.isfinite(x) & np.isfinite(y)
    pos = np.where(x >= hi_cut, sign, np.where(x <= lo_cut, -sign, 0))
    sel = ok & (pos != 0)
    return pos[sel] * y[sel], sel


def run(rows: Sequence[dict[str, Any]], end: Optional[str] = None) -> dict[str, Any]:
    p = build_panel(rows, end=end)
    f = features(p)
    split_d = next((i for i, d in enumerate(p.dates) if d >= SPLIT), len(p.dates))
    cost = COST_PTS / sr.REF_PRICE
    res: dict[str, Any] = {"version": 1, "dates": [p.dates[0], p.dates[-1]] if p.dates else [], "split": SPLIT, "n_days": len(p.dates),
                           "n_disc_days": split_d, "stage1": {}, "carried": [], "stage2": {}}
    for h in HORIZONS:
        ds, x, y = samples(p, f, h)
        disc, hold = ds < split_d, ds >= split_d
        for k in FACTORS:
            xv = x[k]
            key = f"{k}/{h}"
            xd, yd, gd = xv[disc], y[disc], ds[disc]
            b, t = slope_t(_rank(xd), yd, gd)
            okd = np.isfinite(xd) & np.isfinite(yd)
            mid = np.median(gd[okd]) if okd.any() else 0
            b1, _ = slope_t(_rank(xd[gd <= mid]), yd[gd <= mid], gd[gd <= mid])
            b2, _ = slope_t(_rank(xd[gd > mid]), yd[gd > mid], gd[gd > mid])
            sign = 1 if b > 0 else -1
            fin = xd[np.isfinite(xd)]
            lo_c, hi_c = (float(np.quantile(fin, QUANT)), float(np.quantile(fin, 1 - QUANT))) if len(fin) else (np.nan, np.nan)
            g_tr, _ = trade(xd, yd, lo_c, hi_c, sign)
            gross_pts = float(g_tr.mean()) * sr.REF_PRICE if len(g_tr) else float("nan")
            passed = bool(np.isfinite(t) and abs(t) >= T1 and np.sign(b1) == np.sign(b2) == np.sign(b) and gross_pts >= COST_PTS)
            res["stage1"][key] = {"slope_pts": b * sr.REF_PRICE, "t": t, "half_same_sign": bool(np.sign(b1) == np.sign(b2) == np.sign(b)),
                                  "trade_gross_pts": gross_pts, "n_trades": int(len(g_tr)), "lo": lo_c, "hi": hi_c, "sign": sign, "pass": passed}
            if passed:
                res["carried"].append(key)
            res.setdefault("_hold", {})[key] = (xv[hold], y[hold], ds[hold])
    k_n = len(res["carried"])
    if k_n:
        crit = NormalDist().inv_cdf(1 - 0.05 / k_n)
        res["t2"] = crit
        for key in res["carried"]:
            s1 = res["stage1"][key]
            xh, yh, gh = res["_hold"][key]
            g_tr, sel = trade(xh, yh, s1["lo"], s1["hi"], s1["sign"])
            net = g_tr - cost
            mu, t2, nd = cluster_t(net, gh[sel])
            res["stage2"][key] = {"n": int(len(net)), "days": nd, "mean_net_pts": mu * sr.REF_PRICE, "t": t2,
                                  "mean_gross_pts": float(g_tr.mean()) * sr.REF_PRICE if len(g_tr) else float("nan"),
                                  "pass": bool(len(net) >= 100 and np.isfinite(t2) and t2 >= crit and mu > 0)}
    res.pop("_hold", None)
    res["passes"] = [k for k, v in res["stage2"].items() if v["pass"]]
    res["verdict"] = ("有線索：保留期也通過（仍需前向驗證）" if res["passes"] else
                      "探索期有因子過關，但保留期沒有通過" if res["carried"] else "探索期就沒有因子過關")
    return res


def format_report(r: dict[str, Any]) -> str:
    L = [f"進場因子篩選：{r['dates'][0]} ～ {r['dates'][1]}（探索期 {r['n_disc_days']} 天到 {r['split']} 前、之後是保留期）；成本 {COST_PTS:g} 點",
         f"第一關：|t| ≥ {T1}、前後兩半同號、最高／最低 20% 的交易版本毛報酬 ≥ {COST_PTS:g} 點", ""]
    for key, s in r["stage1"].items():
        L.append(f"  {key:<16} 斜率 {s['slope_pts']:+7.2f} 點/全距  t={s['t']:+6.2f}  兩半同號 {'是' if s['half_same_sign'] else '否'}  "
                 f"交易毛 {s['trade_gross_pts']:+6.2f} 點（{s['n_trades']} 筆）  {'→ 帶到第二關' if s['pass'] else ''}")
    if r["carried"]:
        L += ["", f"第二關（保留期，t ≥ {r['t2']:.2f}、淨報酬 > 0）："]
        for key, s in r["stage2"].items():
            L.append(f"  {key:<16} n={s['n']}  淨 {s['mean_net_pts']:+6.2f} 點  毛 {s['mean_gross_pts']:+6.2f}  t={s['t']:+5.2f}  {'✅ 通過' if s['pass'] else '—'}")
    L += ["", f"結論：{r['verdict']}"]
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "run":
        print(__doc__)
        return 2
    if RESULT_PATH.exists() and "--rerun" not in a:
        print("（已經跑過，只跑一次；以下是存的結果）\n")
        print(format_report(json.loads(RESULT_PATH.read_text(encoding="utf-8"))))
        return 0
    from core.market_store import MarketStore
    res = run(MarketStore().bars_1m("TXF"), end=a[a.index("--end") + 1] if "--end" in a else None)
    RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
