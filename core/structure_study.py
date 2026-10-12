"""日內結構檢驗：缺口（開盤跳空之後，當天是續行還是回補）與前日高低（突破之後，是續行還是失敗）。

只讀本機的 1 分 K（bars_1m，日盤），不連券商、不碰交易路徑。這是「檢驗」不是「策略」：先問現象存不存在，
存在才值得寫成策略。判定標準在看結果之前就固定了（update.md 發現 27）：共 8 個檢定，|t| ≥ 2.73（Bonferroni）才宣稱有現象，|t| ≥ 2 只算線索。

做法（全部以 R 為單位，R＝前 20 個時段（高－低）的平均，只用當天以前的資料，所以不同波動時期可以放在一起比）：
  缺口：g＝今日開盤 − 昨日收盤（含夜盤的跳空）。Y＝sign(g)×(今日收盤 − 今日開盤 − 全樣本平均日內漲跌)/R，
        正＝續行、負＝回補。先扣掉整體漲勢，不然「多數是上跳空、又整體在漲」會造成假的續行。
        檢定：全部＋|g| 的三等分（小／中／大）＝4 個；H0：E[Y]＝0。
  前日高低：事件＝今日第一根「收盤」在昨日高點之上（向上）／昨日低點之下（向下）的 K 棒；進場＝那根收盤。
        結果＝順著突破方向 k 分鐘後的移動 ÷ R，減去「其他天、同一個時段（事件前後 10 分鐘）所有時間點」的同樣移動的平均（超額；
        控制時段的波動與整體漲勢）。對照不用同一天的其他時間點：事件之前的時間點，它的後續視窗會包含突破本身（突破是被挑出來的），
        對照會偏向正值，讓「突破後失敗」看起來比實際更顯著。檢定：向上／向下 × 30／60 分鐘＝4 個；H0：超額平均＝0。
        收盤前 5 分鐘內才發生的事件不算（之後沒有資料可看）；k 分鐘後超過收盤就用收盤價。

獨立驗證（confirm）：update.md 發現 27 ④。「突破後失敗」是從 2025-09-03 之後的資料看到的方向，所以拿更早、沒看過的資料（券商 1 分 K 最早到 2020-03-02）
來驗證，**只做 1 個事先固定的檢定**：向上與向下突破合併、30 分鐘後的超額移動，H1：平均 < 0（單尾 5%：t ≤ −1.645）；
通過之後還要平均超額 ≤ −10 點（成本 4 點的 2.5 倍）才談得上策略。

用法：
    python -m core.structure_study                 # 缺口＋前日高低（資料庫裡全部的日子；用 --start/--end 指定範圍）
    python -m core.structure_study gap --json out.json
    python -m core.structure_study confirm --start 2020-03-02 --end 2025-09-02     # 獨立驗證（單一單尾檢定）
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional

import numpy as np

from core.backtest import SCALE_DAYS, Days, load_days, ref_ranges, t_stat

MIN_BARS = 280              # 少於這麼多根的日子（提早收盤、資料殘缺）不納入
TAIL_BARS = 5               # 前日高低：事件發生在收盤前這麼多根以內就不算
NULL_WINDOW = 10            # 前日高低：對照取「其他天」同一個時段，事件前後這麼多分鐘內的所有時間點（窄，才對得上時段的波動與漲勢）
HORIZONS = (30, 60)
N_TESTS = 8
Z_CLAIM = NormalDist().inv_cdf(1 - 0.05 / N_TESTS / 2)       # ≈ 2.73：Bonferroni 校正後雙尾 5%
Z_HINT = 2.0
Z_ONE_SIDED = NormalDist().inv_cdf(0.95)         # 獨立驗證：單一假設、單尾 5% → 1.645
CONFIRM_MIN_POINTS = 10.0                        # 獨立驗證通過後，平均超額要 ≤ −10 點（成本 4 點的 2.5 倍）才談策略


def verdict(t: float) -> str:
    return "過關" if abs(t) >= Z_CLAIM else ("線索" if abs(t) >= Z_HINT else "沒有")


def _sessions(days: Days, scale_days: int = SCALE_DAYS) -> list[dict[str, Any]]:
    refs = ref_ranges(days, scale_days)
    out = []
    for i, (d, bars) in enumerate(days.items()):
        out.append({"date": d, "ref": refs[i], "ok": len(bars) >= MIN_BARS, "bars": bars,
                    "open": bars[0][1], "close": bars[-1][4], "high": max(b[2] for b in bars), "low": min(b[3] for b in bars)})
    return out


def _usable(ss: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(今天, 昨天) 配對：今天有參考振幅、今天和昨天都是完整的一天。"""
    return [(ss[i], ss[i - 1]) for i in range(1, len(ss)) if ss[i]["ref"] and ss[i]["ok"] and ss[i - 1]["ok"]]     # ref 為 None 或 0 都不能用


