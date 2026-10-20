"""前一天的 ATM IV，能不能多預測隔天日盤「會走多遠」（update.md 發現 54）。離線分析，不碰交易路徑。

判定標準在跑之前寫進 update.md。資料：TXF 日盤 1 分 K（bars_1m）＋ data/taifex/vrp 的每日 ATM IV（發現 27）。
預測因子只用「前一個有 IV 的日子」（嚴格早於當天、≤ 4 個日曆天）：x_iv＝ln(IV÷100 × F)；目標 Y₁＝ln(日盤高低差)、Y₂＝ln(|收盤 − 開盤|)。
模型：基準 [1, ln R20]；候選 [1, ln R20, x_iv]；擴張視窗樣本外、Diebold-Mariano（與發現 28 相同的 WARM、LAG）。
過關：探索期（< 2025-01-01）DM ≥ 1.96、保留期 DM ≥ 1.645、兩段 MSE 降幅各 ≥ 2%、全期 x_iv 係數 > 0。
用法：python -m core.iv_range_study run（結果存 data/iv_range_result.json，只跑一次；--rerun 才重跑）"""
from __future__ import annotations

import json
import sys
from bisect import bisect_left
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from core.intraday_context import WARM, Session, dm_t, ols_hac, oos_errors, ref_ranges

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "iv_range_result.json"
IV_DIR = ROOT / "data" / "taifex" / "vrp"
SPLIT = "2025-01-01"
MAX_STALE_DAYS = 4                 # 前一個 IV 日離當天最多幾個日曆天
Z_EXPLORE, Z_HOLDOUT = 1.96, 1.645
MIN_REDUCTION = 0.02
TARGETS = ("range", "move")


def _day_arrays(sessions: Sequence[Session]) -> dict[str, np.ndarray]:
    """每個日盤的高低差、開盤（第一根的開）、收盤（最後一根的收）。"""
    n = len(sessions)
    rng, op, cl = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for i, s in enumerate(sessions):
        if np.isfinite(s.hi).any() and np.isfinite(s.lo).any():
            rng[i] = np.nanmax(s.hi) - np.nanmin(s.lo)
        j, k = np.flatnonzero(np.isfinite(s.op)), np.flatnonzero(np.isfinite(s.cl))
        if len(j) and len(k):
            op[i], cl[i] = s.op[j[0]], s.cl[k[-1]]
    return {"range": rng, "op": op, "cl": cl}


def build(sessions: Sequence[Session], iv: dict[str, dict[str, Any]]) -> dict[str, np.ndarray]:
    """每個日盤：日期、ln R20、兩個目標、前一個 IV 日的 x_iv（嚴格早於當天）、同一天的 x_iv（金絲雀，偷看，不能用）。"""
    dates = [s.date for s in sessions]
    t = _day_arrays(sessions)
    R = ref_ranges(sessions)
    with np.errstate(all="ignore"):
        y_range = np.log(t["range"])
        move = np.abs(t["cl"] - t["op"])
        y_move = np.where(move > 0, np.log(np.where(move > 0, move, 1.0)), np.nan)
        lnR = np.log(R)
    iv_dates = sorted(iv)
    n = len(dates)
    x_prev, x_same = np.full(n, np.nan), np.full(n, np.nan)

    def x_of(d: str) -> float:
        v = iv[d]
        return float(np.log(v["iv"] / 100.0 * v["F"])) if v["iv"] > 0 and v["F"] > 0 else float("nan")

    for i, d in enumerate(dates):
        j = bisect_left(iv_dates, d)                                  # iv_dates[j] ≥ d；j−1 是嚴格早於 d 的最近一天
        if j > 0 and (date.fromisoformat(d) - date.fromisoformat(iv_dates[j - 1])).days <= MAX_STALE_DAYS:
            x_prev[i] = x_of(iv_dates[j - 1])
        if j < len(iv_dates) and iv_dates[j] == d:
            x_same[i] = x_of(d)
    return {"dates": np.array(dates), "lnR": lnR, "range": y_range, "move": y_move, "x_prev": x_prev, "x_same": x_same}


