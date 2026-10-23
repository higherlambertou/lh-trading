"""週選跨式買方的時機（update.md 發現 71）：到期前一個交易日的日盤收盤買價平跨式、持有到最後結算；
「最近 20 天平均每天走幾點」÷「跨式成本」（ratio）能不能挑出值得買的時候。

判定標準在跑之前寫在 update.md 發現 71；只跑一次（結果存 data/weekly_straddle_result.json；--rerun 才重跑）。
純離線研究：策略／風控／下單模組不得引用（tests/test_weekly_straddle_study.py 靜態檢查）。
資料：core/option_history fetch-short（data/taifex/optw/ 短天期鏈、opt_fsp.csv 官方最後結算價）、TX 日行情（data/taifex/tx/）、
美國 CPI 公布日（lh-intel 的 intel.db，只描述用；日期是事實，同 core/event_day_study 的讀法）。
用法：python -m core.weekly_straddle_study run [--rerun]"""
from __future__ import annotations

import json
import sys
from bisect import bisect_left
from datetime import date, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from core import option_backtest as ob
from core.oi_study import TX_HEADER, _iso, _num, _rows

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "weekly_straddle_result.json"
TAIFEX_DIR = ROOT / "data" / "taifex"
DEFAULT_INTEL_DB = ROOT.parent / "lh-intel" / "data" / "intel.db"
FIRST = "2012-11-01"                    # 週選擇權 2012-11-28 第一次到期
N_RM = 20
NW_LAG = 5
N_TESTS = 2
Z = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)          # ≈ 1.96：單尾、Bonferroni
MIN_T2 = 60
THRESHOLD = 1.0                         # ratio > 1：最近每天平均走的點數超過跨式成本
SENS = 0.015                            # 敏感度：買進價改用結算價 × 1.015
CAP = 5.0                               # 穩健度：報酬截在 +500%
RECENT_DAYS = 730
TPE = ZoneInfo("Asia/Taipei")


# ── 資料 ──────────────────────────────────────────────────────────

class TxDay(NamedTuple):
    front: str                          # 當天日盤成交量最大的月合約
    close: dict[str, float]             # 各月合約的日盤收盤


def load_tx(tx_dir: Path = TAIFEX_DIR / "tx") -> dict[str, TxDay]:
    """期交所 TX 日行情 → 日期 → 日盤各月合約（6 位數，價差與週合約不算）的收盤與近月（成交量最大的）。"""
    days: dict[str, dict[str, tuple[float, float]]] = {}
    for f in sorted(tx_dir.glob("*.csv")):
        for r in _rows(f, TX_HEADER):
            code = (r.get("到期月份(週別)") or "").strip()
            if not code.isdigit() or len(code) != 6 or (r.get("交易時段") or "一般").strip() not in ("一般", ""):
                continue
            c, vol = _num(r.get("收盤價", "")), _num(r.get("成交量", ""))
            if c is None or c <= 0:
                continue
            days.setdefault(_iso(r["交易日期"]), {})[code] = (c, vol or 0.0)
    return {d: TxDay(max(v, key=lambda k: v[k][1]), {k: x[0] for k, x in v.items()}) for d, v in sorted(days.items())}


def daily_moves(tx: dict[str, TxDay]) -> tuple[list[str], list[Optional[float]]]:
    """每個交易日：近月日盤收盤對「同一個合約」前一個交易日收盤的變動絕對值（換月不會跳）；前一天沒有這個合約是 None。"""
    dates = sorted(tx)
    mv: list[Optional[float]] = [None]
    for i in range(1, len(dates)):
        d, p = tx[dates[i]], tx[dates[i - 1]]
        c0 = p.close.get(d.front)
        mv.append(abs(d.close[d.front] - c0) if c0 is not None else None)
    return dates, mv


def rm(mv: Sequence[Optional[float]], i: int, n: int = N_RM) -> Optional[float]:
    """第 i 個交易日（含）以前 n 天的平均；n 天都要有。"""
    if i < 0 or i >= len(mv) or i - n + 1 < 0:
        return None
    w = mv[i - n + 1:i + 1]
    return None if any(x is None for x in w) else float(np.mean(w))


