"""機率範圍帶的校準（update.md 發現 48）：「T 分鐘後（或到收盤），價格有 50%／80%／95% 的機率落在現價 ± d 點之內」的實測表。
離線、只讀本機資料，不碰交易路徑；只回答「會走多遠」，不回答「往哪個方向」。

做法：不假設分配、不做模擬——直接量歷史。σ 與 core/stop_calibration.py 完全相同（進場前最近 30 個 1 分鐘收盤價變動的樣本標準差，至少 25 個有效），
每個進場點 t（每個時段從第 30 格起每 5 格一個）、每個持有時間 T：
  落點 z_term＝|收盤(t+T) − 收盤(t)| ÷ (σ·√T)       ← 時間到時價格離現價多遠
  期間 z_path＝max(期間最高 − 收盤(t)，收盤(t) − 期間最低) ÷ (σ·√T)   ← 這段時間內任何時候離現價最遠多遠（停損停利碰不碰到看這個）
把全部進場點的 z 合起來取 50%／80%／95% 分位數 q，範圍＝現價 ± q·σ√T。落點用「絕對值」的分位數（兩邊對稱），避免把樣本期間的漲勢當成往上的偏向。
「到收盤」：剩餘分鐘 R＝收盤前還有幾分鐘，σ√R，依 R 分成 4 組（15~30、31~60、61~120、121~270），每組各一張表（日盤才有）。
常態公式（落點 ±z·σ√T，80% 的 z＝1.2816）當對照只列在報告裡：σ 本身是用 30 筆估的、真實價格有肥尾，所以實測會比公式寬一點。

驗證（事先登記在 update.md 發現 48，只跑一次，不依結果回頭改）：用 2025-01-01 以前的日盤算表，量 2025-01-01 以後的實際涵蓋率；
每格（落點／期間 × 4 個持有時間與 4 組到收盤 × 3 個層級）的涵蓋率與名目值差 ≤ 3 個百分點、或差距小於 2 個日級標準誤才算通過。沒通過的格子在畫面上標「不穩」，不拿掉。
最後寫進 JSON 的是用全部資料算的表（UI 讀的就是它）。夜盤只有幾十晚（TMF 真實成交），只算表、不做驗證、一律標「樣本少」。

用法
  python -m core.range_calibration run [--write]     # 算表＋驗證、印報告；--write 寫進 core/range_calibration.json"""
from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional, Sequence

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from core.stop_calibration import FIRST, MIN_VALID, STEP, WINDOW, day_sessions, entry_slots, flow_sessions, trailing_sigma

LEVELS = (0.5, 0.8, 0.95)
HORIZONS = (5, 15, 30, 60)
BUCKETS = ((15, 30), (31, 60), (61, 120), (121, 270))       # 到收盤：剩餘分鐘的分組（含頭尾）
MIN_REMAIN = BUCKETS[0][0]
MAX_HALF_REL = 0.10                                          # 前後半的分位數差超過這個比例就標「不穩」
TOLERANCE = 0.03                                             # 驗證：涵蓋率與名目值差（個百分點）
SE_MULT = 2.0                                                # …或小於這麼多個日級標準誤
HOLDOUT_SPLIT = "2025-01-01"
MIN_N = 300                                                  # 樣本少於這個標「樣本少」
TOD_EDGES = (30, 90, 150, 210, 270)                          # 描述用：進場格在時段內的位置分組
OUT_PATH = Path(__file__).resolve().parent / "range_calibration.json"
TYPES = ("term", "path")

Session = tuple[str, np.ndarray, np.ndarray, np.ndarray]    # (日期, hi, lo, cl)；陣列等長，缺的格是 NaN


def bucket_key(b: tuple[int, int]) -> str:
    return f"c{b[0]}_{b[1]}"


BUCKET_KEYS = tuple(bucket_key(b) for b in BUCKETS)


def bucket_of(remaining: float) -> Optional[str]:
    """剩餘分鐘 → 組別代號；不到 MIN_REMAIN 或超過最大一組回 None。"""
    for b in BUCKETS:
        if b[0] <= remaining <= b[1]:
            return bucket_key(b)
    return None


def gauss_q(level: float) -> float:
    """常態隨機漫步、落點兩邊對稱：P(|Z| ≤ z) = level 的 z（對照用）。"""
    return NormalDist().inv_cdf((1.0 + level) / 2.0)


# ── 每個進場點的 z ───────────────────────────────────────────────

def _empty() -> dict[str, np.ndarray]:
    return {"slot": np.zeros(0, dtype=np.int64), "term": np.zeros(0), "path": np.zeros(0)}


