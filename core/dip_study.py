"""「跌 1% 就買長天期 Call、漲 1% 就買 Put」的必要條件（update.md 發現 57）：加權指數跌／漲 1% 之後 60、120 個交易日的報酬，是否和平常不一樣。離線分析，不碰交易路徑。

**不看選擇權價格**，只問標的本身。判定標準在跑之前寫進 update.md。資料＝lh-intel 的 taiex.close（1999-02～）。
統計量：H 日後報酬（%）對 [1, 事件日虛擬變數] 的 OLS，係數＝超額報酬（個百分點），Newey-West（落後 H）t 值；跌日期望為正（Call 邏輯）、漲日期望為負（Put 邏輯）。
過關：探索期方向 t ≥ 2.24、保留期方向 t ≥ 1.645、兩段方向超額報酬都 ≥ 3.0（H＝60）／5.0（H＝120）個百分點。
用法：python -m core.dip_study run [--intel-db 路徑]（結果存 data/dip_result.json，只跑一次；--rerun 才重跑）"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from core.intraday_context import ols_hac

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "dip_result.json"
DEFAULT_INTEL_DB = ROOT.parent / "lh-intel" / "data" / "intel.db"

THRESH = 0.01
HORIZONS = (60, 120)
DIRECTIONS = ("dip", "rally")                        # dip＝跌日（Call 邏輯）、rally＝漲日（Put 邏輯）
EXPLORE_END = "2018-12-31"                           # 探索期：前瞻視窗的最後一天 ≤ 這天
HOLDOUT_START = "2019-01-01"                         # 保留期：事件日 ≥ 這天
Z_EXPLORE, Z_HOLDOUT = 2.24, 1.645
MIN_EXCESS = {60: 3.0, 120: 5.0}                     # 方向超額報酬至少幾個百分點
N_TESTS = 4
MIN_EVENTS = 20
GROUP_GAP = 5                                        # 連續事件日相隔 ≤ 這麼多個交易日算同一波
CRISIS_YEARS = (2000, 2001, 2002, 2008, 2020, 2022)


def load_taiex(path: Path | str = DEFAULT_INTEL_DB) -> tuple[list[str], np.ndarray]:
    """唯讀取出加權指數日收盤（每個日期取最新一版、略過空值）。這是研究讀取：歷史收盤是事實，不套 as_of。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT o.event_date, o.value FROM observations o JOIN (SELECT event_date AS d, max(revision) AS r FROM observations WHERE source='twse' AND series='taiex.close' "
                            "AND event_date IS NOT NULL GROUP BY event_date) m ON o.event_date=m.d AND o.revision=m.r WHERE o.source='twse' AND o.series='taiex.close' "
                            "AND o.value IS NOT NULL ORDER BY o.event_date").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows], np.array([float(r[1]) for r in rows])


def daily_returns(close: np.ndarray) -> np.ndarray:
    r = np.full(len(close), np.nan)
    r[1:] = close[1:] / close[:-1] - 1
    return r


def forward_returns(close: np.ndarray, h: int) -> np.ndarray:
    """從第 t 天收盤起 h 個交易日後的簡單報酬（%）；後面不夠 h 天是 NaN。"""
    out = np.full(len(close), np.nan)
    if len(close) > h:
        out[:-h] = (close[h:] / close[:-h] - 1) * 100.0
    return out


def forward_drawdown(close: np.ndarray, h: int) -> np.ndarray:
    """進場後 h 個交易日內（不含進場日）的最低收盤相對進場收盤（%）；不夠 h 天是 NaN。"""
    out = np.full(len(close), np.nan)
    for t in range(len(close) - h):
        out[t] = (close[t + 1:t + h + 1].min() / close[t] - 1) * 100.0
    return out


def event_mask(r: np.ndarray, direction: str, thresh: float = THRESH) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return (r <= -thresh) if direction == "dip" else (r >= thresh)