def group_chain(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """短天期的列 → {(日期, 到期代碼): {"F", "kind", "q": {(履約價, 'C'/'P'): Q}}}，只要一般時段。"""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if r["session"] != "G":
            continue
        g = out.setdefault((r["date"], r["expiry"]), {"F": float(r["F"]), "kind": r["kind"], "q": {}})
        g["q"][(float(r["strike"]), r["right"])] = ob.Q(r["settle"], r["close"], r["bid"], r["ask"])
    return out


def pick_atm(g: dict[str, Any]) -> Optional[float]:
    """買權、賣權都有結算價、最接近遠期價的履約價（一樣近取低的）。"""
    def has(k: float, s: str) -> bool:
        q = g["q"].get((k, s))
        return q is not None and q.settle is not None and q.settle > 0
    ks = [k for k in {k for k, _ in g["q"]} if has(k, "C") and has(k, "P")]
    return min(ks, key=lambda k: (abs(k - g["F"]), k)) if ks else None


def in_window(times: Sequence[float], entry: str, expiry: str) -> bool:
    """有沒有任何時間點落在（進場日 13:45, 到期日 13:30］。"""
    a = datetime.fromisoformat(entry).replace(hour=13, minute=45, tzinfo=TPE).timestamp()
    b = datetime.fromisoformat(expiry).replace(hour=13, minute=30, tzinfo=TPE).timestamp()
    return any(a < t <= b for t in times)


def build_trades(chain: dict[tuple[str, str], dict[str, Any]], fsp: dict[str, tuple[date, float]], tx: dict[str, TxDay],
                 cpi: Optional[Sequence[float]] = None, first: str = FIRST) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """每個到期代碼一筆（依到期日排序）；回傳 (trades, 略過的原因與筆數)。"""
    dates, mv = daily_moves(tx)
    skipped = {"no_entry_rows": 0, "no_atm": 0, "no_rm20": 0}
    trades = []
    for code, (ed, px) in sorted(fsp.items(), key=lambda x: (x[1][0], x[0])):
        e = ed.isoformat()
        if e < first:
            continue
        j = bisect_left(dates, e) - 1                                                    # 到期日前一個交易日
        if j < 0 or (dates[j], code) not in chain:
            skipped["no_entry_rows"] += 1
            continue
        d, g = dates[j], chain[(dates[j], code)]
        k = pick_atm(g)
        if k is None:
            skipped["no_atm"] += 1
            continue
        r20 = rm(mv, j)
        if r20 is None:
            skipped["no_rm20"] += 1
            continue
        qc, qp = g["q"][(k, "C")], g["q"][(k, "P")]
        cost = ob.buy_cost(ob.ask_px(qc)) + ob.buy_cost(ob.ask_px(qp))
        cost_sens = ob.buy_cost(qc.settle * (1 + SENS)) + ob.buy_cost(qp.settle * (1 + SENS))
        payoff = ob.settle_proceeds(abs(px - k))
        peek = rm(mv, j + 1) if j + 1 < len(dates) and dates[j + 1] == e else None           # 金絲雀：包含到期日當天的變動
        trades.append({"code": code, "kind": "M" if code.isdigit() else code[6], "expiry": e, "entry": d, "K": k, "F": g["F"], "fsp": px,
                       "mid": qc.settle + qp.settle, "cost": cost, "cost_sens": cost_sens, "payoff": payoff,
                       "ret": payoff / cost - 1, "ret_sens": payoff / cost_sens - 1, "rm20": r20, "ratio": r20 / cost,
                       "ratio_peek": None if peek is None else peek / cost,
                       "asks": sum(1 for q in (qc, qp) if ob.ask_reasonable(q)),
                       "cpi": None if cpi is None else in_window(cpi, d, e)})
    return trades, skipped


# ── 統計 ──────────────────────────────────────────────────────────

def nw_var(u: np.ndarray, lag: int = NW_LAG) -> float:
    """Newey-West（Bartlett 權重）的長期變異數（每筆）。"""
    n = len(u)
    s = float(u @ u) / n
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * float(u[l:] @ u[:-l]) / n
    return s


def mean_test(y: np.ndarray, lag: int = NW_LAG) -> dict[str, Any]:
    n = len(y)
    if n < 3:
        return {"n": n, "mean": None, "t": None}
    m = float(np.mean(y))
    se = (nw_var(y - m, lag) / n) ** 0.5
    return {"n": n, "mean": m, "t": m / se if se > 0 else None}


def slope_test(y: np.ndarray, x: np.ndarray, lag: int = NW_LAG) -> dict[str, Any]:
    n = len(y)
    if n < 4:
        return {"n": n, "b": None, "t": None}
    xc = x - x.mean()
    sxx = float(xc @ xc)
    if sxx <= 0:
        return {"n": n, "b": None, "t": None}
    b = float(xc @ (y - y.mean())) / sxx
    e = y - y.mean() - b * xc
    se = (n * nw_var(xc * e, lag)) ** 0.5 / sxx
    return {"n": n, "b": b, "t": b / se if se > 0 else None}


def _halves(trades: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    h = len(trades) // 2
    return list(trades[:h]), list(trades[h:])


def t1(trades: Sequence[dict[str, Any]], key: str = "ratio") -> dict[str, Any]:
    tr = [t for t in trades if t.get(key)]
    arr = lambda xs: (np.array([t["ret"] for t in xs]), np.log([t[key] for t in xs]))   # noqa: E731
    out = slope_test(*arr(tr))
    a, b = _halves(tr)
    out["b_first"] = slope_test(*arr(a))["b"] if len(a) >= 4 else None
    out["b_second"] = slope_test(*arr(b))["b"] if len(b) >= 4 else None
    out["pass"] = bool(out["t"] is not None and out["t"] >= Z and (out["b_first"] or 0) > 0 and (out["b_second"] or 0) > 0)
    return out


def t2(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    sub = [t for t in trades if t["ratio"] > THRESHOLD]
    out = mean_test(np.array([t["ret"] for t in sub]))
    a, b = _halves(sub)
    out["first"] = float(np.mean([t["ret"] for t in a])) if a else None
    out["second"] = float(np.mean([t["ret"] for t in b])) if b else None
    out["pass"] = bool(out["t"] is not None and out["t"] >= Z and out["n"] >= MIN_T2 and (out["first"] or 0) > 0 and (out["second"] or 0) > 0)
    return out


def _summ(xs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not xs:
        return {"n": 0}
    r = np.array([t["ret"] for t in xs])
    return {"n": len(xs), "mean": float(r.mean()), "median": float(np.median(r)), "win": float(np.mean(r > 0)),
            "mean_sens": float(np.mean([t["ret_sens"] for t in xs])), "mean_capped": float(np.minimum(r, CAP).mean()),
            "share_signal": float(np.mean([t["ratio"] > THRESHOLD for t in xs]))}


def describe(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"all": _summ(trades)}
    out["realized_over_implied"] = {"median": float(np.median([abs(t["fsp"] - t["F"]) / t["mid"] for t in trades])),
                                    "mean": float(np.mean([abs(t["fsp"] - t["F"]) / t["mid"] for t in trades]))} if trades else {}
    out["by_year"] = {y: _summ([t for t in trades if t["expiry"][:4] == y]) for y in sorted({t["expiry"][:4] for t in trades})}
    out["by_kind"] = {k: _summ([t for t in trades if t["kind"] == k]) for k in ("W", "F", "M")}
    out["signal"] = _summ([t for t in trades if t["ratio"] > THRESHOLD])
    lr = np.log([t["ratio"] for t in trades]) if trades else np.array([])
    qs = []
    if len(trades) >= 10:
        edges = np.quantile(lr, [0.2, 0.4, 0.6, 0.8])
        b = np.searchsorted(edges, lr, side="right")
        for i in range(5):
            xs = [t for t, bi in zip(trades, b) if bi == i]
            qs.append({**_summ(xs), "ratio_mean": float(np.mean([t["ratio"] for t in xs])) if xs else None})
    out["quintiles"] = qs
    if trades and trades[0].get("cpi") is not None:
        out["cpi"] = {"yes": _summ([t for t in trades if t["cpi"]]), "no": _summ([t for t in trades if not t["cpi"]])}
    else:
        out["cpi"] = None
    if trades:
        last = date.fromisoformat(trades[-1]["expiry"])
        rec = [t for t in trades if (last - date.fromisoformat(t["expiry"])).days < RECENT_DAYS]
        out["recent"] = {"n": len(rec), "since": rec[0]["expiry"] if rec else None,
                         "pts_net": float(np.mean([t["payoff"] - t["cost"] for t in rec])) if rec else None,
                         "pts_cost": float(np.mean([t["cost"] for t in rec])) if rec else None,
                         "pts_net_signal": float(np.mean([t["payoff"] - t["cost"] for t in rec if t["ratio"] > THRESHOLD]))
                         if any(t["ratio"] > THRESHOLD for t in rec) else None}
        out["asks_used"] = float(np.mean([t["asks"] / 2 for t in trades]))
    sig = [t["ret"] for t in trades if t["ratio"] > THRESHOLD]
    out["t2_mde"] = float((Z + NormalDist().inv_cdf(0.8)) * np.std(sig, ddof=1) / len(sig) ** 0.5) if len(sig) >= 3 else None
    return out


def run(chain: dict[tuple[str, str], dict[str, Any]], fsp: dict[str, tuple[date, float]], tx: dict[str, TxDay],
        cpi: Optional[Sequence[float]] = None, first: str = FIRST) -> dict[str, Any]:
    trades, skipped = build_trades(chain, fsp, tx, cpi, first)
    r1, r2 = t1(trades), t2(trades)
    canary = t1(trades, key="ratio_peek")
    return {"n": len(trades), "first": trades[0]["expiry"] if trades else None, "last": trades[-1]["expiry"] if trades else None,
            "skipped": skipped, "z": Z, "T1": r1, "T2": r2, "canary": {"t": canary["t"], "b": canary["b"], "n": canary["n"],
                                                                        "ok": bool(canary["t"] is not None and r1["t"] is not None and canary["t"] > r1["t"])},
            "describe": describe(trades),
            "verdict": {"T1": "過關" if r1["pass"] else "沒過", "T2": "過關" if r2["pass"] else "沒過"} if trades else {"T1": "沒有樣本", "T2": "沒有樣本"}}


# ── 報告 ──────────────────────────────────────────────────────────

def _p(x: Optional[float], spec: str = "+.1%") -> str:
    return "—" if x is None else format(x, spec)


def _f(x: Optional[float], spec: str = ".2f") -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    d = r["describe"]
    a = d["all"]
    L = [f"週選跨式買方（到期前一個交易日收盤買價平跨式、持有到結算）：{r['n']} 筆，{r['first']}～{r['last']}；略過 {r['skipped']}",
         f"T1 報酬對 ln(最近 20 天平均每天走的點數 ÷ 跨式成本) 的斜率：b＝{_f(r['T1']['b'], '+.3f')}、t＝{_f(r['T1']['t'])}（要 ≥ {r['z']:.2f}）、"
         f"前半 {_f(r['T1']['b_first'], '+.3f')}／後半 {_f(r['T1']['b_second'], '+.3f')} → {r['verdict']['T1']}",
         f"T2 只在 ratio > 1 時買：{r['T2']['n']} 筆、平均 {_p(r['T2']['mean'])}、t＝{_f(r['T2']['t'])}、前半 {_p(r['T2']['first'])}／後半 {_p(r['T2']['second'])} → {r['verdict']['T2']}",
         f"金絲雀（RM20 包含到期日當天＝偷看）：t＝{_f(r['canary']['t'])}，{'比 T1 大（正常）' if r['canary']['ok'] else '沒有比 T1 大（管線可能有問題）'}"]
    if a.get("n"):
        L.append(f"全部：平均 {_p(a['mean'])}、中位數 {_p(a['median'])}、勝率 {a['win']:.0%}；買進價改結算價×1.015：{_p(a['mean_sens'])}；報酬截在 +500%：{_p(a['mean_capped'])}；"
                 f"實際走的 ÷ 跨式結算價：中位數 {d['realized_over_implied']['median']:.2f}、平均 {d['realized_over_implied']['mean']:.2f}；用到收盤賣價的腳 {d['asks_used']:.0%}")
        s = d["signal"]
        if s.get("n"):
            L.append(f"ratio > 1（{s['n']} 筆，占 {a['share_signal']:.0%}）：平均 {_p(s['mean'])}、中位數 {_p(s['median'])}、勝率 {s['win']:.0%}；敏感度 {_p(s['mean_sens'])}、截 +500% {_p(s['mean_capped'])}；"
                     f"偵測力（80%）約 ±{_p(d['t2_mde'], '.0%')}")
        L.append("ratio 五分位（低 → 高）：" + "；".join(f"{q['n']} 筆 ratio≈{_f(q['ratio_mean'])} 平均 {_p(q.get('mean'))}" for q in d["quintiles"]))
        L.append("每年：" + "；".join(f"{y} {v['n']} 筆 {_p(v.get('mean'))}" for y, v in d["by_year"].items()))
        L.append("種類：" + "；".join(f"{k} {v['n']} 筆 {_p(v.get('mean'))}" for k, v in d["by_kind"].items() if v.get("n")))
        if d["cpi"]:
            L.append(f"持有期間有 CPI：{d['cpi']['yes']['n']} 筆 平均 {_p(d['cpi']['yes'].get('mean'))}；其他 {d['cpi']['no']['n']} 筆 {_p(d['cpi']['no'].get('mean'))}（樣本少，只描述）")
        rc = d["recent"]
        L.append(f"最近兩年（{rc['since']} 起 {rc['n']} 筆）：平均成本 {_f(rc['pts_cost'], '.0f')} 點、每筆淨 {_f(rc['pts_net'], '+.0f')} 點；ratio > 1 的每筆淨 {_f(rc['pts_net_signal'], '+.0f')} 點")
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
        cpi: Optional[list[float]] = load_events(DEFAULT_INTEL_DB)["CPI"]
    except Exception as e:  # noqa: BLE001
        print(f"讀不到 CPI 日期（只影響描述）：{e}", file=sys.stderr)
        cpi = None
    r = run(group_chain(oh.iter_short(TAIFEX_DIR)), oh.load_fsp(TAIFEX_DIR), load_tx(TAIFEX_DIR / "tx"), cpi)
    r["generated_at"] = datetime.now(TPE).isoformat(timespec="seconds")
    RESULT_PATH.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(format_report(r))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
