"""到期前一天買價平買權 vs 賣權（update.md 發現 74）：過夜的順風（發現 72）有沒有讓買權比較划算。

規則同發現 71（core/weekly_straddle_study）：到期前一個交易日日盤收盤、同一個價平履約價，買權與賣權各自單買、持有到最後結算。
判定標準在跑之前寫在 update.md 發現 74；只跑一次（結果存 data/call_put_result.json；--rerun 才重跑）。
純離線研究：策略／風控／下單模組不得引用（tests/test_call_put_study.py 靜態檢查）。
用法：python -m core.call_put_study run [--rerun]"""
from __future__ import annotations

import json
import sys
from bisect import bisect_left
from datetime import date, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional, Sequence

import numpy as np

from core import option_backtest as ob
from core import weekly_straddle_study as ws

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "call_put_result.json"
NW_LAG = 5
N_TESTS = 2
Z = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)          # ≈ 1.96：單尾、Bonferroni
SENS = 0.015
CAP = 5.0
RECENT_DAYS = 730
PTS = 47000.0


def build_legs(chain: dict[tuple[str, str], dict[str, Any]], fsp: dict[str, tuple[date, float]], tx: dict[str, ws.TxDay],
               cpi: Optional[Sequence[float]] = None, first: str = ws.FIRST) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """每個到期代碼一筆：同一個價平履約價的買權與賣權各自的成本、到期淨值、報酬（依到期日排序）。"""
    dates = sorted(tx)
    skipped = {"no_entry_rows": 0, "no_atm": 0}
    out = []
    for code, (ed, px) in sorted(fsp.items(), key=lambda x: (x[1][0], x[0])):
        e = ed.isoformat()
        if e < first:
            continue
        j = bisect_left(dates, e) - 1
        if j < 0 or (dates[j], code) not in chain:
            skipped["no_entry_rows"] += 1
            continue
        d, g = dates[j], chain[(dates[j], code)]
        k = ws.pick_atm(g)
        if k is None:
            skipped["no_atm"] += 1
            continue
        qc, qp = g["q"][(k, "C")], g["q"][(k, "P")]
        cc, cp = ob.buy_cost(ob.ask_px(qc)), ob.buy_cost(ob.ask_px(qp))
        vc, vp = ob.settle_proceeds(max(px - k, 0.0)), ob.settle_proceeds(max(k - px, 0.0))
        sc, sp = ob.buy_cost(qc.settle * (1 + SENS)), ob.buy_cost(qp.settle * (1 + SENS))
        out.append({"code": code, "kind": "M" if code.isdigit() else code[6], "expiry": e, "entry": d, "K": k, "F": g["F"], "fsp": px,
                    "cost_c": cc, "cost_p": cp, "rc": vc / cc - 1, "rp": vp / cp - 1, "rc_sens": vc / sc - 1, "rp_sens": vp / sp - 1,
                    "pts_c": vc - cc, "pts_p": vp - cp, "cpi": None if cpi is None else ws.in_window(cpi, d, e)})
    return out, skipped


def mean_check(x: np.ndarray) -> dict[str, Any]:
    out = ws.mean_test(x, NW_LAG)
    h = len(x) // 2
    out["first"] = float(x[:h].mean()) if h else None
    out["second"] = float(x[h:].mean()) if len(x) - h else None
    out["pass"] = bool(out["t"] is not None and out["t"] >= Z and (out["first"] or 0) > 0 and (out["second"] or 0) > 0)
    return out


def _leg(xs: Sequence[dict[str, Any]], k: str) -> dict[str, Any]:
    if not xs:
        return {"n": 0}
    r = np.array([t["r" + k] for t in xs])
    return {"n": len(xs), "mean": float(r.mean()), "median": float(np.median(r)), "win": float(np.mean(r > 0)),
            "mean_sens": float(np.mean([t["r" + k + "_sens"] for t in xs])), "mean_capped": float(np.minimum(r, CAP).mean())}


