"""漲幅發生在什麼時候（update.md 發現 72）：留倉過夜 vs 日盤、月底月初。

判定標準在跑之前寫在 update.md 發現 72；只跑一次（結果存 data/drift_timing_result.json；--rerun 才重跑）。
純離線研究：策略／風控／下單模組不得引用（tests/test_drift_timing_study.py 靜態檢查）。
資料：期交所 TX 日行情（data/taifex/tx/，2003-01 起）；每天取日盤成交量最大的月合約，所有報酬都用同一個合約的價格（換月不跳）。
用法：python -m core.drift_timing_study run [--rerun]"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from core.oi_study import TX_HEADER, _iso, _num, _rows
from core.weekly_straddle_study import mean_test, slope_test

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "drift_timing_result.json"
TX_DIR = ROOT / "data" / "taifex" / "tx"
NW_LAG = 10
N_TESTS = 3
Z = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)          # ≈ 2.13：單尾、Bonferroni
PTS = 47000.0                                          # 換算成現在的點數
COST = 4.0 / PTS                                       # 微台來回成本 4 點（同發現 43～46）
TOM_FIRST = 3                                          # 下個月前幾個交易日算月初
TPE = ZoneInfo("Asia/Taipei")
_WD = "一二三四五六日"


class Day(NamedTuple):
    front: str                          # 日盤成交量最大的月合約
    open: dict[str, float]              # 日盤開盤
    close: dict[str, float]             # 日盤收盤
    n_open: dict[str, float]            # 這個日盤之前的夜盤（期交所盤後列的日期＝它之後那個日盤）
    n_close: dict[str, float]


def load_tx(tx_dir: Path = TX_DIR) -> dict[str, Day]:
    day: dict[str, dict[str, tuple[float, float, float]]] = {}
    night: dict[str, dict[str, tuple[float, float]]] = {}
    for f in sorted(tx_dir.glob("*.csv")):
        for r in _rows(f, TX_HEADER):
            code = (r.get("到期月份(週別)") or "").strip()
            if not code.isdigit() or len(code) != 6:
                continue
            o, c, vol = _num(r.get("開盤價", "")), _num(r.get("收盤價", "")), _num(r.get("成交量", ""))
            if not o or not c or o <= 0 or c <= 0:
                continue
            d, sess = _iso(r["交易日期"]), (r.get("交易時段") or "一般").strip()
            if sess in ("一般", ""):
                day.setdefault(d, {})[code] = (o, c, vol or 0.0)
            elif sess == "盤後":
                night.setdefault(d, {})[code] = (o, c)
    out = {}
    for d in sorted(day):
        v, n = day[d], night.get(d, {})
        out[d] = Day(max(v, key=lambda k: v[k][2]), {k: x[0] for k, x in v.items()}, {k: x[1] for k, x in v.items()},
                     {k: x[0] for k, x in n.items()}, {k: x[1] for k, x in n.items()})
    return out


def tom_flags(dates: Sequence[str]) -> list[bool]:
    """月最後一個交易日（下一個交易日已經是下個月）＋每個月前 TOM_FIRST 個交易日。最後一天不知道是不是月底，算 False。"""
    out, k = [], 0
    for i, d in enumerate(dates):
        k = k + 1 if i and dates[i - 1][:7] == d[:7] else 0          # 這個月的第幾個交易日（從 0 起）
        last = i + 1 < len(dates) and dates[i + 1][:7] != d[:7]
        out.append(last or k < TOM_FIRST)
    return out


def build(days: dict[str, Day]) -> dict[str, np.ndarray]:
    dates_all = sorted(days)
    tom_all = dict(zip(dates_all, tom_flags(dates_all)))
    rows = []
    for i in range(1, len(dates_all)):
        d, p = days[dates_all[i]], days[dates_all[i - 1]]
        c = d.front
        c0 = p.close.get(c)
        if c0 is None:
            continue
        O, D = np.log(d.open[c] / c0), np.log(d.close[c] / d.open[c])
        if c in d.n_open and c in d.n_close:
            g1, n, g2 = np.log(d.n_open[c] / c0), np.log(d.n_close[c] / d.n_open[c]), np.log(d.open[c] / d.n_close[c])
        else:
            g1 = n = g2 = np.nan
        rows.append((dates_all[i], O, D, tom_all[dates_all[i]], date.fromisoformat(dates_all[i]).weekday(), g1, n, g2))
    cols = list(zip(*rows)) if rows else [[]] * 8
    out = {k: np.array(v, dtype=float) for k, v in zip(("O", "D", "tom", "wd", "g1", "night", "g2"), cols[1:])}
    out["date"] = np.array(cols[0], dtype=object)
    out["R"] = out["O"] + out["D"]
    return out


def tom_trades(days: dict[str, Day]) -> list[dict[str, Any]]:
    """月底月初的交易版本：月最後一個交易日的前一天收盤買（那天的近月）、下個月第 TOM_FIRST 個交易日收盤賣（同一個合約）。"""
    dates = sorted(days)
    out = []
    for i in range(1, len(dates) - TOM_FIRST):
        if dates[i + 1][:7] == dates[i][:7]:
            continue                                                  # i 不是月最後一個交易日
        b, s = dates[i - 1], dates[i + TOM_FIRST]
        if s[:7] != dates[i + 1][:7]:
            continue
        c = days[b].front
        if c in days[s].close:
            out.append({"buy": b, "sell": s, "ret": float(np.log(days[s].close[c] / days[b].close[c])) - COST})
    return out


# ── 檢定 ──────────────────────────────────────────────────────────

def _halves_ok(x: np.ndarray, f) -> tuple[Optional[float], Optional[float]]:
    h = len(x) // 2
    return (f(slice(0, h)) if h >= 3 else None), (f(slice(h, len(x))) if len(x) - h >= 3 else None)


def mean_check(x: np.ndarray) -> dict[str, Any]:
    out = mean_test(x, NW_LAG)
    out["first"], out["second"] = _halves_ok(x, lambda s: float(np.mean(x[s])))
    out["pass"] = bool(out["t"] is not None and out["t"] >= Z and (out["first"] or 0) > 0 and (out["second"] or 0) > 0)
    return out


def tom_check(R: np.ndarray, tom: np.ndarray) -> dict[str, Any]:
    out = slope_test(R, tom, NW_LAG)

    def diff(s: slice) -> Optional[float]:
        r, t = R[s], tom[s] > 0
        return float(r[t].mean() - r[~t].mean()) if t.any() and (~t).any() else None
    out["first"], out["second"] = _halves_ok(R, diff)
    out["pass"] = bool(out["t"] is not None and out["t"] >= Z and (out["first"] or 0) > 0 and (out["second"] or 0) > 0)
    return out


def _pts(x: Sequence[float] | np.ndarray) -> Optional[float]:
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    return float(x.mean() * PTS) if len(x) else None


def describe(b: dict[str, np.ndarray], tt: list[dict[str, Any]]) -> dict[str, Any]:
    if not len(b["O"]):
        return {}
    yrs = np.array([d[:4] for d in b["date"]])
    ok = ~np.isnan(b["night"])
    out: dict[str, Any] = {
        "pts": {k: _pts(b[k]) for k in ("O", "D", "R")},
        "t": {k: mean_test(b[k], NW_LAG)["t"] for k in ("O", "D", "R")},
        "by_year": {y: {"n": int((yrs == y).sum()), "O": _pts(b["O"][yrs == y]), "D": _pts(b["D"][yrs == y])} for y in sorted(set(yrs))},
        "weekday": {_WD[w]: {"n": int((b["wd"] == w).sum()), "O": _pts(b["O"][b["wd"] == w]), "D": _pts(b["D"][b["wd"] == w]), "R": _pts(b["R"][b["wd"] == w])}
                    for w in range(5) if (b["wd"] == w).any()},
        "night_split": {"n": int(ok.sum()), "since": b["date"][ok][0] if ok.any() else None,
                        "g1": _pts(b["g1"][ok]), "night": _pts(b["night"][ok]), "g2": _pts(b["g2"][ok]),
                        "night_t": mean_test(b["night"][ok], NW_LAG)["t"] if ok.sum() > 3 else None},
        "short_day": {"pts_net": _pts(-b["D"] - COST), "t": mean_test(-b["D"] - COST, NW_LAG)["t"]},
        "tom_days": {"n": int((b["tom"] > 0).sum()), "R_tom": _pts(b["R"][b["tom"] > 0]), "R_other": _pts(b["R"][b["tom"] == 0])},
    }
    r = np.array([t["ret"] for t in tt])
    out["tom_trade"] = {"n": len(tt), "pts_net": _pts(r) if len(r) else None, "win": float(np.mean(r > 0)) if len(r) else None,
                        "t": mean_test(r, 0)["t"] if len(r) > 3 else None}
    return out


def run(days: dict[str, Day]) -> dict[str, Any]:
    b = build(days)
    if len(b["O"]) < 30:
        return {"n": len(b["O"]), "verdict": {k: "沒有樣本" for k in ("T1", "T2", "T3")}, "describe": {}}
    r1, r2, r3 = mean_check(b["O"] - b["D"]), mean_check(b["O"] - COST), tom_check(b["R"], b["tom"])
    return {"n": len(b["O"]), "first": b["date"][0], "last": b["date"][-1], "z": Z, "T1": r1, "T2": r2, "T3": r3,
            "describe": describe(b, tom_trades(days)),
            "verdict": {k: "過關" if r["pass"] else "沒過" for k, r in (("T1", r1), ("T2", r2), ("T3", r3))}}


# ── 報告 ──────────────────────────────────────────────────────────

def _f(x: Optional[float], spec: str = "+.2f") -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    if not r.get("describe"):
        return f"樣本不夠（{r['n']} 天）：{r['verdict']}"
    d = r["describe"]
    p = lambda x: _f(None if x is None else x * PTS, "+.1f")                       # noqa: E731
    L = [f"漲幅發生在什麼時候：TX {r['n']} 個交易日，{r['first']}～{r['last']}（點數＝比例 × {PTS:,.0f}）",
         f"T1 過夜 − 日盤：每天 {p(r['T1']['mean'])} 點、t＝{_f(r['T1']['t'])}（要 ≥ {r['z']:.2f}）、前半 {p(r['T1']['first'])}／後半 {p(r['T1']['second'])} → {r['verdict']['T1']}",
         f"T2 只留倉過夜做多（扣 4 點）：每天 {p(r['T2']['mean'])} 點、t＝{_f(r['T2']['t'])}、前半 {p(r['T2']['first'])}／後半 {p(r['T2']['second'])} → {r['verdict']['T2']}",
         f"T3 月底月初 − 其他日子：每天 {p(r['T3']['b'])} 點、t＝{_f(r['T3']['t'])}、前半 {p(r['T3']['first'])}／後半 {p(r['T3']['second'])} → {r['verdict']['T3']}",
         f"平均每天：過夜 {_f(d['pts']['O'], '+.1f')} 點（t {_f(d['t']['O'])}）、日盤 {_f(d['pts']['D'], '+.1f')} 點（t {_f(d['t']['D'])}）、收盤到收盤 {_f(d['pts']['R'], '+.1f')} 點",
         f"日盤做空（扣 4 點）：每天 {_f(d['short_day']['pts_net'], '+.1f')} 點（t {_f(d['short_day']['t'])}）"]
    ns = d["night_split"]
    if ns["n"]:
        L.append(f"{ns['since']} 起 {ns['n']} 天把過夜拆開：收盤→夜盤開盤 {_f(ns['g1'], '+.1f')}、夜盤 {_f(ns['night'], '+.1f')}（t {_f(ns['night_t'])}）、夜盤收盤→日盤開盤 {_f(ns['g2'], '+.1f')} 點")
    L.append(f"月底月初 {d['tom_days']['n']} 天：每天 {_f(d['tom_days']['R_tom'], '+.1f')} 點，其他日子 {_f(d['tom_days']['R_other'], '+.1f')} 點；"
             f"交易版本 {d['tom_trade']['n']} 個月、每筆淨 {_f(d['tom_trade']['pts_net'], '+.1f')} 點、勝率 {_f(d['tom_trade']['win'], '.0%')}、t {_f(d['tom_trade']['t'])}")
    L.append("星期：" + "；".join(f"{k} 過夜 {_f(v['O'], '+.1f')}／日盤 {_f(v['D'], '+.1f')}" for k, v in d["weekday"].items()))
    L.append("每年（過夜／日盤）：" + "；".join(f"{y} {_f(v['O'], '+.0f')}／{_f(v['D'], '+.0f')}" for y, v in d["by_year"].items()))
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
