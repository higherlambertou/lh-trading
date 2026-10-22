"""夜盤振幅、前一天成交值、漲跌家數，能不能多預測日盤振幅（update.md 發現 64）。離線分析，不碰交易路徑。

判定標準在跑之前寫進 update.md。資料：期交所 TX 日行情（data/taifex/tx；盤後列的日期＝它之後那個日盤）＋ ATM IV（data/taifex/vrp）＋ lh-intel 的證交所大盤成交金額與漲跌家數。
每天取日盤成交量最大的月合約；目標 y＝ln(日盤高低差)。基準＝畫面上的模型 [1, ln R20, x_iv]（發現 55）；三個檢定各多加一項：
  N＝ln(夜盤高低差)（同一個合約、開盤前已走完的那個夜盤）、U＝前一天成交值異常、B＝前一天漲跌家數一面倒的程度。
擴張視窗樣本外、Diebold-Mariano；過關：探索期（< 2025-01-01）DM ≥ 2.13、保留期 DM ≥ 1.645、兩段 MSE 降幅各 ≥ 2%、全期係數 > 0。
用法：python -m core.range_predictors_study run [--intel-db 路徑]（結果存 data/range_predictors_result.json，只跑一次；--rerun 才重跑）"""
from __future__ import annotations

import json
import sqlite3
import sys
from bisect import bisect_left
from contextlib import closing
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any, NamedTuple, Optional

import numpy as np

from core.intraday_context import WARM, dm_t, ols_hac, oos_errors
from core.oi_study import TX_HEADER, _iso, _num, _rows

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "range_predictors_result.json"
TX_DIR = ROOT / "data" / "taifex" / "tx"
IV_DIR = ROOT / "data" / "taifex" / "vrp"
DEFAULT_INTEL_DB = ROOT.parent / "lh-intel" / "data" / "intel.db"
FIRST = "2017-05-16"                    # 期交所盤後列從這天起
SPLIT = "2025-01-01"
N_REF = 20                              # R20 與成交值異常的回看天數
MAX_STALE_DAYS = 4
N_TESTS = 3
Z_EXPLORE = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)          # ≈ 2.13
Z_HOLDOUT = NormalDist().inv_cdf(0.95)                         # ≈ 1.645
MIN_REDUCTION = 0.02
TESTS = ("N", "U", "B")
TEST_TEXT = {"N": "夜盤振幅", "U": "前一天成交值異常", "B": "前一天漲跌家數一面倒"}


# ── 資料 ──────────────────────────────────────────────────────────

class TxDay(NamedTuple):
    contract: str
    h: float
    l: float
    nh: Optional[float]                 # 同一個合約的夜盤（盤後列）高、低；沒有是 None
    nl: Optional[float]


def load_tx(tx_dir: Path = TX_DIR) -> dict[str, TxDay]:
    """期交所 TX 日行情 → 日期 → 當天日盤成交量最大的月合約（只要 6 位數的月合約，價差與週合約不算）的日盤高低與同合約的夜盤高低。"""
    day: dict[str, dict[str, tuple[float, float, float]]] = {}
    night: dict[str, dict[str, tuple[float, float]]] = {}
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
            if sess in ("一般", ""):
                day.setdefault(d, {})[code] = (h, l, vol)
            elif sess == "盤後":
                night.setdefault(d, {})[code] = (h, l)
    out: dict[str, TxDay] = {}
    for d in sorted(day):
        c = max(day[d], key=lambda k: day[d][k][2])
        n = night.get(d, {}).get(c)
        out[d] = TxDay(c, day[d][c][0], day[d][c][1], n[0] if n else None, n[1] if n else None)
    return out


