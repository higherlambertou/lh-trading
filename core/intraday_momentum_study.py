"""日盤最後 30 分鐘會不會跟著早上的方向走（日內動能，update.md 發現 75；Gao, Han, Li & Zhou 2018）。

r_first＝當天 09:15 收盤 ÷ 前一個交易日 13:45 收盤（含夜盤與開盤跳空）；r_last＝13:45 收盤 ÷ 13:15 收盤。
判定標準在跑之前寫在 update.md 發現 75；只跑一次（結果存 data/intraday_momentum_result.json；--rerun 才重跑）。
純離線研究：策略／風控／下單模組不得引用（tests/test_intraday_momentum_study.py 靜態檢查）。
資料：bars_1m（TXF 日盤 1 分 K，連續近月；hhmm＝K 棒結束時間）唯讀；交易日曆用期交所 TX 日行情；月結算日用期交所最後結算價表。
用法：python -m core.intraday_momentum_study run [--rerun]"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional, Sequence

import numpy as np

from core.weekly_straddle_study import TPE, mean_test, slope_test

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "intraday_momentum_result.json"
DB = ROOT / "data" / "market_state.db"
NEED = (846, 915, 1245, 1315, 1345)
NW_LAG = 5
N_TESTS = 2
Z = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)          # ≈ 1.96：單尾、Bonferroni
PTS = 47000.0
COST = 4.0 / PTS
COSTS = {"微台": 4.0, "小台": 1.5, "大台": 0.5}


def load_bars(db: Path = DB, code: str = "TXF") -> dict[str, dict[int, tuple[float, float]]]:
    """日期 → {hhmm: (開, 收)}，只取需要的幾根（唯讀）。"""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        out: dict[str, dict[int, tuple[float, float]]] = {}
        q = f"SELECT date, hhmm, open, close FROM bars_1m WHERE code=? AND hhmm IN ({','.join('?' * len(NEED))})"
        for d, hm, o, cl in c.execute(q, (code, *NEED)):
            if o and cl and o > 0 and cl > 0:
                out.setdefault(d, {})[int(hm)] = (float(o), float(cl))
        return out
    finally:
        c.close()


def build(bars: dict[str, dict[int, tuple[float, float]]], calendar: Sequence[str], monthly: set[str], weekly: set[str]) -> tuple[dict[str, np.ndarray], dict[str, int]]:
    """每個可用的交易日一列。前一天要是交易日曆上的前一個交易日；月結算日與它的下一個交易日略過（連續近月在那裡換合約）。"""
    cal = sorted(calendar)
    prev = {cal[i]: cal[i - 1] for i in range(1, len(cal))}
    rows, skipped = [], {"no_prev": 0, "roll": 0, "missing": 0}
    for d in sorted(bars):
        p = prev.get(d)
        if p is None or p not in bars:
            skipped["no_prev"] += 1
            continue
        if d in monthly or p in monthly:
            skipped["roll"] += 1
            continue
        b, pb = bars[d], bars[p]
        if 1345 not in pb or any(h not in b for h in NEED):
            skipped["missing"] += 1
            continue
        rows.append((d, np.log(b[915][1] / pb[1345][1]), np.log(b[915][1] / b[846][0]), np.log(b[1315][1] / b[1245][1]),
                     np.log(b[1345][1] / b[1315][1]), d in weekly))
    cols = list(zip(*rows)) if rows else [[]] * 6
    out = {k: np.array(v, dtype=float) for k, v in zip(("first", "open30", "pre", "last", "wexp"), cols[1:])}
    out["date"] = np.array(cols[0], dtype=object)
    return out, skipped


def trades(signal: np.ndarray, last: np.ndarray, cost: float = COST) -> tuple[np.ndarray, np.ndarray]:
    """照 signal 的方向在 13:15 進場、13:45 出場；signal＝0 不做。回傳 (每筆淨報酬, 有做的那些列)。"""
    s = np.sign(signal)
    on = s != 0
    return s[on] * last[on] - cost, on


def _slope_halves(y: np.ndarray, x: np.ndarray) -> dict[str, Any]:
    out = slope_test(y, x, NW_LAG)
    h = len(y) // 2
    out["first"] = slope_test(y[:h], x[:h], NW_LAG)["b"] if h >= 4 else None
    out["second"] = slope_test(y[h:], x[h:], NW_LAG)["b"] if len(y) - h >= 4 else None
    out["pass"] = bool(out["t"] is not None and out["t"] >= Z and (out["first"] or 0) > 0 and (out["second"] or 0) > 0)
    return out


def _mean_halves(x: np.ndarray) -> dict[str, Any]:
    out = mean_test(x, NW_LAG)
    h = len(x) // 2
    out["first"] = float(x[:h].mean()) if h else None
    out["second"] = float(x[h:].mean()) if len(x) - h else None
    out["pass"] = bool(out["t"] is not None and out["t"] >= Z and (out["first"] or 0) > 0 and (out["second"] or 0) > 0)
    return out


def _pts(x: np.ndarray) -> Optional[float]:
    return float(x.mean() * PTS) if len(x) else None


def describe(b: dict[str, np.ndarray]) -> dict[str, Any]:
    first, last = b["first"], b["last"]
    tr, on = trades(first, last)
    gross = tr + COST
    out: dict[str, Any] = {"gross_pts": _pts(gross), "win": float(np.mean(tr > 0)) if len(tr) else None,
                           "net_pts": {k: _pts(gross - c / PTS) for k, c in COSTS.items()},
                           "last_sd_pts": float(last.std() * PTS)}
    edges = np.quantile(first, [0.2, 0.4, 0.6, 0.8])
    q = np.searchsorted(edges, first, side="right")
    out["quintiles"] = [{"n": int((q == i).sum()), "first": _pts(first[q == i]), "last": _pts(last[q == i])} for i in range(5)]
    for k in ("open30", "pre"):
        s = slope_test(last, b[k], NW_LAG)
        t, _ = trades(b[k], last)
        out[k] = {"b": s["b"], "t": s["t"], "net_pts": _pts(t)}
    yrs = np.array([d[:4] for d in b["date"]])
    yo = yrs[on]
    out["by_year"] = {y: {"n": int((yo == y).sum()), "net_pts": _pts(tr[yo == y])} for y in sorted(set(yrs))}
    w = b["wexp"][on] > 0
    out["weekly_expiry"] = {"yes": {"n": int(w.sum()), "net_pts": _pts(tr[w])}, "no": {"n": int((~w).sum()), "net_pts": _pts(tr[~w])},
                            "slope_yes_t": slope_test(last[b["wexp"] > 0], first[b["wexp"] > 0], NW_LAG)["t"]}
    return out


def run(bars: dict[str, dict[int, tuple[float, float]]], calendar: Sequence[str], monthly: set[str], weekly: set[str]) -> dict[str, Any]:
    b, skipped = build(bars, calendar, monthly, weekly)
    if len(b["last"]) < 30:
        return {"n": len(b["last"]), "skipped": skipped, "verdict": {"T1": "沒有樣本", "T2": "沒有樣本"}, "describe": {}}
    r1 = _slope_halves(b["last"], b["first"])
    tr, _ = trades(b["first"], b["last"])
    r2 = _mean_halves(tr)
    return {"n": len(b["last"]), "first": b["date"][0], "last": b["date"][-1], "skipped": skipped, "z": Z, "T1": r1, "T2": r2,
            "describe": describe(b), "verdict": {"T1": "過關" if r1["pass"] else "沒過", "T2": "過關" if r2["pass"] else "沒過"}}


# ── 報告 ──────────────────────────────────────────────────────────

def _f(x: Optional[float], spec: str = "+.1f") -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    if not r.get("describe"):
        return f"樣本不夠（{r['n']} 天）：{r['verdict']}"
    d = r["describe"]
    L = [f"日內動能：{r['n']} 個日盤（{r['first']}～{r['last']}），略過 {r['skipped']}；點數＝比例 × {PTS:,.0f}",
         f"T1 最後 30 分鐘對早上 30 分鐘（含跳空）的斜率：b＝{_f(r['T1']['b'], '+.4f')}、t＝{_f(r['T1']['t'], '+.2f')}（要 ≥ {r['z']:.2f}）、"
         f"前半 {_f(r['T1']['first'], '+.4f')}／後半 {_f(r['T1']['second'], '+.4f')} → {r['verdict']['T1']}",
         f"T2 13:15 照早上方向進場、13:45 出場（扣 4 點）：{r['T2']['n']} 筆、每筆 {_f(None if r['T2']['mean'] is None else r['T2']['mean'] * PTS)} 點、"
         f"t＝{_f(r['T2']['t'], '+.2f')}、前半 {_f(None if r['T2']['first'] is None else r['T2']['first'] * PTS)}／後半 {_f(None if r['T2']['second'] is None else r['T2']['second'] * PTS)} → {r['verdict']['T2']}",
         f"毛報酬每筆 {_f(d['gross_pts'])} 點、勝率 {_f(d['win'], '.0%')}；淨（微台／小台／大台）" + "／".join(_f(v) for v in d["net_pts"].values()) + f" 點；最後 30 分鐘的標準差 {d['last_sd_pts']:.0f} 點",
         "早上 30 分鐘五分位（跌最多 → 漲最多）之後的最後 30 分鐘：" + "、".join(f"{_f(q['last'])}" for q in d["quintiles"]) + " 點",
         f"只用開盤後 30 分鐘（不含跳空）：斜率 t {_f(d['open30']['t'], '+.2f')}、每筆淨 {_f(d['open30']['net_pts'])} 點；"
         f"倒數第二個半小時當預測者：t {_f(d['pre']['t'], '+.2f')}、每筆淨 {_f(d['pre']['net_pts'])} 點",
         f"週選結算日 {d['weekly_expiry']['yes']['n']} 天：每筆淨 {_f(d['weekly_expiry']['yes']['net_pts'])} 點（斜率 t {_f(d['weekly_expiry']['slope_yes_t'], '+.2f')}）；"
         f"其他 {d['weekly_expiry']['no']['n']} 天：{_f(d['weekly_expiry']['no']['net_pts'])} 點",
         "每年（每筆淨）：" + "；".join(f"{y} {_f(v['net_pts'], '+.0f')}" for y, v in d["by_year"].items())]
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "run":
        print(__doc__)
        return 2
    if RESULT_PATH.exists() and "--rerun" not in a:
        print("已經跑過一次（判定標準只跑一次）；結果：\n" + format_report(json.loads(RESULT_PATH.read_text(encoding="utf-8"))))
        return 0
    from core import option_history as oh
    from core.weekly_straddle_study import TAIFEX_DIR, load_tx
    fsp = oh.load_fsp(TAIFEX_DIR)
    monthly = {d.isoformat() for c, (d, _) in fsp.items() if c.isdigit()}
    weekly = {d.isoformat() for c, (d, _) in fsp.items() if not c.isdigit()}
    r = run(load_bars(), sorted(load_tx(TAIFEX_DIR / "tx")), monthly, weekly)
    r["generated_at"] = datetime.now(TPE).isoformat(timespec="seconds")
    RESULT_PATH.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(format_report(r))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