def longest_loss_streak(r: Sequence[float]) -> int:
    best = cur = 0
    for x in r:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def describe(tr: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not tr:
        return {}
    out: dict[str, Any] = {"call": _leg(tr, "c"), "put": _leg(tr, "p")}
    out["by_year"] = {y: {"n": len(xs), "call": float(np.mean([t["rc"] for t in xs])), "put": float(np.mean([t["rp"] for t in xs]))}
                      for y in sorted({t["expiry"][:4] for t in tr}) for xs in [[t for t in tr if t["expiry"][:4] == y]]}
    out["by_kind"] = {k: {"n": len(xs), "call": float(np.mean([t["rc"] for t in xs])), "put": float(np.mean([t["rp"] for t in xs]))}
                      for k in ("W", "F", "M") for xs in [[t for t in tr if t["kind"] == k]] if xs}
    last = date.fromisoformat(tr[-1]["expiry"])
    rec = [t for t in tr if (last - date.fromisoformat(t["expiry"])).days < RECENT_DAYS]
    out["recent"] = {"n": len(rec), "since": rec[0]["expiry"], "pts_c": float(np.mean([t["pts_c"] for t in rec])), "pts_p": float(np.mean([t["pts_p"] for t in rec])),
                     "cost_c": float(np.mean([t["cost_c"] for t in rec])), "cost_p": float(np.mean([t["cost_p"] for t in rec]))}
    if tr[0].get("cpi") is not None:
        ys = [t for t in tr if t["cpi"]]
        out["cpi"] = {"n": len(ys), "call": float(np.mean([t["rc"] for t in ys])) if ys else None, "put": float(np.mean([t["rp"] for t in ys])) if ys else None}
    else:
        out["cpi"] = None
    out["streak_call"] = longest_loss_streak([t["rc"] for t in tr])
    out["drift_pts"] = float(np.mean([np.log(t["fsp"] / t["F"]) for t in tr]) * PTS)              # 持有期間（13:45 → 隔天結算）實際漂了多少（換算 47,000 點）
    out["moneyness_pts"] = float(np.mean([(t["F"] - t["K"]) / t["F"] for t in tr]) * PTS)         # 價平履約價平均偏離遠期價多少（正＝買權偏價內）
    out["expected_diff"] = float(np.mean([(t["fsp"] - t["F"]) / ((t["cost_c"] + t["cost_p"]) / 2) for t in tr]))   # 只靠漂移推算的買權 − 賣權報酬差（約略）
    return out


def run(chain: dict[tuple[str, str], dict[str, Any]], fsp: dict[str, tuple[date, float]], tx: dict[str, ws.TxDay],
        cpi: Optional[Sequence[float]] = None, first: str = ws.FIRST) -> dict[str, Any]:
    tr, skipped = build_legs(chain, fsp, tx, cpi, first)
    if len(tr) < 10:
        return {"n": len(tr), "skipped": skipped, "verdict": {"T1": "沒有樣本", "T2": "沒有樣本"}, "describe": {}}
    rc, rp = np.array([t["rc"] for t in tr]), np.array([t["rp"] for t in tr])
    r1, r2 = mean_check(rc - rp), mean_check(rc)
    return {"n": len(tr), "first": tr[0]["expiry"], "last": tr[-1]["expiry"], "skipped": skipped, "z": Z, "T1": r1, "T2": r2,
            "describe": describe(tr), "verdict": {"T1": "過關" if r1["pass"] else "沒過", "T2": "過關" if r2["pass"] else "沒過"}}


# ── 報告 ──────────────────────────────────────────────────────────

def _p(x: Optional[float], spec: str = "+.1%") -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    if not r.get("describe"):
        return f"樣本不夠（{r['n']} 筆）：{r['verdict']}"
    d = r["describe"]
    c, p = d["call"], d["put"]
    L = [f"到期前一天買價平買權 vs 賣權：{r['n']} 筆（{r['first']}～{r['last']}），略過 {r['skipped']}",
         f"T1 買權 − 賣權：平均 {_p(r['T1']['mean'])}、t＝{_p(r['T1']['t'], '+.2f')}（要 ≥ {r['z']:.2f}）、前半 {_p(r['T1']['first'])}／後半 {_p(r['T1']['second'])} → {r['verdict']['T1']}",
         f"T2 單買買權：平均 {_p(r['T2']['mean'])}、t＝{_p(r['T2']['t'], '+.2f')}、前半 {_p(r['T2']['first'])}／後半 {_p(r['T2']['second'])} → {r['verdict']['T2']}",
         f"買權：平均 {_p(c['mean'])}、中位數 {_p(c['median'])}、勝率 {c['win']:.0%}、結算價×1.015 {_p(c['mean_sens'])}、截 +500% {_p(c['mean_capped'])}；最長連賠 {d['streak_call']} 筆",
         f"賣權：平均 {_p(p['mean'])}、中位數 {_p(p['median'])}、勝率 {p['win']:.0%}、結算價×1.015 {_p(p['mean_sens'])}、截 +500% {_p(p['mean_capped'])}",
         f"持有期間實際漂移 {d['drift_pts']:+.1f} 點（換算 47,000 點）；價平履約價平均偏離遠期價 {d['moneyness_pts']:+.1f} 點；只靠漂移推算的買權 − 賣權約 {_p(d['expected_diff'])}",
         "每年（買權／賣權）：" + "；".join(f"{y} {_p(v['call'], '+.0%')}／{_p(v['put'], '+.0%')}" for y, v in d["by_year"].items()),
         "種類（買權／賣權）：" + "；".join(f"{k} {v['n']} 筆 {_p(v['call'])}／{_p(v['put'])}" for k, v in d["by_kind"].items())]
    if d["cpi"]:
        L.append(f"持有期間有 CPI：{d['cpi']['n']} 筆，買權 {_p(d['cpi']['call'])}、賣權 {_p(d['cpi']['put'])}（樣本少，只描述）")
    rc = d["recent"]
    L.append(f"最近兩年（{rc['since']} 起 {rc['n']} 筆）：買權成本 {rc['cost_c']:.0f} 點、每筆淨 {rc['pts_c']:+.0f} 點；賣權成本 {rc['cost_p']:.0f} 點、每筆淨 {rc['pts_p']:+.0f} 點")
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
    from core.event_day_study import load_events
    try:
        cpi: Optional[list[float]] = load_events(ws.DEFAULT_INTEL_DB)["CPI"]
    except Exception as e:  # noqa: BLE001
        print(f"讀不到 CPI 日期（只影響描述）：{e}", file=sys.stderr)
        cpi = None
    r = run(ws.group_chain(oh.iter_short(ws.TAIFEX_DIR)), oh.load_fsp(ws.TAIFEX_DIR), ws.load_tx(ws.TAIFEX_DIR / "tx"), cpi)
    r["generated_at"] = datetime.now(ws.TPE).isoformat(timespec="seconds")
    RESULT_PATH.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(format_report(r))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
