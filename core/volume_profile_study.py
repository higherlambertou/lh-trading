"""前一日的價量分布算出來的價位，今天的價格有沒有表現得不一樣：POC 磁吸、價值區邊緣反應（update.md 發現 78）。

判定標準在跑之前寫在 update.md 發現 78；只跑一次（結果存 data/volume_profile_result.json；--rerun 才重跑）。
純離線研究：策略／風控／下單模組不得引用（tests/test_volume_profile_study.py 靜態檢查）。
資料：bars_1m（TXF 日盤 1 分 K，唯讀；hhmm＝K 棒結束時間）、期交所 TX 日行情的交易日曆、期交所最後結算價表（月結算日）。
價量分布與價值區的算法與走勢圖顯示用的是同一份（core/volume_profile.levels／value_area）。
用法：python -m core.volume_profile_study run [--rerun]"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from math import sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional, Sequence

from core.volume_profile import levels

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "volume_profile_result.json"
DB = ROOT / "data" / "market_state.db"
MIN_BARS = 280
BIN_FRAC = 0.0002                 # 格寬＝前一日收盤 × 0.02%
D_MIN = 0.2                       # T1 合格：|O − POC| ≥ 0.2R
E_MIN = 0.05                      # T2 合格：|O − 水平| ≥ 0.05R
N_TESTS = 2
Z = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)          # ≈ 1.96：單尾、Bonferroni
MIN_N = 300
MIN_EFFECT = 0.03
BLOCK = 30                        # 80% 規則用的 30 分鐘
SENSITIVITY = (("typical", BIN_FRAC), ("uniform", 0.00015), ("uniform", 0.0003))


# ── 資料 ──────────────────────────────────────────────────────────

Bar = tuple[int, float, float, float, float, float]            # (hhmm, 開, 高, 低, 收, 量)


def load_bars(db: Path = DB) -> dict[str, list[Bar]]:
    """日期 → 日盤 1 分 K（依時間排序）。只取 08:46～13:45 的。"""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        out: dict[str, list[Bar]] = {}
        for d, hm, o, h, l, cl, v in c.execute("SELECT date, hhmm, open, high, low, close, volume FROM bars_1m WHERE code='TXF' AND hhmm BETWEEN 846 AND 1345 ORDER BY date, hhmm"):
            if o and h and l and cl and h >= l > 0:
                out.setdefault(d, []).append((int(hm), float(o), float(h), float(l), float(cl), float(v or 0)))
        return out
    finally:
        c.close()


# ── 價量分布的重建（前一日的 1 分 K → 每一格的量）─────────────────

def bar_volumes(bars: Sequence[Bar], pbin: float, mode: str = "uniform") -> dict[int, float]:
    """uniform：每根的量平均分給它高低範圍涵蓋的每一格；typical：整根的量放在典型價（高＋低＋收）÷ 3 那一格。"""
    acc: dict[int, float] = {}
    for _, _, h, l, c, v in bars:
        if v <= 0:
            continue
        if mode == "typical":
            b = int(((h + l + c) / 3) // pbin)
            acc[b] = acc.get(b, 0.0) + v
        else:
            lo, hi = int(l // pbin), int(h // pbin)
            share = v / (hi - lo + 1)
            for b in range(lo, hi + 1):
                acc[b] = acc.get(b, 0.0) + share
    return acc


def day_levels(bars: Sequence[Bar], ref_price: float, frac: float = BIN_FRAC, mode: str = "uniform") -> Optional[dict[str, float]]:
    """一天的 POC／VAH／VAL；格寬＝ref_price（前一日收盤）× frac（至少 1 點）。沒有量回 None。"""
    pbin = max(1, round(ref_price * frac))
    acc = bar_volumes(bars, pbin, mode)
    lv = levels([{"price": b * pbin, "total": v} for b, v in acc.items()], pbin, ref_price)
    return None if lv is None else {**lv, "pbin": pbin}


def rule80(bars: Sequence[Bar], lv: dict[str, float]) -> Optional[dict[str, Any]]:
    """80% 規則（只描述）：開盤在價值區外 → 之後連續兩個 30 分鐘收盤都在價值區內 → 之後有沒有走到價值區的另一端。回 None＝開盤在價值區內。"""
    o = bars[0][1]
    if lv["val"] <= o <= lv["vah"]:
        return None
    above = o > lv["vah"]
    closes = [bars[min(BLOCK * (j + 1) - 1, len(bars) - 1)][4] for j in range(len(bars) // BLOCK)]
    for i in range(len(closes) - 1):
        if lv["val"] <= closes[i] <= lv["vah"] and lv["val"] <= closes[i + 1] <= lv["vah"]:
            rest = bars[BLOCK * (i + 2):]
            reached = any((b[3] <= lv["val"]) if above else (b[2] >= lv["vah"]) for b in rest)
            return {"above": above, "entered": True, "reached": reached}
    return {"above": above, "entered": False, "reached": False}


def build_days(bars: dict[str, list[Bar]], calendar: Sequence[str], monthly: set[str], frac: float = BIN_FRAC, mode: str = "uniform") -> tuple[list[dict[str, Any]], dict[str, int]]:
    """每個可用的交易日一筆。前一日要是交易日曆上的前一個交易日、兩天都 ≥ MIN_BARS 根；月結算日與它的下一個交易日略過（連續近月換約）。"""
    cal = sorted(calendar)
    prev = {cal[i]: cal[i - 1] for i in range(1, len(cal))}
    out, skipped = [], {"no_prev": 0, "roll": 0, "short": 0, "no_volume": 0}
    for d in sorted(bars):
        p = prev.get(d)
        if p is None or p not in bars:
            skipped["no_prev"] += 1
            continue
        if d in monthly or p in monthly:
            skipped["roll"] += 1
            continue
        b, pb = bars[d], bars[p]
        if len(b) < MIN_BARS or len(pb) < MIN_BARS:
            skipped["short"] += 1
            continue
        lv = day_levels(pb, pb[-1][4], frac, mode)
        lv_d = day_levels(b, pb[-1][4], frac, mode)
        r = max(x[2] for x in pb) - min(x[3] for x in pb)
        if lv is None or lv_d is None or r <= 0:
            skipped["no_volume"] += 1
            continue
        out.append({"date": d, "O": b[0][1], "H": max(x[2] for x in b), "L": min(x[3] for x in b), "C": b[-1][4], "R": r,
                    "poc": lv["poc"], "vah": lv["vah"], "val": lv["val"], "poc_d": lv_d["poc"], "vah_d": lv_d["vah"], "val_d": lv_d["val"], "r80": rule80(b, lv)})
    return out, skipped


# ── 統計（分層的「實際 − 鏡像」；標準誤用影響函數、以天為群組）────────

def strat_stat(groups: dict[Any, list[tuple[str, float]]], weights: dict[Any, float]) -> Optional[dict[str, Any]]:
    """stat＝Σ 權重 × 各組平均；標準誤＝以天為群組的影響函數（每個值對統計量的貢獻 ＝ 權重 × (值 − 組平均) ÷ 組內個數，同一天的加總後平方再加總）。任何一組少於 2 個就回 None。"""
    if any(len(v) < 2 for v in groups.values()):
        return None
    stat, infl = 0.0, {}
    for k, vs in groups.items():
        m = sum(v for _, v in vs) / len(vs)
        stat += weights[k] * m
        for day, v in vs:
            infl[day] = infl.get(day, 0.0) + weights[k] * (v - m) / len(vs)
    se = sqrt(sum(x * x for x in infl.values()))
    return {"stat": stat, "se": se, "t": stat / se if se > 0 else None, "n": {str(k): len(v) for k, v in groups.items()}}


def touched(x: float, d: dict[str, Any]) -> bool:
    return d["L"] <= x <= d["H"]


def t1_groups(days: Sequence[dict[str, Any]], key: str = "") -> dict[Any, list[tuple[str, float]]]:
    """T1：合格日（|O − POC| ≥ 0.2R）的「POC 碰到 − 鏡像價位碰到」，依 POC 在開盤價上方或下方分兩層。key＝"_d" 是偷看用（今天的 POC）。"""
    g: dict[Any, list[tuple[str, float]]] = {"above": [], "below": []}
    for d in days:
        poc, o = d["poc" + key], d["O"]
        if abs(o - poc) < D_MIN * d["R"]:
            continue
        g["above" if poc > o else "below"].append((d["date"], float(touched(poc, d)) - float(touched(2 * o - poc, d))))
    return g


def t2_groups(days: Sequence[dict[str, Any]], key: str = "") -> dict[Any, list[tuple[str, float]]]:
    """T2：每個價值區邊緣（VAH、VAL）與它的鏡像價位，碰到的才算；反彈＝收盤仍在開盤那一邊。依價位在開盤價的上方（壓力型）或下方（支撐型）分層，實際與鏡像各一組。"""
    g: dict[Any, list[tuple[str, float]]] = {(t, k): [] for t in ("res", "sup") for k in ("actual", "mirror")}
    for d in days:
        o, c = d["O"], d["C"]
        for x in (d["vah" + key], d["val" + key]):
            for kind, lvl in (("actual", x), ("mirror", 2 * o - x)):
                if abs(o - lvl) < E_MIN * d["R"] or not touched(lvl, d):
                    continue
                side = 1 if o > lvl else -1                                   # 開盤在價位上方（價位是支撐）或下方（壓力）
                g[("sup" if side > 0 else "res", kind)].append((d["date"], float((c - lvl) * side > 0)))
    return g


T1_W = {"above": 0.5, "below": 0.5}
T2_W = {("res", "actual"): 0.5, ("res", "mirror"): -0.5, ("sup", "actual"): 0.5, ("sup", "mirror"): -0.5}


def halves(days: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    s = sorted(days, key=lambda x: x["date"])
    return s[:len(s) // 2], s[len(s) // 2:]


def evaluate(days: Sequence[dict[str, Any]], key: str = "") -> dict[str, Any]:
    """T1、T2 的統計量、t、前後兩半與是否過關（過關條件見 update.md 發現 78）。"""
    out: dict[str, Any] = {}
    for name, fn, w, count in (("T1", t1_groups, T1_W, lambda g: sum(len(v) for v in g.values())),
                               ("T2", t2_groups, T2_W, lambda g: sum(len(v) for k, v in g.items() if k[1] == "actual"))):
        r = strat_stat(fn(days, key), w)
        if r is None:
            out[name] = {"stat": None, "t": None, "n": 0, "first": None, "second": None, "pass": False}
            continue
        a, b = halves(days)
        ra, rb = strat_stat(fn(a, key), w), strat_stat(fn(b, key), w)
        r["n_events"] = count(fn(days, key))
        r["first"], r["second"] = (ra or {}).get("stat"), (rb or {}).get("stat")
        r["pass"] = bool(r["t"] is not None and r["t"] >= Z and r["n_events"] >= MIN_N and (r["first"] or 0) > 0 and (r["second"] or 0) > 0 and r["stat"] >= MIN_EFFECT)
        out[name] = r
    return out


# ── 描述 ──────────────────────────────────────────────────────────

def _mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def describe(days: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not days:
        return {}
    out: dict[str, Any] = {}
    g1 = t1_groups(days)
    out["t1_by_side"] = {k: {"n": len(v), "bump": _mean([x for _, x in v])} for k, v in g1.items()}
    buckets = {"0.2~0.4R": (0.2, 0.4), "0.4~0.7R": (0.4, 0.7), ">0.7R": (0.7, 99.0)}
    tb: dict[str, Any] = {}
    for name, (lo, hi) in buckets.items():
        sel = [d for d in days if lo <= abs(d["O"] - d["poc"]) / d["R"] < hi]
        act = [float(touched(d["poc"], d)) for d in sel]
        mir = [float(touched(2 * d["O"] - d["poc"], d)) for d in sel]
        tb[name] = {"n": len(sel), "poc_touch": _mean(act), "mirror_touch": _mean(mir)}
    out["t1_by_distance"] = tb
    g2 = t2_groups(days)
    out["t2_rates"] = {f"{k[0]}_{k[1]}": {"n": len(v), "rejected": _mean([x for _, x in v])} for k, v in g2.items()}
    years = sorted({d["date"][:4] for d in days})
    out["by_year"] = {}
    for y in years:
        dy = [d for d in days if d["date"][:4] == y]
        ev = evaluate(dy)
        out["by_year"][y] = {"days": len(dy), "T1": ev["T1"]["stat"], "T2": ev["T2"]["stat"]}
    r80 = [d["r80"] for d in days if d["r80"]]
    ent = [r for r in r80 if r["entered"]]
    out["rule80"] = {"open_outside_va": len(r80), "entered": len(ent), "reached": sum(1 for r in ent if r["reached"]), "rate": (sum(1 for r in ent if r["reached"]) / len(ent)) if ent else None,
                     "from_above": {"entered": sum(1 for r in ent if r["above"]), "reached": sum(1 for r in ent if r["above"] and r["reached"])},
                     "from_below": {"entered": sum(1 for r in ent if not r["above"]), "reached": sum(1 for r in ent if not r["above"] and r["reached"])}}
    return out


def canary_ok(t: Optional[float]) -> bool:
    """偷看的金絲雀要遠大於門檻（3 倍 Z）；沒有算出來或不夠大就是管線有問題。"""
    return t is not None and t > 3 * Z


def run(bars: dict[str, list[Bar]], calendar: Sequence[str], monthly: set[str]) -> dict[str, Any]:
    days, skipped = build_days(bars, calendar, monthly)
    if len(days) < 30:
        return {"n": len(days), "skipped": skipped, "verdict": {"T1": "沒有樣本", "T2": "沒有樣本"}, "describe": {}}
    ev = evaluate(days)
    canary = evaluate(days, "_d")
    sens = {}
    for mode, frac in SENSITIVITY:
        dd, _ = build_days(bars, calendar, monthly, frac, mode)
        e = evaluate(dd)
        sens[f"{mode}_{frac}"] = {"days": len(dd), "T1": {"stat": e["T1"]["stat"], "t": e["T1"]["t"]}, "T2": {"stat": e["T2"]["stat"], "t": e["T2"]["t"]}}
    return {"n": len(days), "first": days[0]["date"], "last": days[-1]["date"], "skipped": skipped, "z": Z, "T1": ev["T1"], "T2": ev["T2"],
            "canary": {"T1_stat": canary["T1"]["stat"], "T1_t": canary["T1"]["t"], "ok": canary_ok(canary["T1"]["t"])},
            "describe": describe(days), "sensitivity": sens, "verdict": {k: "過關" if ev[k]["pass"] else "沒過" for k in ("T1", "T2")}}


# ── 報告 ──────────────────────────────────────────────────────────

def _f(x: Optional[float], spec: str = "+.3f") -> str:
    return "—" if x is None else format(x, spec)


def format_report(r: dict[str, Any]) -> str:
    if not r.get("describe"):
        return f"樣本不夠（{r['n']} 天）：{r['verdict']}"
    d = r["describe"]
    L = [f"價量分布檢驗：{r['n']} 個日盤（{r['first']}～{r['last']}）；略過 {r['skipped']}",
         f"T1 磁吸（POC 碰到 − 鏡像碰到，上下兩層等權）：{_f(r['T1']['stat'])}、t＝{_f(r['T1']['t'], '+.2f')}（要 ≥ {r['z']:.2f}）、合格日 {r['T1'].get('n_events', 0)}、"
         f"前半 {_f(r['T1']['first'])}／後半 {_f(r['T1']['second'])}（要 ≥ {MIN_EFFECT:.2f}）→ {r['verdict']['T1']}",
         f"T2 邊緣反應（VAH／VAL 反彈 − 鏡像反彈）：{_f(r['T2']['stat'])}、t＝{_f(r['T2']['t'], '+.2f')}、碰到的水平 {r['T2'].get('n_events', 0)}、"
         f"前半 {_f(r['T2']['first'])}／後半 {_f(r['T2']['second'])} → {r['verdict']['T2']}",
         f"金絲雀（偷看：用今天的 POC）：T1 統計量 {_f(r['canary']['T1_stat'])}、t＝{_f(r['canary']['T1_t'], '+.1f')}，{'正常（遠大於門檻）' if r['canary']['ok'] else '不夠大（管線可能有問題）'}",
         "T1 依 POC 在開盤價上／下方（實際碰到 − 鏡像碰到）：" + "；".join(f"{k} {v['n']} 天 {_f(v['bump'])}" for k, v in d["t1_by_side"].items()),
         "T1 依距離（POC 碰到 vs 鏡像碰到）：" + "；".join(f"{k} {v['n']} 天 {_f(v['poc_touch'], '.1%')} vs {_f(v['mirror_touch'], '.1%')}" for k, v in d["t1_by_distance"].items()),
         "T2 反彈比例（實際 vs 鏡像）：" + "；".join(f"{t}：{_f(d['t2_rates'][f'{t}_actual']['rejected'], '.1%')}（{d['t2_rates'][f'{t}_actual']['n']}）vs {_f(d['t2_rates'][f'{t}_mirror']['rejected'], '.1%')}（{d['t2_rates'][f'{t}_mirror']['n']}）" for t in ("res", "sup")),
         f"80% 規則（只描述）：開盤在價值區外 {d['rule80']['open_outside_va']} 天 → 連兩個 30 分鐘收在區內 {d['rule80']['entered']} 天 → 走到另一端 {d['rule80']['reached']} 天（{_f(d['rule80']['rate'], '.0%')}）；"
         f"從上方進 {d['rule80']['from_above']['reached']}／{d['rule80']['from_above']['entered']}、從下方進 {d['rule80']['from_below']['reached']}／{d['rule80']['from_below']['entered']}",
         "重建方式的敏感度（T1／T2 統計量 t）：" + "；".join(f"{k} {v['days']} 天 {_f(v['T1']['stat'])}({_f(v['T1']['t'], '+.1f')})／{_f(v['T2']['stat'])}({_f(v['T2']['t'], '+.1f')})" for k, v in r["sensitivity"].items()),
         "每年（T1／T2 統計量）：" + "；".join(f"{y} {v['days']} 天 {_f(v['T1'], '+.2f')}／{_f(v['T2'], '+.2f')}" for y, v in d["by_year"].items())]
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
    from core.weekly_straddle_study import TAIFEX_DIR, TPE, load_tx
    fsp = oh.load_fsp(TAIFEX_DIR)
    monthly = {d.isoformat() for c, (d, _) in fsp.items() if c.isdigit()}
    r = run(load_bars(), sorted(load_tx(TAIFEX_DIR / "tx")), monthly)
    r["generated_at"] = datetime.now(TPE).isoformat(timespec="seconds")
    RESULT_PATH.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(format_report(r))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