def _z(c: np.ndarray, end: np.ndarray, up: np.ndarray, dn: np.ndarray, unit: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    with np.errstate(invalid="ignore", divide="ignore"):
        term = np.abs(end - c) / unit
        path = np.fmax(up, dn) / unit                    # fmax：一邊缺就用另一邊、兩邊都缺才是 NaN
    return term, path


def session_z(hi: np.ndarray, lo: np.ndarray, cl: np.ndarray, horizons: Sequence[int] = HORIZONS, buckets: bool = True) -> dict[str, dict[str, np.ndarray]]:
    """一個時段的所有進場點：{持有時間或組別: {slot, term, path}}。σ 只用到進場那一格為止的資料；z 用到之後的價格（這就是要量的東西）。
    進場點、σ 的有效條件與 core/stop_calibration.session_counts 相同；落點需要 t+T 的收盤有值，期間需要區間內至少一根有高低。"""
    n = len(cl)
    sig_all = trailing_sigma(cl)
    out: dict[str, dict[str, np.ndarray]] = {}
    for T in horizons:
        ts = entry_slots(n, T)
        if len(ts) == 0:
            out[str(T)] = _empty()
            continue
        sig, c = sig_all[ts], cl[ts]
        ok = np.isfinite(sig) & (sig > 0) & np.isfinite(c)
        ts, sig, c = ts[ok], sig[ok], c[ok]
        if len(ts) == 0:
            out[str(T)] = _empty()
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)                      # 整段都沒成交的窗口是全 NaN
            up = np.nanmax(sliding_window_view(hi, T)[ts + 1], axis=1) - c
            dn = c - np.nanmin(sliding_window_view(lo, T)[ts + 1], axis=1)
        term, path = _z(c, cl[ts + T], up, dn, sig * math.sqrt(T))
        out[str(T)] = {"slot": ts.astype(np.int64), "term": term, "path": path}
    if buckets:
        ts = np.arange(FIRST, n - MIN_REMAIN, STEP)                              # 剩餘 n−1−t ≥ MIN_REMAIN
        ts = ts[(n - 1 - ts) <= BUCKETS[-1][1]]
        sig, c = sig_all[ts], cl[ts]
        ok = np.isfinite(sig) & (sig > 0) & np.isfinite(c)
        ts, sig, c = ts[ok], sig[ok], c[ok]
        rem = (n - 1 - ts).astype(float)
        sufmax = np.maximum.accumulate(np.where(np.isfinite(hi), hi, -np.inf)[::-1])[::-1]       # sufmax[i]＝max(hi[i:])
        sufmin = np.minimum.accumulate(np.where(np.isfinite(lo), lo, np.inf)[::-1])[::-1]
        mx, mn = sufmax[ts + 1], sufmin[ts + 1]
        up = np.where(np.isfinite(mx), mx - c, np.nan)
        dn = np.where(np.isfinite(mn), c - mn, np.nan)
        term, path = _z(c, np.full(len(ts), cl[n - 1]), up, dn, sig * np.sqrt(rem))
        for b in BUCKETS:
            sel = (rem >= b[0]) & (rem <= b[1])
            out[bucket_key(b)] = {"slot": ts[sel].astype(np.int64), "term": term[sel], "path": path[sel]}
    return out


# ── 分位數表 ─────────────────────────────────────────────────────

def _pooled(per_day: Sequence[dict[str, np.ndarray]], kind: str) -> np.ndarray:
    parts = [d[kind][np.isfinite(d[kind])] for d in per_day if len(d[kind])]
    return np.concatenate(parts) if parts else np.zeros(0)


def _quantiles(z: np.ndarray) -> list[Optional[float]]:
    return [float(np.quantile(z, L)) for L in LEVELS] if len(z) else [None] * len(LEVELS)


def _rel_gap(a: Optional[float], b: Optional[float], whole: Optional[float]) -> bool:
    return bool(a is None or b is None or not whole or abs(a - b) / whole > MAX_HALF_REL)


def calibrate(sessions: Sequence[Session], horizons: Sequence[int] = HORIZONS, buckets: bool = True) -> dict[str, Any]:
    """sessions：依日期排序。回傳 {key: {n, days, term, path, gauss, unstable_term, unstable_path, few}}（term／path 各三個分位數，對應 LEVELS）。"""
    per = [session_z(hi, lo, cl, horizons, buckets) for _, hi, lo, cl in sessions]
    keys = [str(T) for T in horizons] + (list(BUCKET_KEYS) if buckets else [])
    half = (len(per) + 1) // 2
    out: dict[str, Any] = {}
    for k in keys:
        days = [p[k] for p in per]
        cell: dict[str, Any] = {"days": int(sum(1 for d in days if len(d["term"]))), "gauss": [gauss_q(L) for L in LEVELS]}
        for kind in TYPES:
            z = _pooled(days, kind)
            q, q1, q2 = _quantiles(z), _quantiles(_pooled(days[:half], kind)), _quantiles(_pooled(days[half:], kind))
            cell[kind] = q
            cell[f"n_{kind}"] = int(len(z))
            cell[f"unstable_{kind}"] = [_rel_gap(a, b, w) for a, b, w in zip(q1, q2, q)]
        cell["n"] = cell["n_term"]
        cell["few"] = bool(cell["n"] < MIN_N)
        out[k] = cell
    return out


