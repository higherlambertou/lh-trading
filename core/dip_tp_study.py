"""「跌 1% 買長天期 Call、漲 1% 買 Put、設停利」的必要條件（update.md 發現 58）：事件日之後 120 個交易日內，標的有沒有比平常更容易走到 ±5%、±10%（停利點的標的等價）。離線分析，不碰交易路徑。

**不看選擇權價格**。判定標準在跑之前寫進 update.md。結果變數：Call 邏輯＝120 日內最高收盤 ≥ 進場 ×(1+m)；Put 邏輯＝最低收盤 ≤ 進場 ×(1−m)；m＝5%、10%。
統計量：命中（0／100）對 [1, 事件日] 的 OLS，係數＝命中率差（個百分點），Newey-West（落後 120）。過關：探索期 t ≥ 2.24、保留期 t ≥ 1.645、兩段命中率差都 ≥ 5 個百分點。
用法：python -m core.dip_tp_study run [--intel-db 路徑]（結果存 data/dip_tp_result.json，只跑一次；--rerun 才重跑）"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from core.dip_study import (CRISIS_YEARS, DEFAULT_INTEL_DB, DIRECTIONS, EXPLORE_END, HOLDOUT_START, THRESH, Z_EXPLORE, Z_HOLDOUT, daily_returns, dummy_excess,
                            event_mask, forward_returns, load_taiex, period_masks)

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "dip_tp_result.json"

H = 120
MOVES = (0.05, 0.10)
MIN_GAP = 5.0                                    # 命中率差至少幾個百分點
N_TESTS = 4
TRAP = 0.05                                      # 「先被套」：命中停利前，標的先反向走了這麼多


def hit_info(close: np.ndarray, direction: str, m: float, h: Optional[int] = None) -> dict[str, np.ndarray]:
    """每個進場日 t：視窗 (t, t+h] 內是否命中（Call：收盤 ≥ 進場×(1+m)；Put：≤ 進場×(1−m)）、第一次命中是第幾天、命中前的最大反向偏離（%）；視窗不完整的 t 是 NaN。"""
    h = H if h is None else h
    n = len(close)
    hit, day, adverse = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    up = direction == "dip"                          # dip＝買 Call：看標的上漲
    for t in range(n - h):
        path = close[t + 1:t + h + 1] / close[t] - 1
        cond = path >= m if up else path <= -m
        if cond.any():
            k = int(np.argmax(cond))
            hit[t], day[t] = 100.0, k + 1
            before = path[:k] if k > 0 else np.array([0.0])
            adverse[t] = (min(before.min(), 0.0) if up else max(before.max(), 0.0)) * 100.0               # 命中前最大的反向偏離（沒有反向走過就是 0）
        else:
            hit[t] = 0.0
    return {"hit": hit, "day": day, "adverse": adverse}


def one_test(dates: Sequence[str], close: np.ndarray, direction: str, m: float) -> dict[str, Any]:
    r = daily_returns(close)
    ev = event_mask(r, direction)
    info = hit_info(close, direction, m)
    masks = period_masks(dates, H, forward_returns(close, H))
    out: dict[str, Any] = {}
    for name, mk in masks.items():
        res = dummy_excess(info["hit"][mk], ev[mk], H)
        base = {"n_days": int(mk.sum()), "n_events": int(ev[mk].sum()), "gap": None, "t": None}
        if res is not None:
            base.update(gap=res[0], t=res[1], rate_event=float(info["hit"][mk & ev].mean()), rate_other=float(info["hit"][mk & ~ev].mean()))
        out[name] = base
    return out


def passes(r: dict[str, Any]) -> bool:
    e, h = r["explore"], r["holdout"]
    if e["t"] is None or h["t"] is None:
        return False
    return bool(e["t"] >= Z_EXPLORE and h["t"] >= Z_HOLDOUT and e["gap"] >= MIN_GAP and h["gap"] >= MIN_GAP)


def describe(dates: Sequence[str], close: np.ndarray, direction: str, m: float) -> dict[str, Any]:
    r = daily_returns(close)
    ev = event_mask(r, direction)
    info = hit_info(close, direction, m)
    fwd = forward_returns(close, H)
    ok = np.isfinite(info["hit"])
    yrs = np.array([int(d[:4]) for d in dates])
    e, o = ok & ev, ok & ~ev
    out: dict[str, Any] = {"n_events": int(e.sum()), "rate_event": float(info["hit"][e].mean()), "rate_other": float(info["hit"][o].mean())}
    trapped = (lambda x: x <= -TRAP * 100) if direction == "dip" else (lambda x: x >= TRAP * 100)         # 命中之前先反向走了 ≥ 5%（Call 邏輯＝先跌；Put 邏輯＝先漲）
    for key, sel in (("event", e), ("other", o)):
        hits = sel & (info["hit"] == 100.0)
        out[f"median_days_{key}"] = float(np.median(info["day"][hits])) if hits.any() else None
        out[f"trapped_{key}"] = float(trapped(info["adverse"][hits]).mean()) if hits.any() else None
        miss = sel & (info["hit"] == 0.0)
        out[f"median_final_when_miss_{key}"] = float(np.median(fwd[miss])) if miss.any() else None
    keep = ok & ~np.isin(yrs, CRISIS_YEARS)
    ex = dummy_excess(info["hit"][keep], ev[keep], H)
    out["ex_crisis_gap"], out["ex_crisis_t"] = (None, None) if ex is None else ex
    full = dummy_excess(info["hit"][ok], ev[ok], H)
    out["full_gap"], out["full_t"] = (None, None) if full is None else full
    return out


def run(dates: Sequence[str], close: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {"n_days": len(dates), "first": dates[0], "last": dates[-1], "h": H, "moves": list(MOVES), "explore_end": EXPLORE_END, "holdout_start": HOLDOUT_START,
                           "z_explore": Z_EXPLORE, "z_holdout": Z_HOLDOUT, "min_gap": MIN_GAP, "tests": {}, "descriptive": {}, "verdict": {}}
    any_pass = False
    for direction in DIRECTIONS:
        for m in MOVES:
            key = f"{direction}/{int(round(m * 100))}%"
            t = one_test(dates, close, direction, m)
            t["pass"] = passes(t)
            out["tests"][key] = t
            out["descriptive"][key] = describe(dates, close, direction, m)
            out["verdict"][key] = "過關" if t["pass"] else "沒過"
            any_pass = any_pass or t["pass"]
    out["necessary_condition_holds"] = bool(any_pass)
    return out


def format_report(r: dict[str, Any]) -> str:
    f = lambda x, spec: "—" if x is None else format(x, spec)   # noqa: E731
    L = [f"跌／漲 {THRESH:.0%} 之後 {r['h']} 個交易日內走到停利點（標的 ±5%／±10%）的機率  {r['first']}～{r['last']}（{r['n_days']} 個交易日）",
         f"過關：探索 t ≥ {r['z_explore']}、保留 t ≥ {r['z_holdout']}、兩段命中率差都 ≥ {r['min_gap']} 個百分點\n",
         f"{'檢定':<12}{'探索 事件':>8}{'命中率 事件/其他':>18}{'差pp':>7}{'t':>6}   {'保留 事件':>8}{'命中率 事件/其他':>18}{'差pp':>7}{'t':>6}  結論"]
    for key, t in r["tests"].items():
        e, h = t["explore"], t["holdout"]
        L.append(f"{key:<12}{e['n_events']:>8}{f(e.get('rate_event'), '.0f') + '/' + f(e.get('rate_other'), '.0f'):>18}{f(e['gap'], '.1f'):>7}{f(e['t'], '.2f'):>6}   "
                 f"{h['n_events']:>8}{f(h.get('rate_event'), '.0f') + '/' + f(h.get('rate_other'), '.0f'):>18}{f(h['gap'], '.1f'):>7}{f(h['t'], '.2f'):>6}  {r['verdict'][key]}")
    L.append("\n描述（全期）：")
    for key, d in r["descriptive"].items():
        L.append(f"  {key}: 事件 {d['n_events']} 日  命中率 {d['rate_event']:.0f}% vs {d['rate_other']:.0f}%（全期差 {f(d['full_gap'], '.1f')}、t {f(d['full_t'], '.1f')}；拿掉危機年差 {f(d['ex_crisis_gap'], '.1f')}、t {f(d['ex_crisis_t'], '.1f')}）  "
                 f"命中者中位數 {f(d['median_days_event'], '.0f')} vs {f(d['median_days_other'], '.0f')} 天  命中前先被反向套 5%：{f(None if d['trapped_event'] is None else d['trapped_event'] * 100, '.0f')}% vs {f(None if d['trapped_other'] is None else d['trapped_other'] * 100, '.0f')}%  "
                 f"沒命中者視窗結束報酬中位數 {f(d['median_final_when_miss_event'], '.1f')}% vs {f(d['median_final_when_miss_other'], '.1f')}%")
    L.append(f"\n結論：{'停利版的必要條件成立（至少一個過關）→ 可以討論下一步（遠月選擇權的真實報價與損耗）' if r['necessary_condition_holds'] else '四個都沒過關 → 停：事件日之後走到停利點的機率沒有比平常高（停利版也不成立）'}")
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