def _stats(x: list[float], med_r: float) -> dict[str, float]:
    a = np.array(x, float)
    n = len(a)
    sd = float(np.std(a, ddof=1)) if n > 1 else 0.0
    t = t_stat(a)
    return {"n": n, "mean": float(a.mean()) if n else 0.0, "sd": sd, "t": t, "points": (float(a.mean()) if n else 0.0) * med_r,
            "mde_points": 2.8 * sd / math.sqrt(n) * med_r if n else 0.0, "verdict": verdict(t)}


# ── 缺口 ──────────────────────────────────────────────────────────

def gap_study(days: Days, scale_days: int = SCALE_DAYS) -> dict[str, Any]:
    pairs = _usable(_sessions(days, scale_days))
    rows = []
    for s, p in pairs:
        g = s["open"] - p["close"]
        if g == 0:
            continue                                        # 沒有缺口，沒有方向可言
        up = g > 0
        rows.append({"date": s["date"], "g": g, "r": s["ref"], "move": (s["close"] - s["open"]) / s["ref"],
                     "filled": (s["low"] <= p["close"]) if up else (s["high"] >= p["close"])})
    if len(rows) < 12:
        raise ValueError(f"缺口檢驗：可用的天數只有 {len(rows)} 天，太少")
    med_r = float(np.median([x["r"] for x in rows]))
    drift = float(np.mean([x["move"] for x in rows]))
    for x in rows:
        x["G"] = x["g"] / x["r"]
        x["y"] = math.copysign(1.0, x["g"]) * (x["move"] - drift)
    order = sorted(range(len(rows)), key=lambda i: abs(rows[i]["G"]))
    names = ("small", "medium", "large")
    groups = {nm: [rows[i] for i in idx] for nm, idx in zip(names, np.array_split(np.array(order), 3))}
    tests: dict[str, Any] = {}
    for name, grp in (("all", rows), *groups.items()):
        st = _stats([x["y"] for x in grp], med_r)
        st["fill_rate"] = float(np.mean([x["filled"] for x in grp]))
        st["up_share"] = float(np.mean([x["g"] > 0 for x in grp]))
        st["abs_G"] = [float(min(abs(x["G"]) for x in grp)), float(max(abs(x["G"]) for x in grp))]
        tests[name] = st
    return {"n": len(rows), "median_R": med_r, "drift_R": drift, "tests": tests}


# ── 前日高低 ──────────────────────────────────────────────────────

def _forward(closes: np.ndarray, r: float, k: int) -> np.ndarray:
    """每個時間點 j 的「k 分鐘後的移動 ÷ R」（超過收盤就用收盤價）；收盤前 TAIL_BARS 根以內不算（NaN）。"""
    last = len(closes) - 1
    j = np.arange(last + 1)
    out = (closes[np.minimum(j + k, last)] - closes) / r
    out[max(last - TAIL_BARS, -1) + 1:] = np.nan
    return out


