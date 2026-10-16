"""「成交量放大＋突然漲跌 → 順勢進場」的歷史檢定（update.md 發現 43）。離線分析，不碰交易路徑。

判定標準在跑之前就寫進 update.md 了（規則定義見 core/signal_rules.py）：9 個檢定（門檻 {30,40,50} 點 × 持有 {5,15,30} 分鐘），
單尾、標準誤依交易日分群、Bonferroni m＝9 → t ≥ 2.54；過關還要事件數 ≥ 100、前後兩半的平均淨報酬都 > 0。**只跑一次**：
結果存在 data/signal_study_result.json，再跑會直接印出已存的結果（要重跑必須明確加 --rerun，那就違反登記，請在 update.md 說明原因）。
資料：bars_1m（TXF 日盤 1 分 K，含成交量）；訊號只在日盤；出場超過收盤的不算那個持有期。
用法：python -m core.signal_study run [--end YYYY-MM-DD]

發現 44（同一個訊號加上停損停利）：python -m core.signal_study bracket [--end YYYY-MM-DD]——同一批事件，出場改成「停利價碰到／停損價碰到／持有滿 30 分鐘」最先發生的，
TP {30,50,80} × SL {20,30,50} 點 × 3 個門檻 ＝ 27 個檢定，Bonferroni m＝27 → t ≥ 2.90；結果存 data/signal_bracket_result.json，同樣只跑一次。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from core import signal_rules as sr

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "signal_study_result.json"
N_MIN = 300                        # 日盤 08:45~13:45 的分鐘數
T_CRIT = 2.54                      # 單尾 α＝0.05、Bonferroni m＝9 → 0.0056 → z＝2.54
MIN_EVENTS = 100
BRACKET_RESULT_PATH = ROOT / "data" / "signal_bracket_result.json"
T_CRIT_BRACKET = 2.90                # 單尾 α＝0.05、Bonferroni m＝27 → 0.00185 → z＝2.90
OPEN_MIN = 8 * 60 + 45
MIN_BARS = 250                     # 一天至少這麼多根才算完整的日盤


class Panel:
    """各日盤的 1 分 K 陣列（天數 × 300），沒有的分鐘是 NaN。"""

    def __init__(self, dates: list[str], o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, v: np.ndarray) -> None:
        self.dates, self.o, self.h, self.l, self.c, self.v = dates, o, h, lo, c, v


def build_panel(rows: Sequence[dict[str, Any]], start: Optional[str] = None, end: Optional[str] = None) -> Panel:
    """bars_1m 的列（date、hhmm＝K 棒結束時間、open/high/low/close/volume）→ Panel。hhmm 08:46 結束的那根是第 0 分鐘。end 是「不含」的日期上限。"""
    by: dict[str, dict[int, tuple]] = {}
    for r in rows:
        d = str(r["date"])
        if (start and d < start) or (end and d >= end):
            continue
        try:
            hhmm = int(r["hhmm"])
            i = (hhmm // 100) * 60 + hhmm % 100 - OPEN_MIN - 1
            vals = (r["open"], r["high"], r["low"], r["close"], r["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < N_MIN and all(x is not None for x in vals[:4]) and vals[3] > 0:
            by.setdefault(d, {})[i] = vals
    dates = sorted(d for d, b in by.items() if len(b) >= MIN_BARS)
    shape = (len(dates), N_MIN)
    o, h, lo, c, v = (np.full(shape, np.nan) for _ in range(5))
    for k, d in enumerate(dates):
        for i, (a, b, l_, c_, vol) in by[d].items():
            o[k, i], h[k, i], lo[k, i], c[k, i] = a, b, l_, c_
            v[k, i] = vol if vol is not None else np.nan
    return Panel(dates, o, h, lo, c, v)


def ratios(p: Panel) -> np.ndarray:
    w = np.vstack([sr.rolling_sum(p.v[d]) for d in range(len(p.dates))]) if p.dates else np.empty((0, N_MIN))
    return sr.ratio_of(w, sr.baseline(w))


def find_events(p: Panel, ratio: np.ndarray, thr_pct: float) -> list[tuple[int, sr.Event]]:
    return [(d, e) for d in range(len(p.dates)) for e in sr.detect(p.c[d], ratio[d], thr_pct)]


def outcomes(p: Panel, events: Sequence[tuple[int, sr.Event]], horizon: int, cost: float) -> list[tuple[int, sr.Outcome]]:
    out = []
    for d, e in events:
        m = e.m
        if m + horizon > N_MIN - 1:                    # 出場要在收盤之前
            continue
        sl = slice(m + 1, m + 1 + horizon)
        oc = sr.outcome(p.o[d, m + 1], p.h[d, sl], p.l[d, sl], p.c[d, sl], e.direction, horizon, cost)
        if oc is not None:
            out.append((d, oc))
    return out


def cluster_t(x: np.ndarray, g: np.ndarray) -> tuple[float, float, int]:
    """平均數與依分群（交易日）的穩健 t：se ＝ sqrt(Σ_g (Σ_{i∈g}(x_i − μ))²) / n。回傳 (平均, t, 群數)。"""
    n = len(x)
    if n == 0:
        return float("nan"), float("nan"), 0
    mu = float(x.mean())
    groups = np.unique(g)
    s2 = sum(float((x[g == k] - mu).sum()) ** 2 for k in groups)
    se = (s2 ** 0.5) / n
    return mu, (mu / se if se > 0 else float("nan")), len(groups)


def stats(res: Sequence[tuple[int, sr.Outcome]], p: Panel) -> dict[str, Any]:
    n = len(res)
    if n == 0:
        return {"n": 0, "pass": False}
    g = np.array([d for d, _ in res])
    net = np.array([o.net for _, o in res])
    gross = np.array([o.gross for _, o in res])
    mu, t, ng = cluster_t(net, g)
    med = float(np.median(g))
    first, second = net[g <= med], net[g > med]
    h1 = float(first.mean()) if len(first) else float("nan")
    h2 = float(second.mean()) if len(second) else float("nan")
    ok = bool(n >= MIN_EVENTS and t >= T_CRIT and h1 > 0 and h2 > 0)
    return {"n": n, "days": ng, "mean_net": mu, "mean_net_pts": mu * sr.REF_PRICE, "t": t, "mean_gross": float(gross.mean()),
            "mean_gross_pts": float(gross.mean()) * sr.REF_PRICE, "win": float((net > 0).mean()), "half1": h1, "half2": h2,
            "mfe_med_pts": float(np.median([o.mfe for _, o in res])) * sr.REF_PRICE, "mae_med_pts": float(np.median([o.mae for _, o in res])) * sr.REF_PRICE,
            "pass": ok}


def control(p: Panel, events: Sequence[tuple[int, sr.Event]], horizon: int, cost: float, seed: int) -> dict[str, float]:
    """對照組：同樣數量、同樣時段分布（同一個 m）、隨機的交易日與隨機方向。淨報酬應該接近 −成本。"""
    rng = np.random.default_rng(seed)
    nets = []
    D = len(p.dates)
    for _, e in events:
        m = e.m
        if m + horizon > N_MIN - 1 or D == 0:
            continue
        for _ in range(5):                               # 抽到沒資料的日子就再抽
            d = int(rng.integers(D))
            sl = slice(m + 1, m + 1 + horizon)
            oc = sr.outcome(p.o[d, m + 1], p.h[d, sl], p.l[d, sl], p.c[d, sl], 1 if rng.random() < 0.5 else -1, horizon, cost)
            if oc is not None:
                nets.append(oc.net)
                break
    return {"n": len(nets), "mean_net_pts": (float(np.mean(nets)) * sr.REF_PRICE) if nets else float("nan")}


def run(rows: Sequence[dict[str, Any]], start: Optional[str] = None, end: Optional[str] = None, seed: int = 7) -> dict[str, Any]:
    p = build_panel(rows, start, end)
    ratio = ratios(p)
    cost = sr.cost_pct()
    res: dict[str, Any] = {"version": 1, "dates": [p.dates[0], p.dates[-1]] if p.dates else [], "n_days": len(p.dates), "cost_pts": sr.cost_pts(),
                           "ref_price": sr.REF_PRICE, "t_crit": T_CRIT, "min_events": MIN_EVENTS, "tests": {}, "per_year": {}, "passes": []}
    for pts in sr.POINTS:
        ev = find_events(p, ratio, sr.threshold_pct(pts))
        years: dict[str, int] = {}
        for d, _ in ev:
            years[p.dates[d][:4]] = years.get(p.dates[d][:4], 0) + 1
        res["per_year"][str(pts)] = years
        res["events"] = res.get("events", {})
        res["events"][str(pts)] = {"n": len(ev), "per_day": len(ev) / max(len(p.dates), 1)}
        for h in sr.HORIZONS:
            key = f"{pts}/{h}"
            st = stats(outcomes(p, ev, h, cost), p)
            st["control"] = control(p, ev, h, cost, seed)
            res["tests"][key] = st
            if st.get("pass"):
                res["passes"].append(key)
    res["verdict"] = "有線索（至少一個檢定過關，仍需前向驗證）" if res["passes"] else "沒有任何檢定過關"
    return res


def format_report(r: dict[str, Any]) -> str:
    L = [f"順勢爆量訊號歷史檢定：{r['dates'][0]} ～ {r['dates'][1]}，{r['n_days']} 個日盤；成本 {r['cost_pts']:g} 點（換算 @{r['ref_price']:,.0f}）",
         f"判定（登記於跑之前）：t ≥ {r['t_crit']}、事件數 ≥ {r['min_events']}、前後兩半的平均淨報酬都 > 0；9 個檢定，單尾，依交易日分群", ""]
    for pts in sr.POINTS:
        e = r["events"][str(pts)]
        yrs = "、".join(f"{y}:{n}" for y, n in sorted(r["per_year"][str(pts)].items()))
        L.append(f"門檻 {pts} 點：事件 {e['n']} 個（平均每個日盤 {e['per_day']:.2f} 個）；每年 {yrs}")
        for h in sr.HORIZONS:
            s = r["tests"][f"{pts}/{h}"]
            if not s.get("n"):
                L.append(f"   持有 {h:>2} 分：沒有事件")
                continue
            L.append(f"   持有 {h:>2} 分：n={s['n']:4d}  淨 {s['mean_net_pts']:+6.2f} 點  t={s['t']:+5.2f}  毛 {s['mean_gross_pts']:+6.2f} 點  勝率 {s['win']:.0%}  "
                     f"前半 {s['half1'] * sr.REF_PRICE:+6.2f}／後半 {s['half2'] * sr.REF_PRICE:+6.2f}  MFE {s['mfe_med_pts']:.0f}／MAE {s['mae_med_pts']:.0f}  "
                     f"對照 {s['control']['mean_net_pts']:+6.2f}  {'✅ 過關' if s['pass'] else '—'}")
    L += ["", f"結論：{r['verdict']}" + (f"（{', '.join(r['passes'])}）" if r["passes"] else "")]
    return "\n".join(L)


# ── 發現 44：停損停利 ────────────────────────────────────────

def bracket_outcomes(p: Panel, events: Sequence[tuple[int, sr.Event]], tp_pct: float, sl_pct: float, cost: float,
                     flip: Optional[np.random.Generator] = None) -> list[tuple[int, sr.Bracket]]:
    """每個事件：進場＝下一分鐘開盤，之後逐分鐘看停損、停利、滿 MAX_HOLD 分鐘。flip 給了就隨機決定方向（對照組：沒有資訊的版本）。"""
    out = []
    H = sr.MAX_HOLD
    for d, e in events:
        m = e.m
        if m + H > N_MIN - 1:
            continue
        sl = slice(m + 1, m + 1 + H)
        direction = e.direction if flip is None else (1 if flip.random() < 0.5 else -1)
        b = sr.bracket(p.o[d, m + 1], direction, p.o[d, sl], p.h[d, sl], p.l[d, sl], p.c[d, sl], tp_pct, sl_pct, H, cost)
        if b is not None:
            out.append((d, b))
    return out


def bracket_stats(res: Sequence[tuple[int, sr.Bracket]]) -> dict[str, Any]:
    n = len(res)
    if n == 0:
        return {"n": 0, "pass": False}
    g = np.array([d for d, _ in res])
    net = np.array([b.net for _, b in res])
    mu, t, ng = cluster_t(net, g)
    med = float(np.median(g))
    first, second = net[g <= med], net[g > med]
    h1 = float(first.mean()) if len(first) else float("nan")
    h2 = float(second.mean()) if len(second) else float("nan")
    reasons = [b.reason for _, b in res]
    return {"n": n, "days": ng, "mean_net": mu, "mean_net_pts": mu * sr.REF_PRICE, "t": t, "mean_gross_pts": float(np.mean([b.gross for _, b in res])) * sr.REF_PRICE,
            "win": float((net > 0).mean()), "half1": h1, "half2": h2, "share_tp": reasons.count("tp") / n, "share_sl": reasons.count("sl") / n, "share_time": reasons.count("time") / n,
            "pass": bool(n >= MIN_EVENTS and t >= T_CRIT_BRACKET and h1 > 0 and h2 > 0)}


def run_brackets(rows: Sequence[dict[str, Any]], start: Optional[str] = None, end: Optional[str] = None, seed: int = 7) -> dict[str, Any]:
    p = build_panel(rows, start, end)
    ratio = ratios(p)
    cost = sr.cost_pct()
    res: dict[str, Any] = {"version": 1, "dates": [p.dates[0], p.dates[-1]] if p.dates else [], "n_days": len(p.dates), "cost_pts": sr.cost_pts(), "ref_price": sr.REF_PRICE,
                           "t_crit": T_CRIT_BRACKET, "min_events": MIN_EVENTS, "max_hold": sr.MAX_HOLD, "tests": {}, "passes": []}
    for pts in sr.POINTS:
        ev = find_events(p, ratio, sr.threshold_pct(pts))
        for tp in sr.TP_GRID:
            for sl_ in sr.SL_GRID:
                key = f"{pts}/{tp}/{sl_}"
                st = bracket_stats(bracket_outcomes(p, ev, sr.threshold_pct(tp), sr.threshold_pct(sl_), cost))
                ctrl = bracket_outcomes(p, ev, sr.threshold_pct(tp), sr.threshold_pct(sl_), cost, flip=np.random.default_rng(seed))
                st["control_net_pts"] = float(np.mean([b.net for _, b in ctrl])) * sr.REF_PRICE if ctrl else float("nan")
                res["tests"][key] = st
                if st.get("pass"):
                    res["passes"].append(key)
    res["verdict"] = "有線索（至少一個檢定過關，仍需前向驗證）" if res["passes"] else "沒有任何檢定過關"
    return res


def format_bracket_report(r: dict[str, Any]) -> str:
    L = [f"順勢爆量訊號加停損停利：{r['dates'][0]} ～ {r['dates'][1]}，{r['n_days']} 個日盤；成本 {r['cost_pts']:g} 點；最長持有 {r['max_hold']} 分鐘",
         f"判定（登記於跑之前）：27 個檢定，t ≥ {r['t_crit']}、事件數 ≥ {r['min_events']}、前後兩半的平均淨報酬都 > 0；碰價用 1 分 K 高低點，同一分鐘兩邊都碰到算停損先到", ""]
    for pts in sr.POINTS:
        L.append(f"門檻 {pts} 點（TP 停利／SL 停損，單位：點）")
        for tp in sr.TP_GRID:
            for sl_ in sr.SL_GRID:
                s = r["tests"][f"{pts}/{tp}/{sl_}"]
                if not s.get("n"):
                    L.append(f"   TP{tp:>2}/SL{sl_:>2}：沒有事件")
                    continue
                L.append(f"   TP{tp:>2}/SL{sl_:>2}：n={s['n']:4d}  淨 {s['mean_net_pts']:+6.2f} 點  t={s['t']:+5.2f}  毛 {s['mean_gross_pts']:+6.2f}  勝率 {s['win']:.0%}  "
                         f"停利 {s['share_tp']:.0%}／停損 {s['share_sl']:.0%}／時間 {s['share_time']:.0%}  對照(隨機方向) {s['control_net_pts']:+6.2f}  {'✅ 過關' if s['pass'] else '—'}")
    L += ["", f"結論：{r['verdict']}" + (f"（{', '.join(r['passes'])}）" if r["passes"] else "")]
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] not in ("run", "bracket"):
        print(__doc__)
        return 2
    end = a[a.index("--end") + 1] if "--end" in a else None
    if a[0] == "bracket":
        if BRACKET_RESULT_PATH.exists() and "--rerun" not in a:
            print(f"（已經跑過，登記的規則只跑一次；以下是 {BRACKET_RESULT_PATH} 存的結果。要重跑請加 --rerun 並在 update.md 說明原因）\n")
            print(format_bracket_report(json.loads(BRACKET_RESULT_PATH.read_text(encoding="utf-8"))))
            return 0
        from core.market_store import MarketStore
        res = run_brackets(MarketStore().bars_1m("TXF"), end=end)
        BRACKET_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        BRACKET_RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(format_bracket_report(res))
        return 0
    if RESULT_PATH.exists() and "--rerun" not in a:
        print(f"（已經跑過，登記的規則只跑一次；以下是 {RESULT_PATH} 存的結果。要重跑請加 --rerun 並在 update.md 說明原因）\n")
        print(format_report(json.loads(RESULT_PATH.read_text(encoding="utf-8"))))
        return 0
    from core.market_store import MarketStore
    res = run(MarketStore().bars_1m("TXF"), end=end)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