def load_twse(path: Path = DEFAULT_INTEL_DB) -> dict[str, dict[str, float]]:
    """lh-intel 的證交所日資料（唯讀）→ 日期 → {turnover, up, down}。每個日期取**最早的一版**（開盤前當時能看到的那一版，不用事後修正的）。
    這是研究讀取：官方收盤後公布的日統計是事實，且只用嚴格早於目標日的日期（見 build），所以不套 as_of。"""
    want = {"market.turnover_twd": "turnover", "breadth.stock.up": "up", "breadth.stock.down": "down"}
    out: dict[str, dict[str, float]] = {}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT o.series, o.event_date, o.value FROM observations o JOIN (SELECT series AS s, event_date AS d, min(revision) AS r FROM observations "
            "WHERE source='twse' AND series IN (?,?,?) AND event_date IS NOT NULL GROUP BY series, event_date) m "
            "ON o.series=m.s AND o.event_date=m.d AND o.revision=m.r WHERE o.source='twse' AND o.value IS NOT NULL", tuple(want)).fetchall()
    for series, d, v in rows:
        out.setdefault(d, {})[want[series]] = float(v)
    return dict(sorted(out.items()))


def _prev_index(keys: list[str], d: str, strict: bool = True) -> Optional[int]:
    """keys（已排序）裡嚴格早於 d（strict）或就是 d（not strict）、且 ≤ MAX_STALE_DAYS 個日曆天的那一個的位置；沒有 None。"""
    j = bisect_left(keys, d)
    if not strict:
        return j if j < len(keys) and keys[j] == d else None
    if j == 0 or (date.fromisoformat(d) - date.fromisoformat(keys[j - 1])).days > MAX_STALE_DAYS:
        return None
    return j - 1


def build(tx: dict[str, TxDay], iv: dict[str, dict[str, Any]], twse: dict[str, dict[str, float]], first: str = FIRST) -> dict[str, np.ndarray]:
    """每個 ≥ first 的日盤：y、ln R20、x_iv、N、U、B，以及描述用的 ln(前一天振幅)、金絲雀用的當天 U、B。缺的是 NaN。"""
    days = sorted(tx)
    rng = np.array([tx[d].h - tx[d].l for d in days], dtype=float)
    iv_dates = sorted(iv)
    tw_dates = sorted(twse)
    lt = np.array([np.log(twse[d]["turnover"]) if twse[d].get("turnover", 0) > 0 else np.nan for d in tw_dates])
    u_all = np.full(len(tw_dates), np.nan)
    for k in range(N_REF, len(tw_dates)):
        ref = lt[k - N_REF:k]
        if np.isfinite(ref).all():
            u_all[k] = lt[k] - ref.mean()

    def breadth(k: int) -> float:
        v = twse[tw_dates[k]]
        up, down = v.get("up"), v.get("down")
        return float(abs(up - down) / (up + down)) if up is not None and down is not None and up + down > 0 else float("nan")

    keys = ("dates", "y", "lnR", "x_iv", "N", "U", "B", "ln_prev", "U_same", "B_same")
    cols: dict[str, list[Any]] = {k: [] for k in keys}
    with np.errstate(divide="ignore", invalid="ignore"):
        for i, d in enumerate(days):
            if d < first or i < N_REF:
                continue
            t = tx[d]
            cols["dates"].append(d)
            cols["y"].append(np.log(rng[i]) if rng[i] > 0 else np.nan)
            ref = rng[i - N_REF:i]
            cols["lnR"].append(np.log(ref.mean()) if ref.mean() > 0 else np.nan)
            cols["ln_prev"].append(np.log(rng[i - 1]) if rng[i - 1] > 0 else np.nan)
            j = _prev_index(iv_dates, d)
            v = iv[iv_dates[j]] if j is not None else None
            cols["x_iv"].append(np.log(v["iv"] / 100.0 * v["F"]) if v and v["iv"] > 0 and v["F"] > 0 else np.nan)
            nr = (t.nh - t.nl) if t.nh is not None and t.nl is not None else None
            cols["N"].append(np.log(nr) if nr and nr > 0 else np.nan)
            k = _prev_index(tw_dates, d)
            cols["U"].append(u_all[k] if k is not None else np.nan)
            cols["B"].append(breadth(k) if k is not None else np.nan)
            k2 = _prev_index(tw_dates, d, strict=False)
            cols["U_same"].append(u_all[k2] if k2 is not None else np.nan)
            cols["B_same"].append(breadth(k2) if k2 is not None else np.nan)
    return {k: np.array(v) if k == "dates" else np.array(v, dtype=float) for k, v in cols.items()}