def period_masks(dates: Sequence[str], h: int, fwd: np.ndarray) -> dict[str, np.ndarray]:
    """探索期：前瞻視窗最後一天 ≤ EXPLORE_END；保留期：事件日 ≥ HOLDOUT_START；兩者都要有完整的前瞻視窗。"""
    n = len(dates)
    end_date = np.array([dates[t + h] if t + h < n else "9999-12-31" for t in range(n)])
    d = np.array(dates)
    ok = np.isfinite(fwd)
    return {"explore": ok & (end_date <= EXPLORE_END), "holdout": ok & (d >= HOLDOUT_START)}


def dummy_excess(y: np.ndarray, d: np.ndarray, lag: int) -> Optional[tuple[float, float]]:
    """y 對 [1, d] 的 OLS：(d 的係數＝事件日比其他日子多的平均前瞻報酬, Newey-West t)。事件或對照太少回 None。"""
    if d.sum() < MIN_EVENTS or (~d).sum() < MIN_EVENTS:
        return None
    X = np.column_stack([np.ones(len(y)), d.astype(float)])
    beta, se = ols_hac(X, y, lag)
    return float(beta[1]), float(beta[1] / se[1])


def one_test(dates: Sequence[str], close: np.ndarray, direction: str, h: int) -> dict[str, Any]:
    r, fwd = daily_returns(close), forward_returns(close, h)
    ev = event_mask(r, direction)
    sign = 1.0 if direction == "dip" else -1.0
    masks = period_masks(dates, h, fwd)
    out: dict[str, Any] = {}
    for name, m in masks.items():
        res = dummy_excess(fwd[m], ev[m], h)
        out[name] = {"n_days": int(m.sum()), "n_events": int(ev[m].sum()), "excess": None, "t": None, "signed_excess": None, "signed_t": None} if res is None else \
            {"n_days": int(m.sum()), "n_events": int(ev[m].sum()), "excess": res[0], "t": res[1], "signed_excess": sign * res[0], "signed_t": sign * res[1]}
    return out


def passes(r: dict[str, Any], h: int) -> bool:
    e, ho = r["explore"], r["holdout"]
    if e["signed_t"] is None or ho["signed_t"] is None:
        return False
    return bool(e["signed_t"] >= Z_EXPLORE and ho["signed_t"] >= Z_HOLDOUT and e["signed_excess"] >= MIN_EXCESS[h] and ho["signed_excess"] >= MIN_EXCESS[h])


def episodes(idx: np.ndarray, gap: int = GROUP_GAP) -> int:
    """事件日序號 → 波段數（相隔 ≤ gap 個交易日算同一波）。"""
    if len(idx) == 0:
        return 0
    return int(1 + (np.diff(idx) > gap).sum())


def describe(dates: Sequence[str], close: np.ndarray, direction: str, h: int) -> dict[str, Any]:
    r, fwd, dd = daily_returns(close), forward_returns(close, h), forward_drawdown(close, h)
    ev = event_mask(r, direction)
    ok = np.isfinite(fwd)
    yrs = np.array([int(d[:4]) for d in dates])
    q = lambda x: {f"P{p}": float(np.percentile(x, p)) for p in (10, 50, 90)}   # noqa: E731
    e, o = ev & ok, ~ev & ok
    out: dict[str, Any] = {"n_events": int(e.sum()), "episodes": episodes(np.flatnonzero(ev)), "events_fwd": q(fwd[e]), "other_fwd": q(fwd[o]),
                           "events_positive": float((fwd[e] > 0).mean()), "other_positive": float((fwd[o] > 0).mean()),
                           "events_drawdown": q(dd[e]), "events_drawdown_mean": float(dd[e].mean()), "other_drawdown_mean": float(dd[o].mean())}
    full = dummy_excess(fwd[ok], ev[ok], h)
    out["full_excess"], out["full_t"] = (None, None) if full is None else full
    keep = ok & ~np.isin(yrs, CRISIS_YEARS)
    ex = dummy_excess(fwd[keep], ev[keep], h)
    out["ex_crisis_excess"], out["ex_crisis_t"] = (None, None) if ex is None else ex
    out["by_year"] = {int(y): {"n": int((e & (yrs == y)).sum()), "mean_fwd": float(fwd[e & (yrs == y)].mean())} for y in sorted(set(yrs[e])) if (e & (yrs == y)).sum() >= 3}
    return out


