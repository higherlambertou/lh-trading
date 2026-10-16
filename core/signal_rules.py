"""順勢爆量訊號的定義（update.md 發現 43）：歷史檢定（core/signal_study.py）與即時影子模式（core/signal_shadow.py）共用這一份，兩邊不會各算各的。

規則（跑之前固定、登記在 update.md，之後不改）：每一分鐘收完時——
  move     ＝ 這根收盤 − W 根之前的收盤，除以 W 根之前的收盤（W＝5 分鐘）；
  vol_ratio ＝ 最近 W 分鐘的成交量合計 ÷「前 BASE_SESSIONS 個時段、同一個時間窗的合計」的中位數（至少 MIN_BASE 個時段有值才算；只用當天以前）。
  |move| ≥ 門檻 且 vol_ratio ≥ 2.0 → 訊號，方向＝move 的方向（順勢）。訊號之後 COOLDOWN 分鐘不再發。
  進場＝下一分鐘的開盤價（訊號在收盤才知道）；持有 H 分鐘，出場＝第 t＋H 分鐘的收盤；成本是每筆來回固定的 COST_PTS 點。
門檻與成本用「點數 ÷ REF_PRICE」換成百分比：TXF 從 2020 年的約 1 萬點漲到 4.7 萬點，同樣 30 點在歷史上不可比；REF_PRICE＝47,000 時就是你說的 30／40／50 點。

純函式、只吃 numpy 陣列，不碰資料庫與下單。時間的序號：時段內第幾分鐘（日盤 0～299、夜盤 0～839），每個陣列的長度就是時段的長度。"""
from __future__ import annotations

import os
import warnings
from typing import NamedTuple, Optional, Sequence

import numpy as np

W = 5                              # 突然＝幾分鐘內的變動
VOL_RATIO = 2.0                    # 成交量要是平常的幾倍以上
BASE_SESSIONS = 20                 # 平常＝前幾個時段
MIN_BASE = 10                      # 同一個時間窗至少幾個時段有值才算
COOLDOWN = 30                      # 訊號之後幾分鐘不再發（也讓 H＝30 的持有不重疊）
REF_PRICE = 47_000.0               # 把點數換成百分比用的參考價
POINTS = (30, 40, 50)              # 你說的「30～50 點」
HORIZONS = (5, 15, 30)             # 持有幾分鐘
HORIZON_MAX = 60                   # 影子模式存訊號之後幾分鐘的 K 棒
TP_GRID = (30, 50, 80)                       # 停利（點，÷REF_PRICE）——發現 44 登記的網格
SL_GRID = (20, 30, 50)                       # 停損（點）
MAX_HOLD = 30                                # 停損停利都沒碰到時，持有滿幾分鐘出場（＝冷卻，所以事件不會重疊）


def cost_pts() -> float:
    """每筆來回成本（點）：預設 4（你說差不多算 4 點）；SHADOW_COST_PTS 可調。"""
    try:
        v = float(os.getenv("SHADOW_COST_PTS", "4"))
    except ValueError:
        return 4.0
    return v if v >= 0 else 4.0


def cost_pct() -> float:
    return cost_pts() / REF_PRICE


def threshold_pct(points: float) -> float:
    return points / REF_PRICE


class Event(NamedTuple):
    m: int                # 訊號那一分鐘（收完）的時段內序號
    direction: int        # +1 往上、−1 往下（順勢＝跟著這個方向）
    move: float           # W 分鐘的變動比例（帶正負）
    ratio: float          # 成交量是平常的幾倍


def rolling_sum(v: np.ndarray, w: int = W) -> np.ndarray:
    """每個位置的「到這一分鐘為止最近 w 分鐘」合計；窗口內有 NaN 或不夠長就是 NaN。"""
    n = len(v)
    out = np.full(n, np.nan)
    if n < w:
        return out
    c = np.concatenate([[0.0], np.cumsum(np.where(np.isfinite(v), v, 0.0))])
    bad = np.concatenate([[0], np.cumsum(~np.isfinite(v))])
    s = c[w:] - c[:-w]
    ok = (bad[w:] - bad[:-w]) == 0
    out[w - 1:] = np.where(ok, s, np.nan)
    return out


def baseline(windowed: np.ndarray, n_base: int = BASE_SESSIONS, min_base: int = MIN_BASE) -> np.ndarray:
    """windowed＝(時段數, 序號) 的 rolling_sum；回傳同形狀的「前 n_base 個時段同一位置的中位數」。第 d 列只用第 d 列以前的（不含自己）；有值的少於 min_base 個就是 NaN。"""
    d_n, n = windowed.shape
    out = np.full((d_n, n), np.nan)
    for d in range(d_n):
        lo = max(0, d - n_base)
        if d - lo < min_base:
            continue
        block = windowed[lo:d]
        cnt = np.isfinite(block).sum(axis=0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)          # 整欄都沒有值的位置會警告，那裡本來就會被 cnt 擋掉
            med = np.nanmedian(block, axis=0)
        out[d] = np.where(cnt >= min_base, med, np.nan)
    return out