def evaluate(dates: np.ndarray, y: np.ndarray, lnR: np.ndarray, x: np.ndarray) -> Optional[dict[str, Any]]:
    """一個目標、一個因子：樣本外誤差（基準 vs 候選）、探索期／保留期的 DM 與 MSE 降幅、全期係數、每年降幅。樣本不夠回 None。"""
    ok = np.isfinite(y) & np.isfinite(lnR) & np.isfinite(x)
    d, yy, rr, xx = dates[ok], y[ok], lnR[ok], x[ok]
    if len(yy) < WARM + 60:
        return None
    X0 = np.column_stack([np.ones(len(yy)), rr])
    X1 = np.column_stack([np.ones(len(yy)), rr, xx])
    e0, e1 = oos_errors(X0, yy), oos_errors(X1, yy)
    od = d[WARM:]
    out: dict[str, Any] = {"n": int(len(yy)), "n_oos": int(len(e0))}
    for name, m in (("explore", od < SPLIT), ("holdout", od >= SPLIT)):
        if m.sum() < 30:
            out[name] = {"n": int(m.sum()), "dm": None, "reduction": None}
            continue
        out[name] = {"n": int(m.sum()), "dm": float(dm_t(e0[m], e1[m])), "reduction": float(1 - (e1[m] ** 2).mean() / (e0[m] ** 2).mean())}
    out["full_reduction"] = float(1 - (e1 ** 2).mean() / (e0 ** 2).mean())
    beta, se = ols_hac(X1, yy)
    b0, se0 = ols_hac(X0, yy)
    r2 = lambda X, b: float(1 - ((yy - X @ b) ** 2).sum() / ((yy - yy.mean()) ** 2).sum())    # noqa: E731
    out["coef"] = {"x_iv": float(beta[2]), "x_iv_t": float(beta[2] / se[2]), "lnR": float(beta[1]), "lnR_t": float(beta[1] / se[1])}
    out["r2"] = {"base": r2(X0, b0), "cand": r2(X1, beta)}
    out["corr_x_lnR"] = float(np.corrcoef(xx, rr)[0, 1])
    yearly = {}
    for yr in sorted({s[:4] for s in od}):
        m = np.array([s[:4] == yr for s in od])
        if m.sum() >= 30:
            yearly[yr] = {"n": int(m.sum()), "reduction": float(1 - (e1[m] ** 2).mean() / (e0[m] ** 2).mean())}
    out["yearly"] = yearly
    return out


def passes(r: Optional[dict[str, Any]]) -> bool:
    if not r or r["explore"]["dm"] is None or r["holdout"]["dm"] is None:
        return False
    return bool(r["explore"]["dm"] >= Z_EXPLORE and r["holdout"]["dm"] >= Z_HOLDOUT and r["explore"]["reduction"] >= MIN_REDUCTION
                and r["holdout"]["reduction"] >= MIN_REDUCTION and r["coef"]["x_iv"] > 0)


def run(sessions: Sequence[Session], iv: dict[str, dict[str, Any]]) -> dict[str, Any]:
    b = build(sessions, iv)
    out: dict[str, Any] = {"split": SPLIT, "n_sessions": len(sessions), "z_explore": Z_EXPLORE, "z_holdout": Z_HOLDOUT, "min_reduction": MIN_REDUCTION, "tests": {}, "canary": {}, "verdict": {}}
    for k in TARGETS:
        r = evaluate(b["dates"], b[k], b["lnR"], b["x_prev"])
        out["tests"][k] = r
        out["verdict"][k] = "沒有樣本" if r is None else "過關" if passes(r) else "沒過"
        same = evaluate(b["dates"], b[k], b["lnR"], b["x_same"])
        prev_red = None if r is None else r["full_reduction"]
        same_red = None if same is None else same["full_reduction"]
        out["canary"][k] = {"prev_reduction": prev_red, "same_day_reduction": same_red,
                            "suspect": bool(prev_red is not None and same_red is not None and prev_red > same_red)}
    return out


def _f(x: Optional[float], spec: str) -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    L = [f"前一天 ATM IV → 隔天日盤振幅  日盤 {r['n_sessions']} 個（探索期 < {r['split']}，保留期 ≥ {r['split']}）",
         f"過關：探索 DM ≥ {r['z_explore']}、保留 DM ≥ {r['z_holdout']}、兩段 MSE 降幅 ≥ {r['min_reduction']:.0%}、全期 x_iv 係數 > 0\n"]
    for k, t in r["tests"].items():
        name = "日盤高低差" if k == "range" else "日盤收開差"
        if t is None:
            L.append(f"{name}：樣本不夠")
            continue
        e, h, c = t["explore"], t["holdout"], t["coef"]
        L.append(f"{name}  n={t['n']}（樣本外 {t['n_oos']}）  探索 DM={_f(e['dm'], '.2f')} 降幅 {_f(e['reduction'], '.1%')}  保留 DM={_f(h['dm'], '.2f')} 降幅 {_f(h['reduction'], '.1%')}  → {r['verdict'][k]}")
        L.append(f"   x_iv 係數 {c['x_iv']:.3f}（HAC t {c['x_iv_t']:.1f}）、ln R20 係數 {c['lnR']:.3f}（t {c['lnR_t']:.1f}）；R² {t['r2']['base']:.3f} → {t['r2']['cand']:.3f}；x_iv 與 ln R20 相關 {t['corr_x_lnR']:.2f}")
        L.append("   每年降幅：" + "、".join(f"{y} {v['reduction']:.1%}(n{v['n']})" for y, v in t["yearly"].items()))
        cn = r["canary"][k]
        L.append(f"   金絲雀：前一天 IV 降幅 {_f(cn['prev_reduction'], '.1%')} vs 同一天 IV（偷看）{_f(cn['same_day_reduction'], '.1%')} → {'⚠ 前一天比同一天還大，管線有問題' if cn['suspect'] else '正常'}")
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