def run(dates: Sequence[str], close: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {"n_days": len(dates), "first": dates[0], "last": dates[-1], "thresh": THRESH, "explore_end": EXPLORE_END, "holdout_start": HOLDOUT_START,
                           "z_explore": Z_EXPLORE, "z_holdout": Z_HOLDOUT, "min_excess": {str(k): v for k, v in MIN_EXCESS.items()}, "tests": {}, "descriptive": {}, "verdict": {}}
    any_pass = False
    for direction in DIRECTIONS:
        for h in HORIZONS:
            key = f"{direction}/{h}"
            t = one_test(dates, close, direction, h)
            t["pass"] = passes(t, h)
            out["tests"][key] = t
            out["descriptive"][key] = describe(dates, close, direction, h)
            out["verdict"][key] = "過關" if t["pass"] else "沒過"
            any_pass = any_pass or t["pass"]
    out["necessary_condition_holds"] = bool(any_pass)
    return out


def format_report(r: dict[str, Any]) -> str:
    f = lambda x, spec: "—" if x is None else format(x, spec)   # noqa: E731
    L = [f"加權指數跌／漲 {r['thresh']:.0%} 之後的前瞻報酬  {r['first']}～{r['last']}（{r['n_days']} 個交易日；探索期＝前瞻視窗結束 ≤ {r['explore_end']}，保留期＝事件日 ≥ {r['holdout_start']}）",
         f"過關：探索方向 t ≥ {r['z_explore']}、保留方向 t ≥ {r['z_holdout']}、兩段方向超額報酬 ≥ {r['min_excess']['60']}（60 日）／{r['min_excess']['120']}（120 日）個百分點\n"]
    L.append(f"{'檢定':<10}{'探索 事件':>8}{'超額pp':>8}{'方向t':>7}   {'保留 事件':>8}{'超額pp':>8}{'方向t':>7}  結論")
    for key, t in r["tests"].items():
        e, h = t["explore"], t["holdout"]
        L.append(f"{key:<10}{e['n_events']:>8}{f(e['signed_excess'], '.2f'):>8}{f(e['signed_t'], '.2f'):>7}   {h['n_events']:>8}{f(h['signed_excess'], '.2f'):>8}{f(h['signed_t'], '.2f'):>7}  {r['verdict'][key]}")
    L.append("\n描述（全期；前瞻報酬 %）：")
    for key, d in r["descriptive"].items():
        L.append(f"  {key}: 事件 {d['n_events']} 日（{d['episodes']} 波）  事件 P10/P50/P90 {d['events_fwd']['P10']:.1f}/{d['events_fwd']['P50']:.1f}/{d['events_fwd']['P90']:.1f}  "
                 f"平常 {d['other_fwd']['P10']:.1f}/{d['other_fwd']['P50']:.1f}/{d['other_fwd']['P90']:.1f}  正報酬比例 {d['events_positive']:.0%} vs {d['other_positive']:.0%}  "
                 f"進場後最大回檔平均 {d['events_drawdown_mean']:.1f}% vs {d['other_drawdown_mean']:.1f}%  全期超額 {f(d['full_excess'], '.2f')}（t {f(d['full_t'], '.1f')}）  拿掉危機年 {f(d['ex_crisis_excess'], '.2f')}（t {f(d['ex_crisis_t'], '.1f')}）")
        if key.endswith("/120"):
            L.append("     每年（事件≥3）平均前瞻報酬：" + "、".join(f"{y}:{v['mean_fwd']:.0f}(n{v['n']})" for y, v in d["by_year"].items()))
    L.append(f"\n結論：{'必要條件成立（至少一個過關）→ 可以討論下一步（遠月選擇權價格）' if r['necessary_condition_holds'] else '四個都沒過關 → 停：標的本身在跌／漲 1% 之後沒有可用的前瞻報酬差異，不必再往選擇權價格走'}")
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
    db = Path(a[a.index("--intel-db") + 1]) if "--intel-db" in a else DEFAULT_INTEL_DB
    dates, close = load_taiex(db)
    res = run(dates, close)
    RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
