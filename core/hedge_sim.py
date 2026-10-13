"""用微台對沖 ATM 跨式的 Delta：歷史模擬（update.md 發現 30 C；判定標準在跑之前就寫在 update.md，只跑一次）。

買同一個 ATM 跨式（core/option_backtest 的週期）持有到期，再用台指期（TXF 的 1 分 K 當微台的代理）把淨 Delta 對沖掉：
- Delta：Black-76（r＝0），IV＝該到期月**前一個交易日**收盤的 ATM IV（盤中不偷看當天的）、T 用分鐘算到到期日 13:30；
  微台口數＝round(5 × 跨式淨 Delta)（微台每點 10 元、選擇權每點 50 元）；淨 Delta 為正就放空。
- 對沖時機：H1 每天收盤；H2 每小時（每天第 0、60、120、180、240 根）加收盤；H3 每分鐘檢查，淨 Delta 與已對沖的差 ≥ 0.15（選擇權單位）就調整。
  進場當天收盤才買跨式、才開始對沖；到期日不再調整，最後一根（13:30）全部平倉。
- 損益＝−口數 × 10 × 標的變動（連隔夜的跳空一起算）；每口每次調整成本 3 點（＝30 元）。

誠實的限制：
- 用 TXF 日盤 1 分 K（2020-03 起）當微台：夜盤的價格沒有，隔夜的跳空是用「日盤收盤到隔天日盤第一根」算，部位在夜盤實際的損益軌跡沒模擬；
  成本是假設值（每口每次 3 點），敏感度看 1／5 點；微台實際的買賣價差與滑價要看成交紀錄。
- 到期日的最後一根用 13:30（到期合約最後一刻）：1 分 K 是連續月，13:30 之後是下個月合約（和到期合約差基差與隔夜的行情），所以不能用日盤最後一根（13:45）。
- 每個檢定只有約 76 個週期，統計力有限（附最小可偵測效果 MDE）；沒過關不等於證明沒有。

用法：
    python -m core.hedge_sim run [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, time
from pathlib import Path
from statistics import NormalDist, mean, stdev
from typing import Any, Optional, Sequence

import core.option_backtest as ob
from core.option_history import DEFAULT_DIR, iter_rows

TMF_MULT = 10.0                        # 微台每點 10 元
OPT_MULT = 50.0                        # 選擇權每點 50 元
LOTS_PER_DELTA = OPT_MULT / TMF_MULT   # 1 單位選擇權 Delta ＝ 5 口微台
HEDGE_COST_PTS = 3.0                   # 每口每次調整：買賣價差 1 點 ＋ 手續費與稅 2 點（假設）
SENS_COSTS = (1.0, 5.0)
THRESHOLD = 0.15                       # H3：淨 Delta 與已對沖的差（選擇權單位）
HOUR_BARS = 60
LAST_BAR = 1330                        # 到期日最後一根（到期合約最後一刻）
START_ENTRY = date(2020, 4, 1)         # 1 分 K 從 2020-03-02 起：只取這天以後進場的週期
N_TESTS = 4
Z_CLAIM = NormalDist().inv_cdf(1 - 0.05 / N_TESTS / 2)       # ≈ 2.50
Z_HINT = 2.0
SCHEMES = ("H1", "H2", "H3")
SCHEME_TEXT = {"X0": "不對沖", "H1": "每天收盤", "H2": "每小時＋收盤", "H3": "門檻 0.15"}

Bars = dict[str, list[tuple[int, float]]]                    # 日期 → [(hhmm 結束時間, 收盤價)]（由舊到新）


def years_left(day: date, hhmm: int, expiry_date: date) -> float:
    """從 day 的 hhmm（1 分 K 的結束時間）到到期日 13:30，單位年；已過就是 0。"""
    now = datetime.combine(day, time(hhmm // 100, hhmm % 100))
    return max((datetime.combine(expiry_date, time(13, 30)) - now).total_seconds(), 0.0) / (365 * 86400)


def straddle_delta(F: float, K: float, T: float, iv: float) -> float:
    """買進一組同履約價 Call＋Put 的淨 Delta（Black-76、r＝0）＝ 2·N(d1) − 1 ＝ erf(d1/√2)。到期（或 IV 為 0）時是 ±1（價平為 0）。"""
    if T <= 0 or iv <= 0:
        return float((F > K) - (F < K))
    v = iv * math.sqrt(T)
    return math.erf((math.log(F / K) / v + 0.5 * v) / math.sqrt(2.0))


def iv_before(ivs: dict[date, float], day: date) -> Optional[float]:
    """最近一個嚴格早於 day 的 IV（不偷看當天）。"""
    earlier = [d for d in ivs if d < day]
    return ivs[max(earlier)] if earlier else None


def is_hedge_bar(scheme: str, idx: int, last: bool) -> bool:
    """H1、H2 在固定的時間點重新對沖（H3 每根都檢查，另外判斷門檻）。"""
    if scheme == "H1":
        return last
    if scheme == "H2":
        return last or idx % HOUR_BARS == 0
    return True


def hedge_cycle(cyc: ob.Cycle, bars: Bars, ivs: dict[date, float], scheme: str) -> Optional[dict[str, Any]]:
    """一個週期、一種對沖法的對沖損益（元）與交易口數；缺資料（進場日或到期日沒有 1 分 K、沒有前一天的 IV）回 None。"""
    K = cyc.straddle[0][0]
    entry, expiry = cyc.entry_day.isoformat(), cyc.expiry_date.isoformat()
    days = [d for d in sorted(bars) if entry <= d <= expiry]
    if not days or days[0] != entry or days[-1] != expiry:
        return None
    lots, prev, pnl, traded, peak, trades = 0, None, 0.0, 0, 0, 0
    for d in days:
        day = date.fromisoformat(d)
        series = [(h, p) for h, p in bars[d] if d != expiry or h <= LAST_BAR]
        iv = iv_before(ivs, day)
        if not series or iv is None:
            return None
        for idx, (hhmm, px) in enumerate(series):
            if prev is not None:
                pnl += lots * TMF_MULT * (px - prev)
            prev = px
            last = idx == len(series) - 1
            if d == expiry:
                continue                                   # 到期日不再調整，最後一根平倉
            if d == entry and not last:
                continue                                   # 進場當天收盤才買跨式、才開始對沖
            if not is_hedge_bar(scheme, idx, last):
                continue
            nd = straddle_delta(px, K, years_left(day, hhmm, cyc.expiry_date), iv)
            if scheme == "H3" and abs(nd - (-lots / LOTS_PER_DELTA)) < THRESHOLD:           # 已對沖的 Delta（選擇權單位）＝ −口數 ÷ 5
                continue
            target = -round(LOTS_PER_DELTA * nd)
            if target != lots:
                traded += abs(target - lots)
                trades += 1
                lots = target
                peak = max(peak, abs(lots))
    traded += abs(lots)                                    # 到期日最後一根平倉
    return {"pnl_twd": pnl, "traded": traded, "trades": trades, "peak": peak}


def expiry_ivs(directory: Path, fix: Optional[dict[str, date]] = None) -> dict[str, dict[date, float]]:
    """{到期月: {日期: 當天收盤的 ATM IV（小數）}}：每個到期月每天各算一個（用結算價、買賣權平價取遠期，和 option_backtest 同定義）。"""
    out: dict[str, dict[date, float]] = {}
    for day, exps in ob.iter_days(iter_rows(directory), fix):
        for e in exps.values():
            res = ob.atm_iv(day, e)
            if res:
                out.setdefault(e.expiry, {})[day] = res[0] / 100.0
    return out


def load_bars(path: Optional[Path] = None, since: str = "2020-03-02") -> Bars:
    from core.market_store import MarketStore
    out: Bars = {}
    for r in MarketStore(path).bars_1m("TXF", since):
        if r["close"]:
            out.setdefault(r["date"], []).append((int(r["hhmm"]), float(r["close"])))
    return out


def evaluate(cycles: Sequence[ob.Cycle], bars: Bars, ivs: dict[str, dict[date, float]], start: date = START_ENTRY) -> dict[str, Any]:
    """每個週期：不對沖與三種對沖法的損益（元）、成本用的交易口數。缺資料的週期不算（列在 skipped）。"""
    recs, skipped = [], []
    for c in cycles:
        if c.entry_day < start:
            continue
        opt = ob.simulate(c)
        if opt is None:
            skipped.append(c.expiry)
            continue
        hs = {s: hedge_cycle(c, bars, ivs.get(c.expiry, {}), s) for s in SCHEMES}
        if any(h is None for h in hs.values()):
            skipped.append(c.expiry)
            continue
        recs.append({"expiry": c.expiry, "entry": c.entry_day.isoformat(), "outlay_twd": opt["outlay"] * OPT_MULT,
                     "opt_twd": opt["X0"]["pnl"] * OPT_MULT, **{s: hs[s] for s in SCHEMES}})
    return {"records": recs, "skipped": skipped}


def roi(rec: dict[str, Any], scheme: str, cost_pts: float = HEDGE_COST_PTS) -> float:
    """總損益 ÷ 進場付出（元）。scheme＝'X0' 是不對沖。"""
    if scheme == "X0":
        return rec["opt_twd"] / rec["outlay_twd"]
    h = rec[scheme]
    return (rec["opt_twd"] + h["pnl_twd"] - h["traded"] * cost_pts * TMF_MULT) / rec["outlay_twd"]


def t_stat(x: Sequence[float]) -> dict[str, Any]:
    n = len(x)
    if n < 3:
        return {"n": n, "mean": None, "se": None, "t": float("nan"), "mde": None}
    se = stdev(x) / math.sqrt(n)
    return {"n": n, "mean": mean(x), "se": se, "t": mean(x) / se if se > 0 else float("nan"),
            "mde": (Z_CLAIM + ob.Z_POWER) * se}


def verdict(t: float) -> str:
    if math.isnan(t):
        return "無法檢定"
    return "過關" if abs(t) >= Z_CLAIM else ("線索" if abs(t) >= Z_HINT else "沒有")


def run_tests(ev: dict[str, Any]) -> dict[str, Any]:
    """4 個事先登記的檢定與描述。"""
    recs = ev["records"]
    r0 = [roi(r, "X0") for r in recs]
    rh = {s: [roi(r, s) for r in recs] for s in SCHEMES}
    tests = {"C1": t_stat(rh["H1"]), "C2": t_stat(rh["H2"]), "C3": t_stat(rh["H3"]), "C4": t_stat([h - u for h, u in zip(rh["H1"], r0)])}
    for v in tests.values():
        v["verdict"] = verdict(v["t"])
    sd0 = stdev(r0) if len(r0) > 1 else None
    desc: dict[str, Any] = {"cycles": len(recs), "skipped": ev["skipped"], "first": recs[0]["entry"] if recs else None, "last": recs[-1]["entry"] if recs else None,
                            "X0": {"roi": ob.describe(r0), "sd": sd0}}
    for s in SCHEMES:
        x = rh[s]
        sd = stdev(x) if len(x) > 1 else None
        desc[s] = {"roi": ob.describe(x), "sd": sd, "sd_ratio": (sd / sd0) if sd is not None and sd0 else None,
                   "hedge_pnl_twd": mean(r[s]["pnl_twd"] for r in recs) if recs else None,
                   "cost_twd": mean(r[s]["traded"] * HEDGE_COST_PTS * TMF_MULT for r in recs) if recs else None,
                   "traded": mean(r[s]["traded"] for r in recs) if recs else None, "peak": mean(r[s]["peak"] for r in recs) if recs else None,
                   "sens": {str(c): t_stat([roi(r, s, c) for r in recs]) for c in SENS_COSTS}}
    return {"tests": tests, "describe": desc}


def _pc(x: Optional[float], d: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def _sd(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def format_report(res: dict[str, Any]) -> str:
    d, t = res["describe"], res["tests"]
    if not d["cycles"]:
        return "沒有可用的週期（沒有 1 分 K 或 IV）"
    lines = [f"用微台對沖跨式的 Delta（TXF 1 分 K 當微台；週期 {d['cycles']} 個，進場 {d['first']} ~ {d['last']}；缺資料跳過 {len(d['skipped'])} 個）", "",
             "方法            平均ROI  中位數   SD    SD÷不對沖  平均對沖損益   平均成本   平均交易口數  最大持倉口數"]
    x0 = d["X0"]
    lines.append(f"X0 {SCHEME_TEXT['X0']:<10}{_pc(x0['roi']['mean']):>9}{_pc(x0['roi']['median']):>8}{_sd(x0['sd']):>7}{'1.00':>10}{'—':>13}{'—':>11}{'—':>13}{'—':>12}")
    for s in SCHEMES:
        v = d[s]
        lines.append(f"{s} {SCHEME_TEXT[s]:<9}{_pc(v['roi']['mean']):>9}{_pc(v['roi']['median']):>8}{_sd(v['sd']):>7}{v['sd_ratio']:>10.2f}"
                     f"{v['hedge_pnl_twd']:>11,.0f} 元{v['cost_twd']:>8,.0f} 元{v['traded']:>11.1f}{v['peak']:>12.1f}")
    lines += ["", f"4 個事先登記的檢定（雙尾；|t| ≥ {Z_CLAIM:.2f} 過關、≥ {Z_HINT:.1f} 線索；MDE＝80% 力道看得出來的最小效果，單位是 ROI）"]
    names = {"C1": "H1 平均 ROI ≠ 0", "C2": "H2 平均 ROI ≠ 0", "C3": "H3 平均 ROI ≠ 0", "C4": "H1 − X0（對沖有沒有改變平均）"}
    for k in ("C1", "C2", "C3", "C4"):
        v = t[k]
        lines.append(f"  {k} {names[k]:<22} n={v['n']:>3}  平均 {_pc(v['mean']):>8}  t={v['t']:+.2f}  MDE ±{(v['mde'] or 0) * 100:.0f}%  → {v['verdict']}")
    lines += ["", "成本敏感度（每口每次調整 1／3／5 點）的平均 ROI（t）："]
    for s in SCHEMES:
        sv = d[s]["sens"]
        lines.append(f"  {s}：1 點 {_pc(sv['1.0']['mean'])}（{sv['1.0']['t']:+.2f}）　3 點 {_pc(d[s]['roi']['mean'])}　5 點 {_pc(sv['5.0']['mean'])}（{sv['5.0']['t']:+.2f}）")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="python -m core.hedge_sim", description="用微台對沖選擇權 Delta 的歷史模擬")
    ap.add_argument("what", choices=("run",))
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--db", help="market_state.db 路徑（預設用專案的）")
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    tx = ob.tx_expiry_data(d / "tx")
    fix = ob.expiry_fixes(tx[0])
    w = ob.run_walker(d, rules=("main",), fix=fix)
    ob.rescue(w, *tx)
    ev = evaluate([c for c in w.done if c.rule == "main"], load_bars(Path(a.db) if a.db else None), expiry_ivs(d, fix))
    res = run_tests(ev)
    print(format_report(res))
    if a.json:
        Path(a.json).write_text(json.dumps(ob._clean({"result": res, "records": ev["records"]}), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