def level_study(days: Days, horizons: tuple[int, ...] = HORIZONS, window: int = NULL_WINDOW,
                scale_days: int = SCALE_DAYS) -> dict[str, Any]:
    pairs = _usable(_sessions(days, scale_days))
    if len(pairs) < 12:
        raise ValueError(f"前日高低檢驗：可用的天數只有 {len(pairs)} 天，太少")
    med_r = float(np.median([s["ref"] for s, _ in pairs]))
    width = max(len(s["bars"]) for s, _ in pairs)
    closes = [np.array([b[4] for b in s["bars"]], float) for s, _ in pairs]
    fwd = {}                                                    # k → (天數 × 時間點) 的矩陣，不足的補 NaN
    for k in horizons:
        m = np.full((len(pairs), width), np.nan)
        for i, (s, _) in enumerate(pairs):
            m[i, :len(closes[i])] = _forward(closes[i], s["ref"], k)
        fwd[k] = m
    ev: dict[tuple[int, int], list[tuple[float, float]]] = {(d, k): [] for d in (1, -1) for k in horizons}
    n_days = {1: 0, -1: 0}
    for i, (s, p) in enumerate(pairs):
        c = closes[i]
        last = len(c) - 1
        for dirn, hit in ((1, c > p["high"]), (-1, c < p["low"])):
            idx = np.flatnonzero(hit)
            if not len(idx) or idx[0] > last - TAIL_BARS:
                continue
            b = int(idx[0])
            n_days[dirn] += 1
            lo, hi = max(0, b - window), min(last - TAIL_BARS, b + window)
            for k in horizons:
                x = dirn * float(fwd[k][i, b])
                win = fwd[k][:, lo:hi + 1]
                sums, cnts = np.nansum(win, axis=1), np.sum(~np.isnan(win), axis=1)
                others = cnts.sum() - cnts[i]
                if others <= 0:
                    continue
                ev[(dirn, k)].append((x, dirn * float((sums.sum() - sums[i]) / others)))
    tests: dict[str, Any] = {}
    for (dirn, k), pairs_xn in ev.items():
        name = f"{'up' if dirn > 0 else 'down'}_{k}"
        if len(pairs_xn) < 2:
            tests[name] = {"n": len(pairs_xn), "mean": 0.0, "sd": 0.0, "t": 0.0, "points": 0.0, "mde_points": 0.0,
                           "verdict": "沒有", "mean_raw": 0.0, "mean_null": 0.0}
            continue
        st = _stats([x - nl for x, nl in pairs_xn], med_r)
        st["mean_raw"] = float(np.mean([x for x, _ in pairs_xn]))
        st["mean_null"] = float(np.mean([nl for _, nl in pairs_xn]))
        tests[name] = st
    pooled = {str(k): _stats([x - nl for d in (1, -1) for x, nl in ev[(d, k)]], med_r) for k in horizons if sum(len(ev[(d, k)]) for d in (1, -1)) >= 2}
    return {"days": len(pairs), "days_with_up_break": n_days[1], "days_with_down_break": n_days[-1], "median_R": med_r, "tests": tests, "pooled": pooled}


def confirm(days: Days, horizon: int = 30, scale_days: int = SCALE_DAYS) -> dict[str, Any]:
    """獨立驗證的單一檢定：向上與向下突破合併、horizon 分鐘後的超額移動，H1：平均 < 0（單尾）。"""
    v = level_study(days, horizons=(horizon,), scale_days=scale_days)
    if str(horizon) not in v["pooled"]:
        raise ValueError("獨立驗證：事件太少，算不出來")
    st = dict(v["pooled"][str(horizon)])
    st["decision"] = "通過" if st["t"] <= -Z_ONE_SIDED else ("相反方向顯著" if st["t"] >= Z_ONE_SIDED else "沒有")
    st["big_enough"] = bool(st["points"] <= -CONFIRM_MIN_POINTS)
    st.update(horizon=horizon, days=v["days"], up_events=v["tests"][f"up_{horizon}"]["n"], down_events=v["tests"][f"down_{horizon}"]["n"])
    return st


def format_confirm(c: dict[str, Any], label: str = "") -> str:
    return (f"  {label}合併（向上 {c['up_events']}＋向下 {c['down_events']} 個事件）{c['horizon']} 分鐘後的超額移動：平均 {c['mean']:+.3f}R（{c['points']:+.1f} 點），"
            f"t={c['t']:+.2f}，單尾 5% 門檻 ±{Z_ONE_SIDED:.3f} → [{c['decision']}]；"
            f"{'失敗幅度夠大（≥ ' + str(int(CONFIRM_MIN_POINTS)) + ' 點）' if c['big_enough'] else '幅度不到 ' + str(int(CONFIRM_MIN_POINTS)) + ' 點（扣掉成本後沒有空間）'}")


# ── 報告 ──────────────────────────────────────────────────────────

def _line(name: str, st: dict[str, float]) -> str:
    return (f"  {name:<14}n={st['n']:<4} 平均 {st['mean']:+.3f}R（{st['points']:+.1f} 點）  t={st['t']:+.2f}  "
            f"[{st['verdict']}]  偵測力：看得出 ≥ {st['mde_points']:.0f} 點")


