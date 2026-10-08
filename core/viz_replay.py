"""市場指標視覺化（歷史回放版）與它的統計驗證。

需求文件《市場指標視覺化工具》：用顏色色相（Hurst）、飽和度（IV 百分位）、形狀（外/內盤方向）同時編碼三個指標，
一眼看出「協調」或「矛盾」。文件規定：**必須先用歷史回放＋統計驗證**，「視覺上感覺對」跟「事後驗證準不準」對得上，
才能做即時版——否則只是好看，不是有效訊號。

這裡做兩件事：
  build_replay  把 indicator_daily（每日 Hurst／日 K 方向／IV 百分位）與 flow_1m（每分鐘外/內盤）排成「日 × 時段」的格子，
                每格有顏色、形狀、協調／矛盾；
  validate      對格子做統計檢定（排列檢定＋CUSUM），回答「協調的時段之後，價格真的比矛盾的時段更常往預期方向走嗎？」

「協調／矛盾」的定義（使用者 2026-10-08 同意沿用系統現有的）：
  預期方向 want ＝ 盤前偏向方向（Hurst 狀態 × 日 K 方向，與 scalp market_bias=2、〈盤中即時〉面板同一套規則）
  協調 ＝ 該時段的買賣力道（外/內盤）和 want 同向；矛盾 ＝ 反向；力道中性不算。
  Hurst 只有「持續性」沒有方向，方向來自日 K 與 20 日均線的關係——文件範例把暖色當成看多，這裡不這麼做。

買賣力道要重現「〈盤中即時〉面板當時會顯示什麼」：最近 100 筆成交的外盤占比（每分鐘用最近幾分鐘湊到 100 筆來近似）＋ 60%/40% 門檻＋遲滯，
逐分鐘跑狀態機，格子取該時段**結束那一刻**的狀態。不能拿整個時段的占比去套 60%/40%——一格幾千筆成交，占比必然貼近 50%，
幾乎全判成中性（第一版就犯了這個錯，整個樣本只剩 1 格協調、1 格矛盾）。

不看未來：第 T 天用 date < T 的最後一列每日指標（盤前就知道的）；買賣力道只用到該時段結束；預期報酬從時段結束之後才開始算。
IV 百分位目前沒有歷史（補不回來），缺的日子飽和度顯示為淡色；「協調」的判斷本來就不用到 IV。
"""
from __future__ import annotations

import math
import time
from bisect import bisect_left
from typing import Any

import numpy as np

from core.daily_summary import combine
from core.flow_store import PREFIX
from core.live_state import FLOW_DOWN, FLOW_MARGIN, FLOW_UP, flow_direction
from core.market_store import MarketStore

SESSION_START = 845                 # 日盤 08:45~13:45
SESSION_MINUTES = 300
MIN_DAY_MINUTES = 200               # 日盤至少有這麼多分鐘有成交，這天才算
SHAPE = {1: "up", -1: "down", 0: "flat"}


def _num(x: Any, nd: int = 4) -> float | None:
    """JSON 不能放 NaN／inf。"""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── 視覺編碼 ─────────────────────────────────────────────────────

def hurst_color(h: float | None, iv_pct: float | None, neutral_band: float = 0.05, full_at: float = 0.10) -> dict[str, Any]:
    """Hurst → 色相、IV 百分位 → 飽和度。

    連續漸變（避免硬門檻在邊界上忽冷忽熱，見 THRESHOLDS.md）：|H−0.5| 在中性帶內是灰色，過了中性帶才慢慢上色，到 full_at 全色；
    暖色（橙→紅）＝趨勢延續、冷色（青→藍）＝均值回歸。IV 百分位越高越鮮豔；沒有 IV 的日子用淡色（並標 iv_known=False）。"""
    if h is None:
        return {"kind": "none", "hue": 0, "sat": 0.0, "light": 18, "strength": 0.0, "iv_known": iv_pct is not None,
                "css": "hsl(240, 12%, 14%)"}
    d = h - 0.5
    strength = clamp((abs(d) - neutral_band) / max(full_at - neutral_band, 1e-9))
    iv_sat = 0.25 if iv_pct is None else 0.25 + 0.75 * clamp(iv_pct / 100.0)
    if strength == 0:
        kind, hue, sat = "neutral", 0.0, 0.0
    else:
        kind = "trend" if d > 0 else "revert"
        hue = 40.0 - 40.0 * strength if d > 0 else 170.0 + 50.0 * strength
        sat = iv_sat * clamp(strength / 0.25)            # 剛過中性帶時從灰色淡入，不會在邊界上突然變色
    light = 50
    return {"kind": kind, "hue": round(hue, 1), "sat": round(sat, 3), "light": light, "strength": round(strength, 3),
            "iv_known": iv_pct is not None, "css": f"hsl({hue:.0f}, {sat * 100:.0f}%, {light}%)" if kind != "neutral"
            else "hsl(0, 0%, 46%)"}