# ── 檢定 ──────────────────────────────────────────────────────────

def compare(dates: np.ndarray, y: np.ndarray, X0: np.ndarray, X1: np.ndarray) -> dict[str, Any]:
    """巢狀模型 X0 ⊂ X1 的擴張視窗樣本外比較：探索期／保留期的 DM 與 MSE 降幅、全期降幅、每年降幅、X1 最後一欄的全期係數與 HAC t。"""
    e0, e1 = oos_errors(X0, y), oos_errors(X1, y)
    od = dates[WARM:]
    out: dict[str, Any] = {"n": int(len(y)), "n_oos": int(len(e0))}
    for name, m in (("explore", od < SPLIT), ("holdout", od >= SPLIT)):
        if m.sum() < 30:
            out[name] = {"n": int(m.sum()), "dm": None, "reduction": None}
            continue
        out[name] = {"n": int(m.sum()), "dm": float(dm_t(e0[m], e1[m])), "reduction": float(1 - (e1[m] ** 2).mean() / (e0[m] ** 2).mean())}
    out["full_reduction"] = float(1 - (e1 ** 2).mean() / (e0 ** 2).mean())
    beta, se = ols_hac(X1, y)
    out["coef"], out["coef_t"] = float(beta[-1]), float(beta[-1] / se[-1])
    r2 = lambda X: float(1 - ((y - X @ np.linalg.lstsq(X, y, rcond=None)[0]) ** 2).sum() / ((y - y.mean()) ** 2).sum())    # noqa: E731
    out["r2"] = {"base": r2(X0), "cand": r2(X1)}
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
                and r["holdout"]["reduction"] >= MIN_REDUCTION and r["coef"] > 0)


def run(tx: dict[str, TxDay], iv: dict[str, dict[str, Any]], twse: dict[str, dict[str, float]]) -> dict[str, Any]:
    b = build(tx, iv, twse)
    base_keys = ("lnR", "x_iv")
    ok = np.isfinite(b["y"])
    for k in base_keys + TESTS:
        ok &= np.isfinite(b[k])
    out: dict[str, Any] = {"split": SPLIT, "first": FIRST, "n_days": int(len(b["dates"])), "n_joint": int(ok.sum()), "z_explore": Z_EXPLORE, "z_holdout": Z_HOLDOUT,
                           "min_reduction": MIN_REDUCTION, "missing": {k: int((~np.isfinite(b[k])).sum()) for k in ("y", "lnR", "x_iv") + TESTS},
                           "tests": {}, "verdict": {}, "describe": {}, "canary": {}}
    if ok.sum() < WARM + 60:
        out["verdict"] = {t: "沒有樣本" for t in TESTS}
        return out
    d, y = b["dates"][ok], b["y"][ok]
    col = lambda k: b[k][ok]                                                  # noqa: E731
    one = np.ones(int(ok.sum()))
    X0 = np.column_stack([one] + [col(k) for k in base_keys])
    out["range"] = {"from": str(d[0]), "to": str(d[-1]), "oos_from": str(d[WARM])}
    for t in TESTS:
        r = compare(d, y, X0, np.column_stack([X0, col(t)]))
        out["tests"][t] = r
        out["verdict"][t] = "過關" if passes(r) else "沒過"
    out["describe"]["all_three"] = compare(d, y, X0, np.column_stack([X0] + [col(t) for t in TESTS]))
    okp = np.isfinite(b["ln_prev"][ok])              # 前一天高低差是 0（整天鎖漲跌停）的隔天沒有 ln(前一天振幅)：描述只用有值的日子
    if okp.sum() >= WARM + 60:
        dp, yp, X0q = d[okp], y[okp], X0[okp]
        X0p = np.column_stack([X0q, col("ln_prev")[okp]])
        out["describe"]["prev_range_in_base"] = {"n": int(okp.sum()), "prev_range": compare(dp, yp, X0q, X0p)}
        for t in TESTS:
            out["describe"]["prev_range_in_base"][t] = compare(dp, yp, X0p, np.column_stack([X0p, col(t)[okp]]))
    corr = {}
    for t in TESTS:
        corr[t] = {k: float(np.corrcoef(col(t)[okp], col(k)[okp])[0, 1]) for k in ("lnR", "x_iv", "ln_prev")}
    out["describe"]["corr"] = corr
    for t in ("U", "B"):                                                      # 金絲雀：當天的（偷看）應該降得更多
        m = np.isfinite(b[f"{t}_same"][ok])
        if m.sum() < WARM + 60:
            out["canary"][t] = None
            continue
        X0m = X0[m]
        prev = compare(d[m], y[m], X0m, np.column_stack([X0m, col(t)[m]]))["full_reduction"]
        same = compare(d[m], y[m], X0m, np.column_stack([X0m, col(f"{t}_same")[m]]))["full_reduction"]
        out["canary"][t] = {"prev_reduction": prev, "same_day_reduction": same, "suspect": bool(prev >= same)}
    return out