def format_gap(g: dict[str, Any]) -> str:
    L = [f"══ 缺口：開盤對昨日收盤的跳空之後，當天續行還是回補（n={g['n']}，R 中位 {g['median_R']:.0f} 點，已扣除整體日內漲跌 {g['drift_R']:+.3f}R）══",
         "  Y 的符號：正＝續行（跳空方向繼續走）、負＝回補"]
    labels = {"all": "全部", "small": "小缺口", "medium": "中缺口", "large": "大缺口"}
    for name, st in g["tests"].items():
        lo, hi = st["abs_G"]
        L.append(_line(labels[name], st) + f"  |g|/R {lo:.2f}~{hi:.2f}，當天回補到昨收的比例 {st['fill_rate']:.0%}，上跳空占 {st['up_share']:.0%}")
    return "\n".join(L)


def format_levels(v: dict[str, Any]) -> str:
    L = [f"══ 前日高低：收盤突破之後（{v['days']} 天；向上突破 {v['days_with_up_break']} 天、向下 {v['days_with_down_break']} 天；R 中位 {v['median_R']:.0f} 點）══",
         "  結果＝順著突破方向的移動 − 對照（其他天、同一時段、同方向的平均）；正＝續行、負＝失敗反轉"]
    for name, st in v["tests"].items():
        d, k = name.split("_")
        L.append(_line(f"{'向上' if d == 'up' else '向下'}突破 {k}分", st) + f"  （原始 {st['mean_raw']:+.3f}R、對照 {st['mean_null']:+.3f}R）")
    for k, st in v.get("pooled", {}).items():
        L.append(f"  （描述，不計入 8 個檢定）向上＋向下合併 {k}分：n={st['n']} 平均 {st['mean']:+.3f}R（{st['points']:+.1f} 點）t={st['t']:+.2f}")
    return "\n".join(L)


def summary(reports: dict[str, dict[str, Any]]) -> str:
    tests = [(f"{part}:{n}", st) for part, rep in reports.items() for n, st in rep["tests"].items()]
    passed = [n for n, st in tests if st["verdict"] == "過關"]
    hints = [n for n, st in tests if st["verdict"] == "線索"]
    head = f"判定（{N_TESTS} 個檢定；過關 |t| ≥ {Z_CLAIM:.2f}、線索 |t| ≥ {Z_HINT:g}）：實際跑了 {len(tests)} 個"
    if len(tests) != N_TESTS:
        head += f"（和事先固定的 {N_TESTS} 個不同，Bonferroni 門檻要重算）"
    return head + f"；過關 {len(passed)} 個 {passed or ''}；線索 {len(hints)} 個 {hints or ''}。" + \
        ("" if passed else "沒有任何檢定過關＝這份資料上看不出可交易的現象。")


# ── CLI ───────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.structure_study", description="日內結構檢驗：缺口、前日高低（只讀本機資料庫）")
    ap.add_argument("what", nargs="?", choices=("gap", "levels", "all", "confirm"), default="all")
    ap.add_argument("--db", help="market_state.db 路徑（預設 data/market_state.db）")
    ap.add_argument("--code", help="合約代碼（預設 TXF）")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD（含）")
    ap.add_argument("--end", help="結束日期 YYYY-MM-DD（含）")
    ap.add_argument("--horizon", type=int, default=30, help="confirm：看幾分鐘後的移動（事先固定是 30）")
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    args = ap.parse_args(argv)
    try:
        if args.what == "confirm":
            if not (args.start and args.end):
                raise ValueError("confirm 需要 --start 和 --end（獨立驗證要明確指定那一段沒看過的資料）")
        days = load_days(args.db, "bars", args.code, "day", args.start, args.end)
        if not days:
            raise ValueError("沒有 K 棒資料")
        reports: dict[str, dict[str, Any]] = {}
        if args.what == "confirm":
            c = confirm(days, args.horizon)
            print(f"══ 獨立驗證：前日高低「突破後失敗」（{args.start} → {args.end}，{c['days']} 個可用的日子，單一事先固定的檢定）══")
            print(format_confirm(c))
            if args.json:
                Path(args.json).write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
            return 0
        if args.what in ("gap", "all"):
            reports["gap"] = gap_study(days)
            print(format_gap(reports["gap"]))
        if args.what in ("levels", "all"):
            reports["levels"] = level_study(days)
            print(format_levels(reports["levels"]))
    except (ValueError, OSError) as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 2
    print("\n" + summary(reports))
    if args.json:
        Path(args.json).write_text(json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完整結果已寫入 {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
