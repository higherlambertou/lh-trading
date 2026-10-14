"""停損停利建議的校準（update.md 發現 33）：「被雜訊掃到的機率」與「沒有技巧時的勝率」的實測表。離線、只讀本機資料，不碰交易路徑。

兩個問題
  1. 價格在持有 T 分鐘內，碰到不利方向 d＝k·σ·√T 的機率有多大？（σ＝進場前最近 30 個 1 分鐘收盤價變動的標準差）
     常態隨機漫步的公式是 2·(1−Φ(k))；真實的日內波動有肥尾、波動聚集、買賣價差跳動，所以用實測。
  2. 進場沒有任何資訊（隨機方向、隨機時間）時，停利 k_tp·σ√T、停損 k_sl·σ√T，先碰到停利的比例（沒有技巧的勝率）是多少？
     這是「勝率」的基準：你的勝率要比它高，才叫有優勢（公式值 k_sl÷(k_tp＋k_sl)，同一根 K 棒兩邊都碰到算停損先到）。
定義全部固定在這裡（update.md 發現 33 事先登記，只跑一次，不依結果回頭改）。

用法
  python -m core.stop_calibration run [--write]    # 算表、印報告；--write 寫進 core/stop_calibration.json（UI 讀的就是它）
  python -m core.stop_calibration check-source     # TMF 真實成交與 TXF 1 分 K 的 σ 一致嗎（執行時用 TMF、日盤表格用 TXF）"""
from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

HORIZONS = (5, 15, 30, 60)                 # 持有時間（分鐘）
KS = (0.5, 1.0, 1.5, 2.0, 3.0)             # 距離倍數（距離＝k·σ·√T）
KS_TP = KS
KS_SL = (1.0, 1.5, 2.0, 3.0)
WINDOW = 30                                # σ：最近 30 個 1 分鐘變動
MIN_VALID = 25                             # 其中至少 25 個有效才算
FIRST = 30                                 # 每個時段從第 30 格起取進場點
STEP = 5                                   # 每 5 格一個
MIN_N = 300                                # 進場樣本少於這個就標「樣本少」
MAX_HALF_GAP = 0.04                        # 前後半差距超過這個就標「不穩」
DAY_SLOTS = 300                            # 日盤 08:45~13:45
NIGHT_SLOTS = 840                          # 夜盤 15:00~05:00
OPEN_MIN = 8 * 60 + 45
NIGHT_OPEN_MIN = 15 * 60
OUT_PATH = Path(__file__).resolve().parent / "stop_calibration.json"


def gauss_touch(k: float) -> float:
    """連續隨機漫步在 T 內碰到 ±k·σ√T 其中一邊（單邊）的機率：2·(1−Φ(k))。"""
    return math.erfc(k / math.sqrt(2.0))