# ── 樣本外涵蓋率 ─────────────────────────────────────────────────

def coverage(per_day: Sequence[dict[str, np.ndarray]], kind: str, qs: Sequence[Optional[float]]) -> list[dict[str, Any]]:
    """每個層級：實際有多少比例的進場點 z ≤ q（涵蓋率）、日級標準誤（每天的涵蓋率的標準差 ÷ √天數）、n。q 是 None 的層級回 None 值。"""
    out = []
    for L, q in zip(LEVELS, qs):
        if q is None:
            out.append({"level": L, "coverage": None, "se": None, "n": 0, "days": 0})
            continue
        hit = tot = 0
        day_means = []
        for d in per_day:
            z = d[kind][np.isfinite(d[kind])]
            if not len(z):
                continue
            h = int((z <= q).sum())
            hit, tot = hit + h, tot + len(z)
            day_means.append(h / len(z))
        se = float(np.std(day_means, ddof=1) / math.sqrt(len(day_means))) if len(day_means) > 1 else None
        out.append({"level": L, "coverage": hit / tot if tot else None, "se": se, "n": tot, "days": len(day_means)})
    return out


def cell_ok(cov: Optional[float], se: Optional[float], nominal: float) -> bool:
    """差 ≤ TOLERANCE，或差 < SE_MULT 個日級標準誤。沒有涵蓋率（沒樣本）一律不通過。"""
    if cov is None:
        return False
    gap = abs(cov - nominal)
    return bool(gap <= TOLERANCE or (se is not None and gap < SE_MULT * se))


def holdout_check(sessions: Sequence[Session], split: str = HOLDOUT_SPLIT, horizons: Sequence[int] = HORIZONS) -> dict[str, Any]:
    """split 以前的時段算表、split 以後的時段量涵蓋率。回傳每格的涵蓋率、通過與否、常態公式的涵蓋率（只有落點）與時段內位置的涵蓋率（描述）。"""
    train = [s for s in sessions if s[0] < split]
    test = [s for s in sessions if s[0] >= split]
    tab = calibrate(train, horizons)
    per_test = [session_z(hi, lo, cl, horizons) for _, hi, lo, cl in test]
    cells = []
    for k, cell in tab.items():
        days = [p[k] for p in per_test]
        for kind in TYPES:
            cov = coverage(days, kind, cell[kind])
            gauss = coverage(days, kind, cell["gauss"]) if kind == "term" else [None] * len(LEVELS)
            for L, c, g in zip(LEVELS, cov, gauss):
                cells.append({"key": k, "type": kind, "level": L, "coverage": c["coverage"], "se": c["se"], "n": c["n"],
                              "ok": cell_ok(c["coverage"], c["se"], L), "gauss_coverage": None if g is None else g["coverage"]})
    tod = []                                                                                 # 描述：80% 落點在時段的不同位置涵蓋率一樣嗎
    for k in [str(T) for T in horizons]:
        q80 = tab[k]["term"][LEVELS.index(0.8)]
        row = {"key": k, "buckets": []}
        for a, b in zip(TOD_EDGES, TOD_EDGES[1:]):
            hit = tot = 0
            for p in per_test:
                d = p[k]
                sel = (d["slot"] >= a) & (d["slot"] < b) & np.isfinite(d["term"])
                tot += int(sel.sum())
                hit += int((d["term"][sel] <= q80).sum()) if q80 is not None else 0
            row["buckets"].append({"from": a, "to": b, "n": tot, "coverage": hit / tot if tot else None})
        tod.append(row)
    return {"split": split, "train_sessions": len(train), "test_sessions": len(test), "tolerance": TOLERANCE, "se_mult": SE_MULT,
            "ok": int(sum(c["ok"] for c in cells)), "total": len(cells), "cells": cells, "tod": tod}


# ── 組出 JSON、報告 ──────────────────────────────────────────────

def attach_flags(table: dict[str, Any], holdout: dict[str, Any]) -> None:
    """把驗證結果放回每格（holdout_ok_term／holdout_ok_path：三個層級各一個布林），UI 用來標「不穩」。"""
    for c in holdout["cells"]:
        cell = table.get(c["key"])
        if cell is not None:
            cell.setdefault(f"holdout_ok_{c['type']}", [None] * len(LEVELS))[LEVELS.index(c["level"])] = bool(c["ok"])


