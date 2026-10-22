"""夜盤會走多遠能不能事先估（update.md 發現 67）。離線分析，不碰交易路徑。

判定標準在跑之前寫進 update.md。資料：期交所 TX 日行情（data/taifex/tx；盤後列的日期 D＝它之後那個日盤，所以夜盤 D＝前一個交易日 15:00 到 D 的 05:00）
＋ 每日 ATM IV（data/taifex/vrp）。每個夜盤取盤後成交量最大的月合約；目標 y＝ln(夜盤高低差)。
基準 [1, ln(前 20 個夜盤的平均高低差)]；T1 多加前一個交易日的 x_iv＝ln(IV×F)；T2 多加 ln(前一個日盤的高低差)。
擴張視窗樣本外、Diebold-Mariano；過關：探索期（< 2025-01-01）DM ≥ 1.96、保留期 DM ≥ 1.645、兩段 MSE 降幅各 ≥ 2%、全期係數 > 0。
用法：python -m core.night_range_study run（結果存 data/night_range_result.json，只跑一次；--rerun 才重跑）"""
from __future__ import annotations

import json
import sys
from bisect import bisect_left
from pathlib import Path
from typing import Any, Optional

import numpy as np

from core.intraday_context import WARM
from core.oi_study import TX_HEADER, _iso, _num, _rows
from core.range_predictors_study import compare

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "night_range_result.json"
TX_DIR = ROOT / "data" / "taifex" / "tx"
IV_DIR = ROOT / "data" / "taifex" / "vrp"
FIRST = "2017-05-16"
SPLIT = "2025-01-01"
N_REF = 20
Z_EXPLORE, Z_HOLDOUT = 1.96, 1.645
MIN_REDUCTION = 0.02
TESTS = ("T1", "T2")
TEST_TEXT = {"T1": "前一個交易日的 IV", "T2": "前一個日盤的高低差"}
TEST_COL = {"T1": "x_iv", "T2": "ln_day"}


def load_tx(tx_dir: Path = TX_DIR) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """期交所 TX 日行情 → (日盤：日期 → 日盤成交量最大的月合約的(高, 低)，夜盤：盤後列日期 → 盤後成交量最大的月合約的(高, 低))。
    只要 6 位數的月合約（價差、週合約不算）、成交量 > 0、高 ≥ 低。"""
    day: dict[str, dict[str, tuple[float, float, float]]] = {}
    night: dict[str, dict[str, tuple[float, float, float]]] = {}
    for f in sorted(tx_dir.glob("*.csv")):
        for r in _rows(f, TX_HEADER):
            code = (r.get("到期月份(週別)") or "").strip()
            if not code.isdigit() or len(code) != 6:
                continue
            vol, h, l = _num(r.get("成交量", "")), _num(r.get("最高價", "")), _num(r.get("最低價", ""))
            if not vol or vol <= 0 or h is None or l is None or h < l:
                continue
            d = _iso(r["交易日期"])
            sess = (r.get("交易時段") or "一般").strip()
            target = day if sess in ("一般", "") else night if sess == "盤後" else None
            if target is not None:
                target.setdefault(d, {})[code] = (h, l, vol)
    pick = lambda m: {d: (v[max(v, key=lambda k: v[k][2])][0], v[max(v, key=lambda k: v[k][2])][1]) for d, v in m.items()}     # noqa: E731
    return pick(day), pick(night)


def build(day: dict[str, tuple[float, float]], night: dict[str, tuple[float, float]], iv: dict[str, dict[str, Any]], first: str = FIRST) -> dict[str, np.ndarray]:
    """每個 ≥ first 的夜盤 D：y、ln R20（前 20 個夜盤）、前一個交易日（D 之前最近的日盤）的 x_iv 與 ln(日盤高低差)，以及金絲雀用的 D 當天（夜盤之後）的 x_iv。"""
    labels = sorted(night)
    days = sorted(day)
    nr = np.array([night[d][0] - night[d][1] for d in labels], dtype=float)

    def x_of(d: Optional[str]) -> float:
        v = iv.get(d) if d else None
        return float(np.log(v["iv"] / 100.0 * v["F"])) if v and v["iv"] > 0 and v["F"] > 0 else float("nan")

    cols: dict[str, list[Any]] = {k: [] for k in ("dates", "prev_day", "y", "lnR", "x_iv", "ln_day", "x_iv_after")}
    with np.errstate(divide="ignore", invalid="ignore"):
        for i, d in enumerate(labels):
            if d < first or i < N_REF:
                continue
            j = bisect_left(days, d)
            prev = days[j - 1] if j > 0 else None
            ref = nr[i - N_REF:i]
            dr = (day[prev][0] - day[prev][1]) if prev else float("nan")
            cols["dates"].append(d)
            cols["prev_day"].append(prev or "")
            cols["y"].append(np.log(nr[i]) if nr[i] > 0 else np.nan)
            cols["lnR"].append(np.log(ref.mean()) if ref.mean() > 0 else np.nan)
            cols["x_iv"].append(x_of(prev))
            cols["ln_day"].append(np.log(dr) if dr > 0 else np.nan)
            cols["x_iv_after"].append(x_of(d))
    return {k: np.array(v) if k in ("dates", "prev_day") else np.array(v, dtype=float) for k, v in cols.items()}


def passes(r: Optional[dict[str, Any]]) -> bool:
    if not r or r["explore"]["dm"] is None or r["holdout"]["dm"] is None:
        return False
    return bool(r["explore"]["dm"] >= Z_EXPLORE and r["holdout"]["dm"] >= Z_HOLDOUT and r["explore"]["reduction"] >= MIN_REDUCTION
                and r["holdout"]["reduction"] >= MIN_REDUCTION and r["coef"] > 0)