def want_direction(hurst_state: str | None, direction: int | None, iv_state: str | None = None) -> int:
    """預期方向 +1／-1／0：與 scalp market_bias=2 同一套（趨勢 → 順日 K 方向、均值回歸 → 逆日 K 方向、其餘 0）。"""
    state = combine(hurst_state or "UNCERTAIN", iv_state or "UNKNOWN")[0]
    mult = 1 if state == "TREND" else -1 if state == "REVERT" else 0
    return int(direction or 0) * mult


def _hhmm_label(minute_of_session: int) -> str:
    base = (SESSION_START // 100) * 60 + SESSION_START % 100
    t = base + minute_of_session
    return f"{t // 60:02d}:{t % 60:02d}"


# ── 回放：日 × 時段的格子 ─────────────────────────────────────────

def build_replay(store: MarketStore | None = None, *, days: int = 40, block_min: int = 15, flow_window: int = 100,
                 flow_up: float = FLOW_UP, flow_down: float = FLOW_DOWN, flow_margin: float = FLOW_MARGIN,
                 neutral_band: float = 0.05, full_at: float = 0.10, min_trades: int = 30) -> dict[str, Any]:
    """最近 days 個「日盤資料完整」的交易日，每天切成 block_min 分鐘一格。
    買賣力道＝最近 flow_window 筆有方向成交的外盤占比（逐分鐘，向前最多湊 5 分鐘；不足 min_trades 筆就當沒有資料），
    套 flow_up／flow_down 與遲滯；格子取該時段結束那一刻的狀態，預期報酬看「下一格」的價格變動。"""
    store = store or MarketStore()
    daily = store.indicator_daily()
    dates = [r["date"] for r in daily]
    by_day: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for r in store.flow_1m(PREFIX):
        lt = time.localtime(r["ts"])
        m = (lt.tm_hour * 60 + lt.tm_min) - ((SESSION_START // 100) * 60 + SESSION_START % 100)     # 這是日盤第幾分鐘
        if 0 <= m < SESSION_MINUTES:
            by_day.setdefault(f"{lt.tm_year}-{lt.tm_mon:02d}-{lt.tm_mday:02d}", []).append((m, r))
    chosen = sorted(d for d, v in by_day.items() if len(v) >= MIN_DAY_MINUTES)[-days:]
    n_blocks = -(-SESSION_MINUTES // block_min)
    out_days: list[dict[str, Any]] = []
    for d in chosen:
        i = bisect_left(dates, d) - 1                               # date < d 的最後一列：這天盤前就知道的
        base = daily[i] if i >= 0 else None
        want = want_direction(base["hurst_state"], base["direction"], base["iv_state"]) if base else 0
        color = hurst_color(base["hurst"] if base else None, base["iv_pct"] if base else None, neutral_band, full_at)
        mins = by_day[d]
        buy, sell, unk = np.zeros(SESSION_MINUTES), np.zeros(SESSION_MINUTES), np.zeros(SESSION_MINUTES)
        close, open_ = [None] * SESSION_MINUTES, [None] * SESSION_MINUTES
        for m, r in mins:
            buy[m], sell[m], unk[m], close[m], open_[m] = r["buy_n"], r["sell_n"], r["unk_n"], r["close"], r["open"]
        # 逐分鐘跑「最近 flow_window 筆」的外盤占比＋遲滯狀態機（重現即時面板）；每天從中性開始
        dirs, shares, prev = [0] * SESSION_MINUTES, [None] * SESSION_MINUTES, 0
        for m in range(SESSION_MINUTES):
            lo, tot = m, 0.0
            while lo >= max(0, m - 4) and tot < flow_window:
                tot += buy[lo] + sell[lo]
                lo -= 1
            nb, ns = buy[lo + 1: m + 1].sum(), sell[lo + 1: m + 1].sum()
            sh = nb / (nb + ns) if nb + ns >= min_trades else None
            prev = flow_direction(sh, prev, flow_up, flow_down, flow_margin) if sh is not None else 0
            dirs[m], shares[m] = prev, sh
        ends = []                                                   # 每格「最後一個有成交的分鐘」
        for b in range(n_blocks):
            idx = [m for m in range(b * block_min, min((b + 1) * block_min, SESSION_MINUTES)) if close[m] is not None]
            ends.append(idx[-1] if idx else None)
        cells = []
        for b in range(n_blocks):
            e = ends[b]
            span = range(b * block_min, min((b + 1) * block_min, SESSION_MINUTES))
            dir_ = dirs[e] if e is not None else 0
            share = shares[e] if e is not None else None
            coh = None if (want == 0 or share is None) else (0 if dir_ == 0 else 1 if dir_ == want else -1)
            nxt = ends[b + 1] if b + 1 < n_blocks else None
            fwd = None
            if e is not None and nxt is not None:
                # 起點＝訊號之後的第一筆成交價（下一個有成交的分鐘開盤價），終點＝下一格最後一筆成交價。
                # 不能用「訊號那一刻的最後一筆成交價」當起點：外盤成交印在賣價、內盤印在買價，起點會系統性偏向訊號的方向，
                # 之後的變動就帶著買賣價差彈跳的假回歸（對照組實測：2 分鐘的買賣力道看起來有「反轉」效果，p=0.03）。
                first_after = next((m for m in range(e + 1, SESSION_MINUTES) if open_[m] is not None), None)
                if first_after is not None:
                    fwd = close[nxt] - open_[first_after]
            cells.append({"i": b, "start": _hhmm_label(b * block_min),
                          "trades": int(sum(buy[m] + sell[m] + unk[m] for m in span)), "share": _num(share), "dir": dir_,
                          "shape": SHAPE[dir_], "coherence": coh, "up_min": sum(1 for m in span if dirs[m] == 1),
                          "down_min": sum(1 for m in span if dirs[m] == -1), "close": close[e] if e is not None else None,
                          "fwd": _num(fwd, 1)})
        first_open, last_close = mins[0][1]["open"], mins[-1][1]["close"]
        out_days.append({"date": d, "asof": base["date"] if base else None,
                         "hurst": _num(base["hurst"]) if base else None, "hurst_z": _num(base["hurst_z"], 2) if base else None,
                         "hurst_state": base["hurst_state"] if base else None, "direction": base["direction"] if base else None,
                         "iv_pct": _num(base["iv_pct"], 1) if base else None, "iv_state": base["iv_state"] if base else None,
                         "want": want, "color": color, "open": first_open, "close": last_close,
                         "move": _num(last_close - first_open, 1), "cells": cells})
    return {"params": {"days": days, "block_min": block_min, "flow_window": flow_window, "flow_up": flow_up, "flow_down": flow_down,
                       "flow_margin": flow_margin, "neutral_band": neutral_band, "full_at": full_at, "min_trades": min_trades},
            "blocks": [{"i": b, "start": _hhmm_label(b * block_min)} for b in range(n_blocks)],
            "days": out_days,
            "coverage": {"days": len(out_days), "with_hurst": sum(1 for x in out_days if x["hurst"] is not None),
                         "with_iv": sum(1 for x in out_days if x["iv_pct"] is not None),
                         "with_want": sum(1 for x in out_days if x["want"] != 0)}}


# ── 統計驗證 ─────────────────────────────────────────────────────

def _group(s: np.ndarray) -> dict[str, Any]:
    nz = s[s != 0]
    return {"n": int(s.size), "hit_rate": _num((nz > 0).mean()) if nz.size else None, "mean_move": _num(s.mean(), 2) if s.size else None}


def validate(replay: dict[str, Any], *, n_perm: int = 2000, seed: int = 7, min_group: int = 30, n_env: int = 500) -> dict[str, Any]:
    """「協調」的時段之後，價格真的比「矛盾」的時段更常往預期方向走嗎？

    單位是格子（預設 15 分鐘）：s ＝ 該格結束後、到下一格結束的價格變動（點，起點是訊號後第一筆成交）× 預期方向 want（正＝往預期方向走）。
    主要檢定：D ＝ 協調格的平均 s − 矛盾格的平均 s；虛無假設「買賣力道的方向跟後續走勢無關」→ 在同一天內把力道方向隨機洗牌
    （保留每天的 want 與當天的漲跌趨勢），算 D 的分布，雙尾 p 值。
    另有日級檢定（want 是否猜中當天日盤漲跌）與 CUSUM（協調格的累計 s，對照同樣數量的隨機格子形成的 5%~95% 區間）。
    只有一份歷史樣本，且同時看了兩個檢定——顯著也只能算線索，要用新資料再驗一次。"""
    rng = np.random.default_rng(seed)
    rows = []
    for di, day in enumerate(replay["days"]):
        w = day["want"]
        if w == 0:
            continue
        for c in day["cells"]:
            if c["coherence"] is None or c["fwd"] is None:
                continue
            rows.append((di, c["dir"], w, c["fwd"] * w))
    res: dict[str, Any] = {"eligible_cells": len(rows), "days_with_want": sum(1 for d in replay["days"] if d["want"] != 0),
                           "n_perm": n_perm, "min_group": min_group}
    if not rows:
        res.update({"coherent": _group(np.empty(0)), "contradictory": _group(np.empty(0)), "neutral": _group(np.empty(0)),
                    "diff": None, "p_value": None, "cusum": None, "day_level": None,
                    "verdict": {"level": "insufficient", "text": "沒有可檢定的格子（沒有預期方向的日子，或外/內盤資料不足）"}})
        return res
    day_ = np.array([r[0] for r in rows])
    dir_ = np.array([r[1] for r in rows])
    want_ = np.array([r[2] for r in rows])
    s_ = np.array([r[3] for r in rows], dtype=float)
    coh, con, neu = dir_ == want_, dir_ == -want_, dir_ == 0

    def stat(d: np.ndarray) -> float:
        a, b = d == want_, d == -want_
        return float(s_[a].mean() - s_[b].mean()) if a.any() and b.any() else float("nan")

    d_obs = stat(dir_)
    edges = np.flatnonzero(np.diff(day_)) + 1
    slices = list(zip(np.concatenate([[0], edges]), np.concatenate([edges, [day_.size]])))
    perm = np.empty(n_perm)
    for k in range(n_perm):
        p = dir_.copy()
        for a, b in slices:
            p[a:b] = rng.permutation(dir_[a:b])                      # 同一天內洗牌：保留當天的 want 與趨勢，只打散「力道方向」
        perm[k] = stat(p)
    ok = np.isfinite(perm)
    p_value = float((1 + np.sum(np.abs(perm[ok]) >= abs(d_obs))) / (ok.sum() + 1)) if np.isfinite(d_obs) and ok.any() else None

    cusum = None
    if coh.sum() >= 2:
        obs = np.cumsum(s_[coh])
        env = np.empty((n_env, obs.size))
        for k in range(n_env):
            idx = np.sort(rng.choice(s_.size, size=obs.size, replace=False))
            env[k] = np.cumsum(s_[idx])
        lo, hi = np.percentile(env, 5, axis=0), np.percentile(env, 95, axis=0)
        cusum = {"observed": [_num(x, 1) for x in obs], "lo": [_num(x, 1) for x in lo], "hi": [_num(x, 1) for x in hi],
                 "end_outside": bool(obs[-1] > hi[-1] or obs[-1] < lo[-1])}

    # 日級：want 有沒有猜中當天日盤的漲跌？（把 want 在各天之間洗牌當虛無假設）
    dd = [(d["want"], d["move"]) for d in replay["days"] if d["want"] != 0 and d["move"] not in (None, 0)]
    day_level = None
    if len(dd) >= 5:
        w = np.array([x[0] for x in dd])
        mv = np.sign([x[1] for x in dd])
        obs_hit = float((w == mv).mean())
        null = np.array([(rng.permutation(w) == mv).mean() for _ in range(n_perm)])
        p_day = float((1 + np.sum(np.abs(null - null.mean()) >= abs(obs_hit - null.mean()))) / (n_perm + 1))
        day_level = {"days": len(dd), "hit_rate": _num(obs_hit), "null_mean": _num(null.mean()), "p_value": _num(p_day)}

    c, k = int(coh.sum()), int(con.sum())
    if c < min_group or k < min_group:
        verdict = {"level": "insufficient", "text": f"樣本不足：協調 {c} 格、矛盾 {k} 格（各至少 {min_group} 格才檢定）。外/內盤歷史越長越準。"}
    elif p_value is None or p_value >= 0.05:
        verdict = {"level": "none", "text": ("沒有顯著差異：協調的時段之後，價格並沒有比矛盾的時段更常往預期方向走（p="
                                             f"{p_value:.2f}）。目前只是好看，不是有效訊號，不建議做即時版。")}
    elif d_obs > 0:
        verdict = {"level": "significant", "text": f"有顯著差異（p={p_value:.3f}），方向符合預期。但只是一份歷史樣本，上線前要用新資料再驗證一次。"}
    else:
        verdict = {"level": "wrong_way", "text": f"有顯著差異（p={p_value:.3f}），但方向相反：協調的時段之後反而更不容易往預期方向走。"}
    res.update({"coherent": _group(s_[coh]), "contradictory": _group(s_[con]), "neutral": _group(s_[neu]),
                "diff": _num(d_obs, 2), "p_value": _num(p_value), "cusum": cusum, "day_level": day_level, "verdict": verdict})
    return res