def ratio_of(windowed_today: np.ndarray, base: np.ndarray) -> np.ndarray:
    """今天的窗口合計 ÷ 基準；基準是 NaN 或 ≤ 0 就是 NaN。"""
    with np.errstate(all="ignore"):
        r = windowed_today / base
    return np.where(np.isfinite(r) & (base > 0), r, np.nan)


def detect(close: np.ndarray, ratio: np.ndarray, thr_pct: float, *, vol_ratio: float = VOL_RATIO, w: int = W, cooldown: int = COOLDOWN) -> list[Event]:
    """一個時段內的訊號。只用「到這一分鐘為止」的資料（close[m]、close[m−w]、ratio[m]），冷卻只看更早的訊號——不偷看之後。"""
    out: list[Event] = []
    n = len(close)
    m = w
    while m < n:
        c0, c1, r = close[m - w], close[m], ratio[m]
        if np.isfinite(c0) and np.isfinite(c1) and np.isfinite(r) and c0 > 0:
            mv = (c1 - c0) / c0
            if abs(mv) >= thr_pct and r >= vol_ratio:
                out.append(Event(m, 1 if mv > 0 else -1, float(mv), float(r)))
                m += max(cooldown, 0) + 1
                continue
        m += 1
    return out


class Outcome(NamedTuple):
    horizon: int
    gross: float          # 未扣成本的報酬比例（順勢方向）
    net: float            # 扣掉成本（cost_pct）
    mfe: float            # 持有期間最有利的比例（用高低點）
    mae: float            # 最不利的比例（≥ 0 的幅度）


def outcome(entry: float, highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], direction: int, horizon: int,
            cost: Optional[float] = None) -> Optional[Outcome]:
    """進場價 entry（下一分鐘的開盤）、進場那一分鐘起的高／低／收序列（第 0 個＝進場那一分鐘）。第 horizon 分鐘的收盤出場；序列不夠長回 None。"""
    if not (entry and entry > 0) or len(closes) < horizon:
        return None
    c = cost_pct() if cost is None else cost
    h, lo, cl = np.asarray(highs[:horizon], float), np.asarray(lows[:horizon], float), float(closes[horizon - 1])
    if not (np.isfinite(h).all() and np.isfinite(lo).all() and np.isfinite(cl)):
        return None
    gross = direction * (cl - entry) / entry
    fav = (h.max() - entry) / entry if direction > 0 else (entry - lo.min()) / entry
    adv = (entry - lo.min()) / entry if direction > 0 else (h.max() - entry) / entry
    return Outcome(horizon, float(gross), float(gross - c), float(max(fav, 0.0)), float(max(adv, 0.0)))


class Bracket(NamedTuple):
    reason: str           # "tp" 停利先到｜"sl" 停損先到｜"time" 滿 max_hold 分鐘出場
    gross: float          # 未扣成本的報酬比例（順勢方向）
    net: float            # 扣掉成本
    minutes: int          # 持有幾分鐘（出場那一根是進場後第幾根，1 起算）


def bracket(entry: float, direction: int, opens: Sequence[float], highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
            tp_pct: float, sl_pct: float, max_hold: int = MAX_HOLD, cost: Optional[float] = None) -> Optional[Bracket]:
    """進場價 entry（進場那一分鐘的開盤），序列的第 0 個＝進場那一分鐘。逐分鐘看：停損價碰到、停利價碰到、或滿 max_hold 分鐘（第 max_hold 分鐘的收盤）出場，最先發生的算。
    保守的假設（發現 44 登記）：同一分鐘高低點把停利與停損都碰到 → 算停損先到；停損遇到跳空（開盤已經越過停損價）→ 用開盤價（比停損價更差）；停利一律用停利價（不算跳空多賺的）。
    序列不夠長或有缺值回 None。"""
    if not (entry and entry > 0) or min(len(opens), len(highs), len(lows), len(closes)) < max_hold:
        return None
    o = np.asarray(opens[:max_hold], float)
    h = np.asarray(highs[:max_hold], float)
    lo = np.asarray(lows[:max_hold], float)
    cl = np.asarray(closes[:max_hold], float)
    if not (np.isfinite(o).all() and np.isfinite(h).all() and np.isfinite(lo).all() and np.isfinite(cl).all()):
        return None
    c = cost_pct() if cost is None else cost
    if direction > 0:
        tp_px, sl_px = entry * (1 + tp_pct), entry * (1 - sl_pct)
    else:
        tp_px, sl_px = entry * (1 - tp_pct), entry * (1 + sl_pct)
    for k in range(max_hold):
        sl_hit = lo[k] <= sl_px if direction > 0 else h[k] >= sl_px
        tp_hit = h[k] >= tp_px if direction > 0 else lo[k] <= tp_px
        if sl_hit:                                                 # 同一分鐘兩邊都碰到也算停損先到
            px = min(sl_px, o[k]) if direction > 0 else max(sl_px, o[k])
            reason = "sl"
        elif tp_hit:
            px, reason = tp_px, "tp"
        else:
            continue
        g = direction * (px - entry) / entry
        return Bracket(reason, float(g), float(g - c), k + 1)
    g = direction * (cl[max_hold - 1] - entry) / entry
    return Bracket("time", float(g), float(g - c), max_hold)