def build(store: Any) -> dict[str, Any]:
    day, night = day_sessions(store), flow_sessions(store, night=True)
    day_tab = calibrate(day)
    hold = holdout_check(day)
    attach_flags(day_tab, hold)
    return {"version": 1, "generated": datetime.now().strftime("%Y-%m-%d"), "levels": list(LEVELS), "horizons": list(HORIZONS), "buckets": [list(b) for b in BUCKETS],
            "sigma": {"window": WINDOW, "min_valid": MIN_VALID, "unit": "點／√分鐘", "entry_first": FIRST, "entry_step": STEP},
            "max_half_rel": MAX_HALF_REL, "min_n": MIN_N,
            "day": {"source": "TXF bars_1m（日盤 1 分 K）", "sessions": len(day), "from": day[0][0] if day else None, "to": day[-1][0] if day else None, "table": day_tab},
            "night": {"source": "TMF flow_1m（夜盤真實成交 1 分 K）", "sessions": len(night), "from": night[0][0] if night else None, "to": night[-1][0] if night else None,
                      "table": calibrate(night, buckets=False), "few": True},
            "holdout": hold}


def _f(x: Optional[float], nd: int = 2) -> str:
    return "  —  " if x is None else f"{x:{5 + nd - 2}.{nd}f}"


def format_report(res: dict[str, Any]) -> str:
    lines = [f"日盤：{res['day']['sessions']} 個時段（{res['day']['from']} ~ {res['day']['to']}）；夜盤：{res['night']['sessions']} 晚（{res['night']['from']} ~ {res['night']['to']}）",
             f"層級：{'／'.join(f'{int(L * 100)}%' for L in LEVELS)}；表裡的數字是倍數 q，範圍＝現價 ± q·σ√T"]
    for name, sec in (("日盤（TXF 1 分 K）", res["day"]), ("夜盤（TMF 真實成交，樣本少）", res["night"])):
        lines.append(f"\n═══ {name} ═══")
        for k, c in sec["table"].items():
            flag = "（樣本少）" if c["few"] else ""
            lines.append(f"{'持有 ' + k + ' 分鐘' if k.isdigit() else '到收盤 ' + k[1:].replace('_', '~') + ' 分鐘'}：進場點 {c['n']:,}、{c['days']} 個時段{flag}")
            lines.append("    落點 q    " + "  ".join(_f(x) for x in c["term"]) + "   常態公式 " + "  ".join(_f(x) for x in c["gauss"])
                         + "   不穩 " + "".join("×" if u else "·" for u in c["unstable_term"]))
            lines.append("    期間 q    " + "  ".join(_f(x) for x in c["path"]) + "   不穩 " + "".join("×" if u else "·" for u in c["unstable_path"]))
    h = res["holdout"]
    lines.append(f"\n═══ 樣本外驗證（{h['split']} 以前算表：{h['train_sessions']} 個時段；以後量涵蓋率：{h['test_sessions']} 個時段）═══")
    lines.append(f"通過 {h['ok']}／{h['total']} 格（差 ≤ {h['tolerance'] * 100:.0f} 個百分點，或 < {h['se_mult']:.0f} 個日級標準誤）")
    for kind, label in (("term", "落點"), ("path", "期間")):
        lines.append(f"  {label}：實際涵蓋率（%）[名目 50｜80｜95]；  ✓ 通過 ✗ 沒過" + ("；括號是常態公式的涵蓋率" if kind == "term" else ""))
        for k in list(res["day"]["table"]):
            row = [c for c in h["cells"] if c["key"] == k and c["type"] == kind]
            cells = []
            for c in row:
                s = f"{_f(None if c['coverage'] is None else c['coverage'] * 100, 1)}{'✓' if c['ok'] else '✗'}"
                if kind == "term" and c["gauss_coverage"] is not None:
                    s += f"({c['gauss_coverage'] * 100:4.1f})"
                cells.append(s)
            lines.append(f"    {k:<9}" + "   ".join(cells))
    lines.append("  描述：80% 落點的涵蓋率，依進場格在時段內的位置（第幾分鐘）")
    for r in h["tod"]:
        lines.append(f"    T={r['key']:<3}" + "  ".join(f"{b['from']}~{b['to']}:{_f(None if b['coverage'] is None else b['coverage'] * 100, 1)}" for b in r["buckets"]))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="python -m core.range_calibration", description="機率範圍帶的校準（update.md 發現 48）")
    ap.add_argument("cmd", choices=("run",))
    ap.add_argument("--write", action="store_true", help="把結果寫進 core/range_calibration.json")
    a = ap.parse_args(argv)
    from core.market_store import MarketStore
    res = build(MarketStore())
    print(format_report(res))
    if a.write:
        OUT_PATH.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        print(f"\n已寫入 {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