def run(day: dict[str, tuple[float, float]], night: dict[str, tuple[float, float]], iv: dict[str, dict[str, Any]]) -> dict[str, Any]:
    b = build(day, night, iv)
    ok = np.isfinite(b["y"]) & np.isfinite(b["lnR"]) & np.isfinite(b["x_iv"]) & np.isfinite(b["ln_day"])
    out: dict[str, Any] = {"split": SPLIT, "first": FIRST, "n_nights": int(len(b["dates"])), "n_joint": int(ok.sum()), "z_explore": Z_EXPLORE, "z_holdout": Z_HOLDOUT,
                           "min_reduction": MIN_REDUCTION, "missing": {k: int((~np.isfinite(b[k])).sum()) for k in ("y", "lnR", "x_iv", "ln_day")},
                           "tests": {}, "verdict": {}, "describe": {}, "canary": None}
    if ok.sum() < WARM + 60:
        out["verdict"] = {t: "沒有樣本" for t in TESTS}
        return out
    d, y = b["dates"][ok], b["y"][ok]
    col = lambda k: b[k][ok]                                                  # noqa: E731
    X0 = np.column_stack([np.ones(int(ok.sum())), col("lnR")])
    out["range"] = {"from": str(d[0]), "to": str(d[-1]), "oos_from": str(d[WARM])}
    for t in TESTS:
        r = compare(d, y, X0, np.column_stack([X0, col(TEST_COL[t])]))
        out["tests"][t] = r
        out["verdict"][t] = "過關" if passes(r) else "沒過"
    both = np.column_stack([X0, col("x_iv"), col("ln_day")])
    out["describe"]["both"] = compare(d, y, X0, both)
    chosen = [TEST_COL[t] for t in TESTS if out["verdict"][t] == "過關"]
    Xd = np.column_stack([X0] + [col(c) for c in chosen])
    beta = np.linalg.lstsq(Xd, y, rcond=None)[0]
    res = y - Xd @ beta
    out["describe"]["display_model"] = {"terms": ["const", "lnR"] + chosen, "beta": [float(x) for x in beta],
                                        "q16": float(np.quantile(res, 0.16)), "q50": float(np.quantile(res, 0.50)), "q84": float(np.quantile(res, 0.84)), "n": int(len(y))}
    m = np.isfinite(b["x_iv_after"][ok])
    if m.sum() >= WARM + 60:                                                  # 金絲雀：夜盤之後那天的 IV（已經包含這個夜盤＝偷看）應該降得更多
        X0m = X0[m]
        prev_red = compare(d[m], y[m], X0m, np.column_stack([X0m, col("x_iv")[m]]))["full_reduction"]
        after_red = compare(d[m], y[m], X0m, np.column_stack([X0m, col("x_iv_after")[m]]))["full_reduction"]
        out["canary"] = {"prev_reduction": prev_red, "after_reduction": after_red, "suspect": bool(prev_red >= after_red)}
    return out


def _f(x: Optional[float], spec: str) -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    L = [f"夜盤振幅  {r['n_nights']} 個夜盤、全部欄位都有 {r['n_joint']} 個" + (f"（{r['range']['from']}～{r['range']['to']}，樣本外從 {r['range']['oos_from']}）" if r.get("range") else ""),
         f"基準 [1, ln 前 20 個夜盤平均高低差]；過關：探索 DM ≥ {r['z_explore']}、保留 DM ≥ {r['z_holdout']}、兩段 MSE 降幅 ≥ {r['min_reduction']:.0%}、全期係數 > 0",
         "缺值：" + "、".join(f"{k} {v}" for k, v in r["missing"].items()) + "\n"]
    for t, v in r["tests"].items():
        e, h = v["explore"], v["holdout"]
        L.append(f"{t} {TEST_TEXT[t]}：探索 DM={_f(e['dm'], '.2f')} 降幅 {_f(e['reduction'], '.1%')}  保留 DM={_f(h['dm'], '.2f')} 降幅 {_f(h['reduction'], '.1%')}  "
                 f"係數 {v['coef']:.3f}（t {v['coef_t']:.1f}）  R² {v['r2']['base']:.3f}→{v['r2']['cand']:.3f}  → {r['verdict'][t]}")
        L.append("   每年降幅：" + "、".join(f"{yr} {w['reduction']:.1%}" for yr, w in v["yearly"].items()))
    ds = r.get("describe") or {}
    if ds.get("both"):
        a = ds["both"]
        L.append(f"\n（描述）兩項一起加：探索降幅 {_f(a['explore']['reduction'], '.1%')}、保留 {_f(a['holdout']['reduction'], '.1%')}；R² {a['r2']['base']:.3f}→{a['r2']['cand']:.3f}")
    if ds.get("display_model"):
        dm = ds["display_model"]
        L.append(f"（描述）畫面用的模型：{' + '.join(dm['terms'])}；殘差 16／50／84% 分位數 {dm['q16']:.3f}／{dm['q50']:.3f}／{dm['q84']:.3f}（n={dm['n']}）")
    c = r.get("canary")
    if c:
        L.append(f"金絲雀：前一天 IV 降幅 {c['prev_reduction']:.1%} vs 夜盤之後那天的 IV（偷看）{c['after_reduction']:.1%} → {'⚠ 前一天不比偷看差，管線有問題' if c['suspect'] else '正常'}")
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
    from core.vrp_study import load_iv
    day, night = load_tx(TX_DIR)
    res = run(day, night, load_iv(IV_DIR))
    RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