# ── 報告 ──────────────────────────────────────────────────────────

def _f(x: Optional[float], spec: str) -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    L = [f"夜盤振幅／成交值／漲跌家數 → 日盤振幅  {r['n_days']} 個日盤、全部欄位都有 {r['n_joint']} 個"
         + (f"（{r['range']['from']}～{r['range']['to']}，樣本外從 {r['range']['oos_from']}）" if r.get("range") else ""),
         f"基準 [1, ln R20, x_iv]；過關：探索 DM ≥ {r['z_explore']:.2f}、保留 DM ≥ {r['z_holdout']:.3f}、兩段 MSE 降幅 ≥ {r['min_reduction']:.0%}、全期係數 > 0",
         "缺值：" + "、".join(f"{k} {v}" for k, v in r["missing"].items()) + "\n"]
    for t, v in r["tests"].items():
        e, h = v["explore"], v["holdout"]
        L.append(f"{t} {TEST_TEXT[t]}：探索 DM={_f(e['dm'], '.2f')} 降幅 {_f(e['reduction'], '.1%')}  保留 DM={_f(h['dm'], '.2f')} 降幅 {_f(h['reduction'], '.1%')}  "
                 f"係數 {v['coef']:.3f}（t {v['coef_t']:.1f}）  R² {v['r2']['base']:.3f}→{v['r2']['cand']:.3f}  → {r['verdict'][t]}")
        L.append("   每年降幅：" + "、".join(f"{y} {w['reduction']:.1%}" for y, w in v["yearly"].items()))
    ds = r.get("describe") or {}
    if ds.get("all_three"):
        a = ds["all_three"]
        L.append(f"\n（描述）三項一起加：探索降幅 {_f(a['explore']['reduction'], '.1%')}、保留降幅 {_f(a['holdout']['reduction'], '.1%')}；R² {a['r2']['base']:.3f}→{a['r2']['cand']:.3f}")
    pr = ds.get("prev_range_in_base")
    if pr:
        p = pr["prev_range"]
        L.append(f"（描述）基準多加前一天日盤振幅：探索降幅 {_f(p['explore']['reduction'], '.1%')}、保留 {_f(p['holdout']['reduction'], '.1%')}；之後各項還剩："
                 + "；".join(f"{t} 探索 {_f(pr[t]['explore']['reduction'], '.1%')}／保留 {_f(pr[t]['holdout']['reduction'], '.1%')}（t {pr[t]['coef_t']:.1f}）" for t in TESTS))
    if ds.get("corr"):
        L.append("（描述）相關：" + "；".join(f"{t} 與 lnR {c['lnR']:.2f}、x_iv {c['x_iv']:.2f}、前一天振幅 {c['ln_prev']:.2f}" for t, c in ds["corr"].items()))
    for t, c in (r.get("canary") or {}).items():
        if c:
            L.append(f"金絲雀 {t}：前一天 {c['prev_reduction']:.1%} vs 當天（偷看）{c['same_day_reduction']:.1%} → {'⚠ 前一天不比偷看差，管線有問題' if c['suspect'] else '正常'}")
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
    db = Path(a[a.index("--intel-db") + 1]) if "--intel-db" in a else DEFAULT_INTEL_DB
    res = run(load_tx(TX_DIR), load_iv(IV_DIR), load_twse(db))
    RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