def trailing_sigma(cl: np.ndarray, window: int = WINDOW, min_valid: int = MIN_VALID) -> np.ndarray:
    """每一格 t 的 σ：到 t 為止最近 window 個 1 分鐘收盤價變動（cl[j]−cl[j−1]，j＝t−window+1…t）的樣本標準差（ddof=1）。
    有效（兩端都不是 NaN）的變動少於 min_valid 個就是 NaN；前 window 格沒有。單位：點（每分鐘）。"""
    n = len(cl)
    out = np.full(n, np.nan)
    if n <= window:
        return out
    d = np.diff(cl)                                           # d[j−1]＝cl[j]−cl[j−1]
    w = sliding_window_view(d, window)                        # 第 m 列＝d[m:m+window]，對應進場格 t＝m+window
    cnt = np.isfinite(w).sum(axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        sd = np.sqrt(np.nanvar(w, axis=1, ddof=1))
    sd[cnt < min_valid] = np.nan
    out[window:] = sd
    return out


def entry_slots(n: int, horizon: int, first: int = FIRST, step: int = STEP) -> np.ndarray:
    """進場格：從 first 起每 step 格，且之後 horizon 格都還在時段內。"""
    return np.arange(first, n - horizon, step)


def _first_hit(exc: np.ndarray, dist: np.ndarray, horizon: int) -> np.ndarray:
    """每個進場點：excursion（m×T）第一次 ≥ dist 的位置（0 起算）；沒碰到＝horizon。"""
    hit = exc >= dist[:, None]
    return np.where(hit.any(axis=1), hit.argmax(axis=1), horizon)


def session_counts(hi: np.ndarray, lo: np.ndarray, cl: np.ndarray, horizon: int) -> Optional[dict[str, Any]]:
    """一個時段、一個持有時間的計數（多空都算，各自獨立）：
    n          有效進場點數
    touch      各 k：多單不利（碰下緣）＋空單不利（碰上緣）的次數（分母 2n）
    wins/loss  各 (k_tp, k_sl)：多空合計先碰停利／先碰停損的次數（同一格兩邊都碰＝停損先到；都沒碰＝未結算）"""
    n = len(cl)
    ts = entry_slots(n, horizon)
    if len(ts) == 0:
        return None
    sig = trailing_sigma(cl)[ts]
    c = cl[ts]
    ok = np.isfinite(sig) & (sig > 0) & np.isfinite(c)
    if not ok.any():
        return None
    ts, sig, c = ts[ok], sig[ok], c[ok]
    h = sliding_window_view(hi, horizon)[ts + 1]              # 進場後 horizon 格的高、低
    low = sliding_window_view(lo, horizon)[ts + 1]
    up, dn = h - c[:, None], c[:, None] - low                 # 對多單：up＝有利、dn＝不利（空單相反）
    unit = sig * math.sqrt(horizon)
    fu = {k: _first_hit(up, k * unit, horizon) for k in KS}   # 往上碰到 k·σ√T 的第一格
    fd = {k: _first_hit(dn, k * unit, horizon) for k in KS}
    touch = [int(((fd[k] < horizon).astype(int) + (fu[k] < horizon)).sum()) for k in KS]
    wins = np.zeros((len(KS_TP), len(KS_SL)), dtype=np.int64)
    loss = np.zeros_like(wins)
    for i, kt in enumerate(KS_TP):
        for j, ks in enumerate(KS_SL):
            for tp_idx, sl_idx in ((fu[kt], fd[ks]), (fd[kt], fu[ks])):          # 多單；空單
                wins[i, j] += int((tp_idx < sl_idx).sum())
                loss[i, j] += int(((sl_idx <= tp_idx) & (sl_idx < horizon)).sum())
    return {"n": int(len(ts)), "touch": np.array(touch, dtype=np.int64), "wins": wins, "loss": loss}


def _merge(a: Optional[dict], b: Optional[dict]) -> Optional[dict]:
    if a is None:
        return b
    if b is None:
        return a
    return {"n": a["n"] + b["n"], "touch": a["touch"] + b["touch"], "wins": a["wins"] + b["wins"], "loss": a["loss"] + b["loss"]}


def _rates(c: Optional[dict]) -> dict[str, Any]:
    """計數 → 比例；沒有樣本就都是 None。"""
    if not c or c["n"] == 0:
        return {"n": 0, "touch": [None] * len(KS), "win": [[None] * len(KS_SL) for _ in KS_TP], "unres": [[None] * len(KS_SL) for _ in KS_TP]}
    res = c["wins"] + c["loss"]
    with np.errstate(invalid="ignore", divide="ignore"):
        win = np.where(res > 0, c["wins"] / np.maximum(res, 1), np.nan)
    unres = 1.0 - res / (2.0 * c["n"])
    return {"n": c["n"], "touch": [float(x) for x in c["touch"] / (2.0 * c["n"])],
            "win": [[None if not np.isfinite(x) else float(x) for x in row] for row in win], "unres": [[float(x) for x in row] for row in unres]}


def calibrate(sessions: Sequence[tuple[str, np.ndarray, np.ndarray, np.ndarray]], horizons: Sequence[int] = HORIZONS) -> dict[str, Any]:
    """sessions：[(日期, hi, lo, cl)]（依日期排序、每個陣列等長、缺的格是 NaN）。回傳每個持有時間的實測表（含前後半與日級標準誤）。"""
    out: dict[str, Any] = {}
    half = (len(sessions) + 1) // 2
    for T in horizons:
        per_day = [session_counts(hi, lo, cl, T) for _, hi, lo, cl in sessions]
        total = None
        halves: list[Optional[dict]] = [None, None]
        for i, c in enumerate(per_day):
            total = _merge(total, c)
            halves[0 if i < half else 1] = _merge(halves[0 if i < half else 1], c)
        day_means = np.array([c["touch"] / (2.0 * c["n"]) for c in per_day if c is not None])
        se = (day_means.std(axis=0, ddof=1) / math.sqrt(len(day_means))).tolist() if len(day_means) > 1 else [None] * len(KS)
        r, r1, r2 = _rates(total), _rates(halves[0]), _rates(halves[1])
        unstable_touch = [bool(a is None or b is None or abs(a - b) > MAX_HALF_GAP) for a, b in zip(r1["touch"], r2["touch"])]
        unstable_win = [[bool(a is None or b is None or abs(a - b) > MAX_HALF_GAP) for a, b in zip(ra, rb)] for ra, rb in zip(r1["win"], r2["win"])]
        out[str(T)] = {"n": r["n"], "days": len(day_means), "touch": r["touch"], "touch_se": se, "gauss": [gauss_touch(k) for k in KS],
                       "win": r["win"], "unres": r["unres"], "half_touch": [r1["touch"], r2["touch"]], "half_win": [r1["win"], r2["win"]],
                       "unstable_touch": unstable_touch, "unstable_win": unstable_win, "few": bool(r["n"] < MIN_N)}
    return out


# ── 讀資料 ───────────────────────────────────────────────────────

def day_sessions(store: Any, code: str = "TXF") -> list[tuple[str, np.ndarray, np.ndarray, np.ndarray]]:
    from core.intraday_context import load_sessions
    return [(s.date, s.hi, s.lo, s.cl) for s in load_sessions(store, code)]


def flow_sessions(store: Any, code: str = "TMF", night: bool = True, min_bars: int = 300) -> list[tuple[str, np.ndarray, np.ndarray, np.ndarray]]:
    """flow_1m（TMF 真實成交的 1 分 K；ts＝分鐘起點）→ 夜盤（15:00 起、840 格）或日盤（08:45 起、300 格）時段。沒成交的分鐘是 NaN。"""
    rows = store.flow_1m(code)
    n_slots, base = (NIGHT_SLOTS, NIGHT_OPEN_MIN) if night else (DAY_SLOTS, OPEN_MIN)
    by_day: dict[str, dict[str, np.ndarray]] = {}
    for r in rows:
        if r.get("close") is None or r.get("high") is None or r.get("low") is None:
            continue
        t = datetime.fromtimestamp(r["ts"])                                  # 本機時間（台北）
        mod = t.hour * 60 + t.minute
        slot = (mod - base) % 1440
        if slot >= n_slots:
            continue
        if night:                                                            # 凌晨那一段算前一天晚上的時段
            day = t.date() if mod >= base else datetime.fromtimestamp(r["ts"] - 86400).date()
        else:
            day = t.date()
        a = by_day.setdefault(str(day), {k: np.full(n_slots, np.nan) for k in ("hi", "lo", "cl")})
        a["hi"][slot], a["lo"][slot], a["cl"][slot] = r["high"], r["low"], r["close"]
    out = []
    for d, a in sorted(by_day.items()):
        if int(np.isfinite(a["cl"]).sum()) >= min_bars:
            out.append((d, a["hi"], a["lo"], a["cl"]))
    return out


# ── 來源一致性：同一分鐘的 σ（TMF 真實成交）÷ σ（TXF 1 分 K）──────────

def sigma_ratio(txf: Sequence[tuple[str, np.ndarray, np.ndarray, np.ndarray]], tmf: Sequence[tuple[str, np.ndarray, np.ndarray, np.ndarray]]) -> dict[str, Any]:
    by_tmf = {d: cl for d, _, _, cl in tmf}
    ratios = []
    days = 0
    for d, _, _, cl in txf:
        other = by_tmf.get(d)
        if other is None:
            continue
        days += 1
        a, b = trailing_sigma(cl), trailing_sigma(other)
        idx = np.arange(FIRST, len(cl), STEP)
        ok = np.isfinite(a[idx]) & np.isfinite(b[idx]) & (a[idx] > 0)
        ratios.extend((b[idx][ok] / a[idx][ok]).tolist())
    if not ratios:
        return {"days": days, "n": 0, "median": None, "p10": None, "p90": None}
    r = np.array(ratios)
    return {"days": days, "n": len(r), "median": float(np.median(r)), "p10": float(np.percentile(r, 10)), "p90": float(np.percentile(r, 90))}


# ── 報告 ─────────────────────────────────────────────────────────

def _pct(x: Optional[float]) -> str:
    return "  —  " if x is None else f"{x * 100:5.1f}"


def format_report(name: str, cal: dict[str, Any]) -> str:
    lines = [f"═══ {name} ═══"]
    for T, c in cal.items():
        flag = "（樣本少）" if c["few"] else ""
        lines.append(f"\n持有 {T} 分鐘：進場點 {c['n']:,}、{c['days']} 個時段{flag}")
        lines.append("  被雜訊掃到的機率（%）   k：" + "  ".join(f"{k:>5}" for k in KS))
        lines.append("    實測                 " + "  ".join(_pct(x) for x in c["touch"]))
        lines.append("    常態公式             " + "  ".join(_pct(x) for x in c["gauss"]))
        lines.append("    前半／後半差（點數）  " + "  ".join(f"{abs((a or 0) - (b or 0)) * 100:5.1f}" for a, b in zip(*c["half_touch"])))
        lines.append("  沒有技巧時的勝率（%）：列＝停利 k_tp，欄＝停損 k_sl（實測｜公式 k_sl÷(k_tp＋k_sl)｜未結算比例）")
        lines.append("    k_tp＼k_sl   " + "   ".join(f"{k:>16}" for k in KS_SL))
        for i, kt in enumerate(KS_TP):
            cells = [f"{_pct(c['win'][i][j])}|{ks / (kt + ks) * 100:5.1f}|{c['unres'][i][j] * 100:4.0f}" for j, ks in enumerate(KS_SL)]
            lines.append(f"    {kt:>5}      " + "   ".join(cells))
        bad = sum(c["unstable_touch"]) + sum(sum(r) for r in c["unstable_win"])
        lines.append(f"  前後半差距 > {MAX_HALF_GAP * 100:.0f} 個百分點的格子：{bad} 格（UI 會標「不穩」）")
    return "\n".join(lines)


def build(store: Any) -> dict[str, Any]:
    day, night = day_sessions(store), flow_sessions(store, night=True)
    return {"version": 1, "generated": datetime.now().strftime("%Y-%m-%d"),
            "sigma": {"window": WINDOW, "min_valid": MIN_VALID, "unit": "點／√分鐘", "entry_first": FIRST, "entry_step": STEP},
            "ks": list(KS), "ks_tp": list(KS_TP), "ks_sl": list(KS_SL), "horizons": list(HORIZONS), "min_n": MIN_N, "max_half_gap": MAX_HALF_GAP,
            "day": {"source": "TXF bars_1m（日盤 1 分 K）", "sessions": len(day), "from": day[0][0] if day else None, "to": day[-1][0] if day else None, "table": calibrate(day)},
            "night": {"source": "TMF flow_1m（夜盤真實成交 1 分 K）", "sessions": len(night), "from": night[0][0] if night else None, "to": night[-1][0] if night else None,
                      "table": calibrate(night), "few": True}}


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="python -m core.stop_calibration", description="停損停利建議的校準（update.md 發現 33）")
    ap.add_argument("cmd", choices=("run", "check-source"))
    ap.add_argument("--write", action="store_true", help="run：把結果寫進 core/stop_calibration.json")
    a = ap.parse_args(argv)
    from core.market_store import MarketStore
    store = MarketStore()
    if a.cmd == "check-source":
        r = sigma_ratio(day_sessions(store), flow_sessions(store, night=False, min_bars=280))
        print(json.dumps(r, ensure_ascii=False))
        return 0
    res = build(store)
    print(f"日盤：{res['day']['sessions']} 個時段（{res['day']['from']} ~ {res['day']['to']}）；夜盤：{res['night']['sessions']} 晚（{res['night']['from']} ~ {res['night']['to']}）")
    print(format_report("日盤（TXF 1 分 K）", res["day"]["table"]))
    print()
    print(format_report("夜盤（TMF 真實成交 1 分 K，樣本少）", res["night"]["table"]))
    if a.write:
        OUT_PATH.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"\n已寫入 {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
