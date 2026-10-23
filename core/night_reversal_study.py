"""日盤收黑之後，夜盤的順風會不會更大（update.md 發現 73）。

發現 72：漲幅幾乎都在過夜（夜盤平均每晚 +25 點），但天天留倉風險太大。這裡檢驗順風是不是集中在「日盤收黑」（收盤 < 開盤）之後。
判定標準在跑之前寫在 update.md 發現 73；只跑一次（結果存 data/night_reversal_result.json；--rerun 才重跑）。
純離線研究：策略／風控／下單模組不得引用（tests/test_night_reversal_study.py 靜態檢查）。
資料與報酬的定義同 core/drift_timing_study（TX 日行情、同一個合約）。
用法：python -m core.night_reversal_study run [--rerun]"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional

import numpy as np

from core.drift_timing_study import COST, PTS, TPE, TX_DIR, Day, build, load_tx
from core.weekly_straddle_study import slope_test

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "night_reversal_result.json"
NW_LAG = 10
N_TESTS = 2
Z = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)          # ≈ 1.96：單尾、Bonferroni
_WD = "一二三四五六日"


def pairs(days: dict[str, Day]) -> dict[str, np.ndarray]:
    """第 t 個交易日的訊號（日盤收黑 D_t < 0、收跌 R_t < 0）對上下一個交易日的過夜 O、夜盤 N、兩段跳空。
    t 與 t+1 要在交易日曆上相鄰（中間有被略過的日子就不算）。"""
    b = build(days)
    pos = {d: i for i, d in enumerate(sorted(days))}
    keep = [i for i in range(len(b["O"]) - 1) if pos[b["date"][i + 1]] == pos[b["date"][i]] + 1]
    k, k1 = np.array(keep, dtype=int), np.array(keep, dtype=int) + 1
    return {"date": b["date"][k], "D": b["D"][k], "R": b["R"][k], "wd": b["wd"][k],
            "O": b["O"][k1], "N": b["night"][k1], "g1": b["g1"][k1], "g2": b["g2"][k1]}


def _diff(y: np.ndarray, x: np.ndarray) -> Optional[float]:
    return float(y[x].mean() - y[~x].mean()) if x.any() and (~x).any() else None


def check(y: np.ndarray, down: np.ndarray) -> dict[str, Any]:
    """y ~ 1 ＋ 收黑：係數＝收黑之後的平均 − 收紅之後的平均；NW 的 t；前後兩半各自的係數。"""
    ok = ~np.isnan(y)
    y, down = y[ok], down[ok]
    out = slope_test(y, down.astype(float), NW_LAG)
    h = len(y) // 2
    out["first"] = _diff(y[:h], down[:h]) if h >= 3 else None
    out["second"] = _diff(y[h:], down[h:]) if len(y) - h >= 3 else None
    out["pass"] = bool(out["t"] is not None and out["t"] >= Z and (out["first"] or 0) > 0 and (out["second"] or 0) > 0)
    return out


def _grp(p: dict[str, np.ndarray], sel: np.ndarray) -> dict[str, Any]:
    def m(k: str) -> Optional[float]:
        v = p[k][sel]
        v = v[~np.isnan(v)]
        return float(v.mean() * PTS) if len(v) else None

    def win(k: str) -> Optional[float]:
        v = p[k][sel]
        v = v[~np.isnan(v)]
        return float(np.mean(v > 0)) if len(v) else None
    return {"n": int(sel.sum()), "O": m("O"), "N": m("N"), "g1": m("g1"), "g2": m("g2"), "O_win": win("O"), "N_win": win("N")}


def trading(p: dict[str, np.ndarray], sel: np.ndarray) -> dict[str, Any]:
    """只在 sel 的日子收盤留倉做多、隔天開盤出場（扣 4 點）；其他日子空手。"""
    day = np.where(sel, p["O"] - COST, 0.0) * PTS
    eq = np.cumsum(day)
    dd = eq - np.maximum.accumulate(eq) if len(eq) else np.array([0.0])
    tr = day[sel]
    return {"trades": int(sel.sum()), "per_trade": float(tr.mean()) if len(tr) else None, "per_day": float(day.mean()) if len(day) else None,
            "sharpe": float(day.mean() / day.std() * np.sqrt(250)) if len(day) and day.std() > 0 else None,
            "worst": float(day.min()) if len(day) else None, "max_dd": float(dd.min()), "win": float(np.mean(tr > 0)) if len(tr) else None}


def describe(p: dict[str, np.ndarray]) -> dict[str, Any]:
    down, fall = p["D"] < 0, p["R"] < 0
    out: dict[str, Any] = {"down": _grp(p, down), "up": _grp(p, ~down), "fall": _grp(p, fall), "rise": _grp(p, ~fall)}
    out["fall_diff"] = slope_test(p["O"], fall.astype(float), NW_LAG)
    s = slope_test(p["O"], p["D"], NW_LAG)
    out["slope"] = {"b": s["b"], "t": s["t"]}
    edges = np.quantile(p["D"], [0.2, 0.4, 0.6, 0.8])
    q = np.searchsorted(edges, p["D"], side="right")
    out["quintiles"] = [{"n": int((q == i).sum()), "D": float(p["D"][q == i].mean() * PTS), "O": float(p["O"][q == i].mean() * PTS),
                         "N": float(np.nanmean(p["N"][q == i]) * PTS) if (~np.isnan(p["N"][q == i])).any() else None}
                        if (q == i).any() else {"n": 0, "D": None, "O": None, "N": None} for i in range(5)]
    out["trade_down"], out["trade_all"] = trading(p, down), trading(p, np.ones(len(down), dtype=bool))
    yrs = np.array([d[:4] for d in p["date"]])
    out["by_year"] = {y: {"n": int((yrs == y).sum()), "diff": None if (d := _diff(p["O"][yrs == y], down[yrs == y])) is None else d * PTS}
                      for y in sorted(set(yrs))}
    out["weekday"] = {_WD[w]: {"n": int((p["wd"] == w).sum()), "diff": None if (d := _diff(p["O"][p["wd"] == w], down[p["wd"] == w])) is None else d * PTS}
                      for w in range(5) if (p["wd"] == w).any()}
    return out


def run(days: dict[str, Day]) -> dict[str, Any]:
    p = pairs(days)
    if len(p["O"]) < 30:
        return {"n": len(p["O"]), "verdict": {"T1": "沒有樣本", "T2": "沒有樣本"}, "describe": {}}
    down = p["D"] < 0
    r1, r2 = check(p["O"], down), check(p["N"], down)
    nn = ~np.isnan(p["N"])
    return {"n": len(p["O"]), "first": p["date"][0], "last": p["date"][-1], "n_night": int(nn.sum()),
            "night_first": p["date"][nn][0] if nn.any() else None, "z": Z, "T1": r1, "T2": r2, "describe": describe(p),
            "verdict": {"T1": "過關" if r1["pass"] else "沒過", "T2": "過關" if r2["pass"] else "沒過"}}


# ── 報告 ──────────────────────────────────────────────────────────

def _f(x: Optional[float], spec: str = "+.1f") -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    if not r.get("describe"):
        return f"樣本不夠（{r['n']} 天）：{r['verdict']}"
    d = r["describe"]
    p = lambda x: _f(None if x is None else x * PTS)                                # noqa: E731
    dn, up = d["down"], d["up"]
    L = [f"日盤收黑之後的過夜：TX {r['n']} 天（{r['first']}～{r['last']}）；夜盤 {r['n_night']} 天（{r['night_first']} 起）；點數＝比例 × {PTS:,.0f}",
         f"T1 過夜（收黑 − 收紅）：{p(r['T1']['b'])} 點、t＝{_f(r['T1']['t'], '+.2f')}（要 ≥ {r['z']:.2f}）、前半 {p(r['T1']['first'])}／後半 {p(r['T1']['second'])} → {r['verdict']['T1']}",
         f"T2 夜盤（收黑 − 收紅）：{p(r['T2']['b'])} 點、t＝{_f(r['T2']['t'], '+.2f')}、前半 {p(r['T2']['first'])}／後半 {p(r['T2']['second'])} → {r['verdict']['T2']}",
         f"收黑之後 {dn['n']} 天：過夜 {_f(dn['O'])}（勝率 {_f(dn['O_win'], '.0%')}）、夜盤 {_f(dn['N'])}（{_f(dn['N_win'], '.0%')}）、收盤→夜盤開盤 {_f(dn['g1'])}、夜盤收盤→日盤開盤 {_f(dn['g2'])} 點",
         f"收紅之後 {up['n']} 天：過夜 {_f(up['O'])}（勝率 {_f(up['O_win'], '.0%')}）、夜盤 {_f(up['N'])}（{_f(up['N_win'], '.0%')}）、收盤→夜盤開盤 {_f(up['g1'])}、夜盤收盤→日盤開盤 {_f(up['g2'])} 點",
         f"改用收跌（收盤 < 前一天收盤）：之後過夜 {_f(d['fall']['O'])} vs 收漲之後 {_f(d['rise']['O'])} 點（差的 t {_f(d['fall_diff']['t'], '+.2f')}）",
         f"連續：過夜對日盤報酬的斜率 {_f(d['slope']['b'], '+.3f')}（t {_f(d['slope']['t'], '+.2f')}）；日盤五分位（跌最多 → 漲最多）之後的過夜："
         + "、".join(f"{_f(q['O'])}" for q in d["quintiles"]) + " 點"]
    for k, name in (("trade_down", "只在收黑後留倉"), ("trade_all", "每天留倉")):
        t = d[k]
        L.append(f"{name}（扣 4 點）：{t['trades']} 筆、每筆 {_f(t['per_trade'])}、每天 {_f(t['per_day'])} 點、夏普 {_f(t['sharpe'], '.2f')}、勝率 {_f(t['win'], '.0%')}、"
                 f"最差一天 {_f(t['worst'], '+,.0f')}、最大回落 {_f(t['max_dd'], '+,.0f')} 點")
    L.append("每年（收黑 − 收紅的過夜）：" + "；".join(f"{y} {_f(v['diff'], '+.0f')}" for y, v in d["by_year"].items()))
    L.append("星期（訊號那天）：" + "；".join(f"{k} {_f(v['diff'], '+.0f')}" for k, v in d["weekday"].items()))
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "run":
        print(__doc__)
        return 2
    if RESULT_PATH.exists() and "--rerun" not in a:
        print("已經跑過一次（判定標準只跑一次）；結果：\n" + format_report(json.loads(RESULT_PATH.read_text(encoding="utf-8"))))
        return 0
    r = run(load_tx(TX_DIR))
    r["generated_at"] = datetime.now(TPE).isoformat(timespec="seconds")
    RESULT_PATH.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(format_report(r))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
