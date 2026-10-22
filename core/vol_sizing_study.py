"""依預期振幅決定口數，每天承受的波動會不會更穩（update.md 發現 61）。離線分析，不碰交易路徑。

判定標準在跑之前寫進 update.md。三種定口數：固定 1 口／∝ 1÷R20／∝ 1÷IV 模型預測（發現 54 的 [1, ln R20, x_iv]，擴張視窗樣本外）。
每天承受量＝口數 × 當天日盤高低差；比「尾部比」＝承受量 99 百分位 ÷ 平均（與口數的整體縮放無關）。
檢定 A＝IV 模型 vs 固定、B＝IV 模型 vs R20；統計量＝尾部比的相對變化；95% 區間＝移動區塊重抽（20 天一塊、5,000 次）。
過關：探索期與保留期的區間上限都 < 0，且點估計都至少下降 A 10%、B 3%。
用法：python -m core.vol_sizing_study run（結果存 data/vol_sizing_result.json，只跑一次；--rerun 才重跑）"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from core.intraday_context import WARM, Session, oos_errors
from core.iv_range_study import IV_DIR, SPLIT, build

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "vol_sizing_result.json"
Q = 99.0
BLOCK, N_BOOT, SEED = 20, 5000, 20261022
MIN_DROP = {"A": 0.10, "B": 0.03}
CONTROL = {"A": "fixed", "B": "r20"}


def tail_ratio(x: np.ndarray) -> float:
    return float(np.percentile(x, Q) / x.mean())


def exposures(dates: np.ndarray, y_range: np.ndarray, lnR: np.ndarray, x: np.ndarray) -> Optional[dict[str, np.ndarray]]:
    """樣本外的每一天：三種定口數的承受量（口數 × 實際高低差；口數沒有縮放，尾部比與縮放無關）、口數本身。樣本不夠回 None。"""
    ok = np.isfinite(y_range) & np.isfinite(lnR) & np.isfinite(x)
    d, yy, rr, xx = dates[ok], y_range[ok], lnR[ok], x[ok]
    if len(yy) < WARM + 60:
        return None
    X1 = np.column_stack([np.ones(len(yy)), rr, xx])
    pred = yy[WARM:] - oos_errors(X1, yy)                       # 第 i 天的預測只用 i 之前的樣本
    actual = np.exp(yy[WARM:])
    lots = {"fixed": np.ones(len(actual)), "r20": np.exp(-rr[WARM:]), "iv": np.exp(-pred)}
    out = {k: v * actual for k, v in lots.items()}
    out["dates"] = d[WARM:]
    out["lots_iv"] = lots["iv"] / np.median(lots["iv"])
    return out


def block_ci(a: np.ndarray, b: np.ndarray, rng: np.random.Generator, block: int = BLOCK, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """tail(a)/tail(b) − 1 的點估計與 95% 區間（移動區塊重抽，a、b 用同一組日子）。"""
    n = len(a)
    point = tail_ratio(a) / tail_ratio(b) - 1
    k = int(np.ceil(n / block))
    stats = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n - block + 1, k)
        idx = (starts[:, None] + np.arange(block)).ravel()[:n]
        stats[i] = tail_ratio(a[idx]) / tail_ratio(b[idx]) - 1
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(point), float(lo), float(hi)


def describe(e: np.ndarray) -> dict[str, float]:
    s = e / e.mean()
    return {"tail": tail_ratio(e), "cv": float(s.std()), "worst5": float(np.sort(s)[-5:].mean()), "max": float(s.max())}


def evaluate(ex: dict[str, np.ndarray], seed: int = SEED, n_boot: int = N_BOOT) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    d = ex["dates"]
    out: dict[str, Any] = {"n_oos": int(len(d)), "tests": {}, "describe": {}, "yearly": {}}
    for seg, m in (("explore", d < SPLIT), ("holdout", d >= SPLIT)):
        out["describe"][seg] = {k: describe(ex[k][m]) for k in ("fixed", "r20", "iv")} if m.sum() >= 60 else None
        for t, ctrl in CONTROL.items():
            if m.sum() < 60:
                out["tests"].setdefault(t, {})[seg] = None
                continue
            p, lo, hi = block_ci(ex["iv"][m], ex[ctrl][m], rng, n_boot=n_boot)
            out["tests"].setdefault(t, {})[seg] = {"n": int(m.sum()), "change": p, "lo": lo, "hi": hi}
    for yr in sorted({s[:4] for s in d}):
        m = np.array([s[:4] == yr for s in d])
        if m.sum() >= 60:
            out["yearly"][yr] = {k: tail_ratio(ex[k][m]) for k in ("fixed", "r20", "iv")}
    lv = ex["lots_iv"]
    out["lots_iv"] = {"min": float(lv.min()), "p5": float(np.percentile(lv, 5)), "median": 1.0, "p95": float(np.percentile(lv, 95)), "max": float(lv.max())}
    return out


def passes(r: dict[str, Any], t: str) -> bool:
    segs = [r["tests"][t].get(s) for s in ("explore", "holdout")]
    return all(s is not None and s["hi"] < 0 and s["change"] <= -MIN_DROP[t] for s in segs)


def run(sessions: Sequence[Session], iv: dict[str, dict[str, Any]], n_boot: int = N_BOOT) -> dict[str, Any]:
    b = build(sessions, iv)
    out: dict[str, Any] = {"split": SPLIT, "n_sessions": len(sessions), "q": Q, "block": BLOCK, "n_boot": n_boot, "min_drop": MIN_DROP, "verdict": {}, "canary": None}
    ex = exposures(b["dates"], b["range"], b["lnR"], b["x_prev"])
    if ex is None:
        out["result"] = None
        out["verdict"] = {t: "沒有樣本" for t in CONTROL}
        return out
    r = evaluate(ex, n_boot=n_boot)
    out["result"] = r
    out["verdict"] = {t: "過關" if passes(r, t) else "沒過" for t in CONTROL}
    same = exposures(b["dates"], b["range"], b["lnR"], b["x_same"])            # 金絲雀：用同一天的 IV（偷看）應該更穩
    if same is not None:
        prev_t, same_t = tail_ratio(ex["iv"]), tail_ratio(same["iv"])
        out["canary"] = {"prev_tail": prev_t, "same_day_tail": same_t, "suspect": bool(prev_t < same_t)}
    return out


def format_report(r: dict[str, Any]) -> str:
    L = [f"依預期振幅定口數  日盤 {r['n_sessions']} 個（探索期 < {r['split']}，保留期 ≥ {r['split']}）；尾部比＝承受量 {r['q']:.0f} 百分位 ÷ 平均",
         f"過關：兩段 95% 區間上限 < 0，且下降至少 A {r['min_drop']['A']:.0%}（vs 固定）、B {r['min_drop']['B']:.0%}（vs R20）\n"]
    res = r.get("result")
    if not res:
        return "\n".join(L + ["樣本不夠"])
    names = {"fixed": "固定", "r20": "R20", "iv": "IV 模型"}
    for seg in ("explore", "holdout"):
        ds = res["describe"][seg]
        if ds:
            L.append(f"{'探索期' if seg == 'explore' else '保留期'}：" + "；".join(
                f"{names[k]} 尾部比 {v['tail']:.2f}、標準差÷平均 {v['cv']:.2f}、最慘 5 天 {v['worst5']:.2f} 倍平均" for k, v in ds.items()))
    for t, ctrl in CONTROL.items():
        segs = res["tests"][t]
        txt = "、".join(f"{'探索' if s == 'explore' else '保留'} {v['change']:+.1%}（{v['lo']:+.1%}～{v['hi']:+.1%}）" for s, v in segs.items() if v)
        L.append(f"{t}：IV 模型 vs {names[ctrl]}  {txt}  → {r['verdict'][t]}")
    L.append("每年尾部比：" + "；".join(f"{y} 固定 {v['fixed']:.2f}／R20 {v['r20']:.2f}／IV {v['iv']:.2f}" for y, v in res["yearly"].items()))
    lv = res["lots_iv"]
    L.append(f"IV 模型口數（中位數＝1）：最小 {lv['min']:.2f}、5% {lv['p5']:.2f}、95% {lv['p95']:.2f}、最大 {lv['max']:.2f}")
    c = r.get("canary")
    if c:
        L.append(f"金絲雀：前一天 IV 尾部比 {c['prev_tail']:.3f} vs 同一天 IV（偷看）{c['same_day_tail']:.3f} → {'⚠ 前一天比偷看還穩，管線有問題' if c['suspect'] else '正常'}")
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
    from core.intraday_context import load_sessions
    from core.market_store import MarketStore
    from core.vrp_study import load_iv
    res = run(load_sessions(MarketStore(), "TXF"), load_iv(IV_DIR))
    RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
