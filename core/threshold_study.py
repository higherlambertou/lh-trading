"""門檻判斷連續化檢查（需求文件〈門檻判斷連續化檢查〉第二步）：量測指標在硬門檻附近的震盪。

對每個「指標 → 硬門檻 → 分類」的地方，在真實歷史上算：
  切換     硬門檻下分類標籤變動的次數（日級指標看「每日」、逐筆指標看「每小時」）
  來回     切換後 K 筆之內又切回去的比例（越高 ＝ 門檻附近越像雜訊在抖）
  帶內     指標落在門檻 ±5% 範圍內的時間比例
  遲滯後   加一條遲滯帶（Schmitt trigger：進入要超過門檻 +5%、離開要回到門檻 -5% 以內）後的切換次數；
           「可省」＝遲滯帶能省掉多少比例的切換

只讀本機資料（data/market_state.db 的日 K／1 分 K、data/ticks.db 的逐筆成交），不連券商、不碰交易路徑。
門檻直接讀程式裡的常數與策略的預設值（不抄數字），改了門檻重跑就會跟著變；
指標的算式直接呼叫策略自己的方法（`_rsi()`／`_bands()`／`_ma()`），不另抄一份。

    python -m core.threshold_study
"""
from __future__ import annotations

import inspect
import sqlite3
import time
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

import numpy as np

from core.hurst_analyzer import REVERT_TH, TREND_TH, trend_direction
from core.hurst_study import CODE, rolling_daily
from core.iv_monitor import HIGH_PCT, LOOKBACK, LOW_PCT, iv_percentile
from core.live_state import BIG_MOVE, FLOW_DOWN, FLOW_UP, FLOW_WINDOWS, QUIET
from core.market_store import MarketStore
from core.tick_store import DB_PATH as TICKS_DB
from strategies.bollinger import BollingerStrategy
from strategies.breakout import BreakoutStrategy
from strategies.ma_cross import MACrossStrategy
from strategies.momentum import MomentumStrategy
from strategies.rsi import RSIStrategy
from strategies.scalp import ScalpStrategy
from strategies.vwap_revert import VWAPRevertStrategy

EPS = 1e-9
MARGIN_PCT = 0.05           # 遲滯帶／「門檻附近」＝門檻 ±5%
MIN_DAILY = 25              # 日級指標至少要這麼多筆才量測
MIN_IV_POINTS = 30          # IV 百分位序列至少要這麼多筆才量測
WHIP_DAILY = 3              # 日級：3 個交易日內切回去算「來回」
WHIP_BAR = 5                # 1 分 K：5 根內
WHIP_TICK = 20              # 逐筆：20 筆成交內（約幾秒）
SESSION_GAP = 1800.0        # 逐筆資料間隔超過 30 分鐘 ＝ 不同盤別，指標視窗各自重算（等於每個盤別重新啟動策略）
PREOPEN = ((830, 845), (1450, 1500))     # 開盤前試算時段（日盤 08:30~08:45、夜盤 14:50~15:00）：行情不是真實成交


# ── 核心：分類、遲滯、震盪統計 ────────────────────────────────────

def label_hard(x: Sequence[float], hi: float, lo: float | None = None, inclusive: bool = False) -> np.ndarray:
    """硬門檻分類。三態：x 高於 hi → +1、低於 lo → -1、其餘 0；hi == lo（單一門檻）時只有 ±1 兩態。
    inclusive=True 時等於門檻也算（程式裡用 >= / <= 的地方，如 scalp 的外盤占比）。"""
    a = np.asarray(x, dtype=float)
    lo = hi if lo is None else lo
    if hi == lo:
        return np.where(a > hi, 1, -1).astype(np.int8)
    up = a >= hi - EPS if inclusive else a > hi
    dn = a <= lo + EPS if inclusive else a < lo
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def label_hyst(x: Sequence[float], hi: float, lo: float | None = None, margin: float = 0.0,
               inclusive: bool = False) -> np.ndarray:
    """遲滯分類（Schmitt trigger）：進入 +1 要超過 hi+margin、進入 -1 要低於 lo-margin；
    已在 +1 要回到 hi-margin 以下才離開（-1 同理）。起點用硬門檻決定。"""
    xs = np.asarray(x, dtype=float).tolist()
    if not xs:
        return np.zeros(0, dtype=np.int8)
    lo = hi if lo is None else lo
    two = hi == lo
    up_in, dn_in, up_out, dn_out = hi + margin, lo - margin, hi - margin, lo + margin
    state = int(label_hard([xs[0]], hi, lo, inclusive)[0])
    out = [0] * len(xs)
    for i, v in enumerate(xs):
        if two:
            if state <= 0 and v > up_in:
                state = 1
            elif state >= 0 and v < dn_in:
                state = -1
        elif state == 1:
            if v < up_out:
                state = -1 if v < dn_in else 0
        elif state == -1:
            if v > dn_out:
                state = 1 if v > up_in else 0
        elif v > up_in:
            state = 1
        elif v < dn_in:
            state = -1
        out[i] = state
    return np.asarray(out, dtype=np.int8)


def _flips(labels: np.ndarray) -> int:
    return int(np.count_nonzero(np.diff(labels))) if labels.size > 1 else 0


def _reversed_flips(labels: np.ndarray, k: int) -> int:
    """切換之後 k 筆之內又回到切換前標籤的次數。"""
    idx = np.flatnonzero(np.diff(labels)) + 1
    return sum(1 for j in idx if (labels[j + 1: j + 1 + k] == labels[j - 1]).any())


def _as_segments(obj: Any) -> list[np.ndarray]:
    """單一序列或「多段序列」（如每天一段；段與段之間不算切換）。"""
    if isinstance(obj, np.ndarray) and obj.ndim == 1:
        return [obj]
    items = list(obj)
    if items and np.ndim(items[0]) == 0:
        return [np.asarray(items, dtype=float)]
    return [np.asarray(s, dtype=float) for s in items]


def edge_stats(segments: Any, hi: float, lo: float | None = None, *, margin: float, whip_k: int = 3,
               inclusive: bool = False, units: float = 1.0) -> dict[str, Any]:
    """一個指標在門檻附近的震盪統計。units = 資料涵蓋的單位數（日／小時），用來換算成每單位切換次數。"""
    lo = hi if lo is None else lo
    n = flips = flips_h = rev = near = 0
    for seg in _as_segments(segments):
        s = np.asarray(seg, dtype=float)
        s = s[np.isfinite(s)]
        if s.size == 0:
            continue
        hard = label_hard(s, hi, lo, inclusive)
        hyst = label_hyst(s, hi, lo, margin, inclusive)
        n += int(s.size)
        flips += _flips(hard)
        flips_h += _flips(hyst)
        rev += _reversed_flips(hard, whip_k)
        near += int(((np.abs(s - hi) <= margin) | (np.abs(s - lo) <= margin)).sum())
    u = units if units > 0 else 1.0
    return {
        "n": n, "flips": flips, "per_unit": flips / u, "dwell": (n / flips) if flips else None,
        "whipsaw": (rev / flips) if flips else 0.0, "near": (near / n) if n else 0.0,
        "flips_hyst": flips_h, "per_unit_hyst": flips_h / u, "removed": (1.0 - flips_h / flips) if flips else 0.0,
    }


def _margin(hi: float, lo: float | None, x: np.ndarray | None = None) -> float:
    """遲滯帶寬度＝門檻的 ±5%；門檻在 0 附近（如均線差）改用指標本身標準差的 5%。"""
    lo = hi if lo is None else lo
    scale = (abs(hi) + abs(lo)) / 2
    if x is not None:
        v = np.asarray(x, dtype=float)
        v = v[np.isfinite(v)]
        if v.size > 1 and scale < 0.1 * float(v.std()):
            scale = float(v.std())
    return MARGIN_PCT * scale


# ── 指標序列（算式直接用策略／模組自己的）────────────────────────────

def ma_deviation(close: Sequence[float], ma: int) -> np.ndarray:
    """收盤 / ma 日均線 - 1（與 trend_direction 同算式，逐日）。"""
    c = np.asarray(close, dtype=float)
    if c.size < ma:
        return np.empty(0)
    sma = np.convolve(c, np.ones(ma) / ma, "valid")
    return c[ma - 1:] / sma - 1.0


def amplitude_ratio(high: Sequence[float], low: Sequence[float], lookback: int = 20, min_prev: int = 5) -> np.ndarray:
    """當日振幅 / 前 lookback 日平均振幅（與盤中即時面板的「振幅比」同定義）。"""
    rng = np.asarray(high, dtype=float) - np.asarray(low, dtype=float)
    out = []
    for i in range(rng.size):
        prev = rng[max(0, i - lookback): i]
        if prev.size >= min_prev and prev.mean() > 0:
            out.append(rng[i] / prev.mean())
    return np.asarray(out)


def iv_percentile_series(ivs: Sequence[float], lookback: int = LOOKBACK, min_hist: int = MIN_IV_POINTS) -> np.ndarray:
    """ivs 為舊→新。第 i 天的百分位＝它在「之前」最多 lookback 天裡的位置（與 evaluate_iv 同一個 iv_percentile）。"""
    out = []
    for i in range(min_hist, len(ivs)):
        p = iv_percentile(ivs[i], list(ivs[max(0, i - lookback): i])[::-1], lookback)
        if p is not None:
            out.append(p)
    return np.asarray(out)


def flow_ratio(types: Sequence[int], window: int) -> np.ndarray:
    """最近 window 筆「有方向」事件（tick_type 1 外盤／2 內盤）中的外盤占比；不足 window 筆的前段不輸出。"""
    t = np.asarray(types)
    typed = t[(t == 1) | (t == 2)]
    if typed.size < window:
        return np.empty(0)
    return np.convolve((typed == 1).astype(float), np.ones(window), "valid") / window


def rsi_series(prices: Sequence[float], s: RSIStrategy) -> np.ndarray:
    out = np.full(len(prices), np.nan)
    for i, p in enumerate(prices):
        s.prices.append(p)
        r = s._rsi()
        if r is not None:
            out[i] = r
    return out


def bollinger_z(prices: Sequence[float], s: BollingerStrategy) -> np.ndarray:
    """(價格 - 中軌) / 標準差；策略的上下軌就是 ±num_std。"""
    out = np.full(len(prices), np.nan)
    for i, p in enumerate(prices):
        s.prices.append(p)
        b = s._bands()
        if b is not None:
            mean, upper, _ = b
            sd = (upper - mean) / s.num_std if s.num_std else 0.0
            out[i] = (p - mean) / sd if sd > 0 else 0.0
    return out


def ma_gap(prices: Sequence[float], s: MACrossStrategy) -> np.ndarray:
    """快線 - 慢線（>0 做多、否則做空）。"""
    out = np.full(len(prices), np.nan)
    for i, p in enumerate(prices):
        s.prices.append(p)
        a, b = s._ma(s.short_period), s._ma(s.long_period)
        if a is not None and b is not None:
            out[i] = a - b
    return out


def momentum_pct(prices: Sequence[float], s: MomentumStrategy) -> np.ndarray:
    """目前價相對 period 筆之前的漲跌幅（%）。"""
    buf: deque[float] = deque(maxlen=s.period + 1)
    out = np.full(len(prices), np.nan)
    for i, p in enumerate(prices):
        buf.append(p)
        if len(buf) == s.period + 1 and buf[0]:
            out[i] = (p - buf[0]) / buf[0] * 100.0
    return out


def range_position(prices: Sequence[float], lookback: int) -> np.ndarray:
    """價格在「前 lookback 筆」高低區間的位置：>1 突破上緣、<0 跌破下緣（與 breakout 同樣先比較後納入）。"""
    buf: deque[float] = deque(maxlen=lookback)
    out = np.full(len(prices), np.nan)
    for i, p in enumerate(prices):
        if len(buf) == lookback:
            top, bot = max(buf), min(buf)
            if top > bot:
                out[i] = (p - bot) / (top - bot)
        buf.append(p)
    return out


def vwap_deviation(bars_of_day: Sequence[dict[str, Any]], warmup: int) -> np.ndarray:
    """收盤 - 當日 VWAP（與 VWAPRevertStrategy.on_bar 同算式：典型價×量累計、量最少算 1、暖機 warmup 根後才有值）。"""
    cum_pv = cum_v = 0.0
    out = []
    for i, b in enumerate(bars_of_day, start=1):
        v = max(b.get("volume") or 0, 1)
        cum_pv += (b["high"] + b["low"] + b["close"]) / 3 * v
        cum_v += v
        if i >= warmup:
            out.append(b["close"] - cum_pv / cum_v)
    return np.asarray(out)


# ── 量測 ─────────────────────────────────────────────────────────

def _row(group: str, name: str, rule: str, stream: str, unit: str, segments: Any, hi: float, lo: float | None, *,
         margin: float, whip_k: int, units: float, inclusive: bool = False) -> dict[str, Any]:
    st = edge_stats(segments, hi, lo, margin=margin, whip_k=whip_k, inclusive=inclusive, units=units)
    return {"group": group, "name": name, "rule": rule, "stream": stream, "unit": unit, "margin": margin, **st}


def study_daily(store: MarketStore | None = None) -> list[dict[str, Any]]:
    """日級的市場狀態判斷：Hurst、日 K 方向、振幅比、IV 百分位。"""
    store = store or MarketStore()
    rows: list[dict[str, Any]] = []
    daily = store.bars(CODE, 10_000)
    g = "市場狀態（日級）"
    if len(daily) >= MIN_DAILY:
        dates = [b["date"] for b in daily]
        close = np.array([b["close"] for b in daily], dtype=float)
        high = np.array([b["high"] for b in daily], dtype=float)
        low = np.array([b["low"] for b in daily], dtype=float)

        hs = np.array([h for _, h, _ in rolling_daily(dates, close.tolist(), 60)])
        if hs.size >= 10:
            rows.append(_row(g, "Hurst H（日K 60根）", f">{TREND_TH} 趨勢／<{REVERT_TH} 均值回歸", "日K", "日", hs,
                             TREND_TH, REVERT_TH, margin=_margin(TREND_TH, REVERT_TH, hs), whip_k=WHIP_DAILY, units=hs.size))

        sig = inspect.signature(trend_direction).parameters
        ma, band = int(sig["ma"].default), float(sig["band"].default)
        dev = ma_deviation(close, ma)
        if dev.size >= 10:
            rows.append(_row(g, f"日K方向（收盤 vs {ma}日均線）", f"偏離 ±{band:.1%} 內算貼近", "日K", "日", dev,
                             band, -band, margin=_margin(band, -band, dev), whip_k=WHIP_DAILY, units=dev.size,
                             inclusive=True))

        amp = amplitude_ratio(high, low)
        if amp.size >= 10:
            rows.append(_row(g, "日盤振幅比（vs 近20日均）", f"≥{BIG_MOVE} 大波動／≤{QUIET} 清淡", "日K", "日", amp,
                             BIG_MOVE, QUIET, margin=_margin(BIG_MOVE, QUIET, amp), whip_k=WHIP_DAILY, units=amp.size,
                             inclusive=True))

    with closing(store._conn()) as c:
        ivs = [r[0] for r in c.execute("SELECT iv FROM iv_history ORDER BY date").fetchall()]
    pct = iv_percentile_series(ivs)
    if pct.size >= 10:
        rows.append(_row(g, "IV 百分位", f"<{LOW_PCT:.0f} 偏低／>{HIGH_PCT:.0f} 偏高", "IV歷史", "日", pct,
                         HIGH_PCT, LOW_PCT, margin=_margin(HIGH_PCT, LOW_PCT, pct), whip_k=WHIP_DAILY, units=pct.size))
    else:
        rows.append({"group": g, "name": "IV 百分位", "rule": f"<{LOW_PCT:.0f} 偏低／>{HIGH_PCT:.0f} 偏高",
                     "stream": "IV歷史", "unit": "日", "insufficient": f"IV 歷史只有 {len(ivs)} 筆，"
                     f"至少要 {MIN_IV_POINTS + 10} 筆才算得出百分位序列"})
    return rows


def study_minutes(store: MarketStore | None = None) -> list[dict[str, Any]]:
    """以 1 分 K 為訊號的策略：vwap_revert 的 VWAP 偏離（orb 每方向每天只進一次，天然防抖，不量測）。"""
    store = store or MarketStore()
    bars = store.bars_1m(CODE)
    if not bars:
        return []
    s = VWAPRevertStrategy()
    by_day: dict[str, list[dict[str, Any]]] = {}
    for b in bars:
        by_day.setdefault(b["date"], []).append(b)
    segs = [d for d in (vwap_deviation(v, s.warmup_bars) for v in by_day.values()) if d.size]
    if not segs:
        return []
    return [_row("策略訊號（1分K）", "vwap_revert：收盤 - VWAP", f"≥+{s.dev_pts} 做空／≤-{s.dev_pts} 做多（平倉在 ±{s.dev_pts * s.exit_ratio:.0f}）",
                 "1分K", "日", segs, float(s.dev_pts), -float(s.dev_pts), margin=MARGIN_PCT * s.dev_pts, whip_k=WHIP_BAR,
                 units=len(segs), inclusive=True)]


def load_trades(path: str | Path | None = None) -> dict[str, np.ndarray]:
    """ticks.db 的逐筆成交（volume>0），依時間排序。唯讀開啟，服務執行中也能讀。"""
    p = Path(path or TICKS_DB)
    with closing(sqlite3.connect(f"file:{quote(str(p))}?mode=ro", uri=True, timeout=30)) as c:
        rows = c.execute("SELECT ts, code, price, tick_type FROM ticks WHERE volume > 0 ORDER BY ts").fetchall()
    ts = np.array([r[0] for r in rows], dtype=float)
    lt = [time.localtime(x) for x in ts]
    return {
        "ts": ts,
        "hhmm": np.array([x.tm_hour * 100 + x.tm_min for x in lt], dtype=np.int64),
        "prefix": np.array([str(r[1])[:3] for r in rows]),
        "is_tmf": np.array([str(r[1]).startswith("TMF") for r in rows], dtype=bool),
        "price": np.array([r[2] for r in rows], dtype=float),
        "tick_type": np.array([r[3] for r in rows], dtype=np.int64),
    }


def split_sessions(ts: np.ndarray, gap: float = SESSION_GAP) -> list[tuple[int, int]]:
    """依時間間隔切成連續的盤別，回傳 [(起, 迄)) 索引。"""
    if ts.size == 0:
        return []
    cuts = np.flatnonzero(np.diff(ts) > gap) + 1
    edges = [0, *cuts.tolist(), int(ts.size)]
    return list(zip(edges[:-1], edges[1:]))


def preopen_report(t: dict[str, np.ndarray]) -> dict[str, Any]:
    """開盤前試算時段的行情：筆數，以及同一分鐘內 TMF/MXF/TXF 中位價的最大差（點）。"""
    m = np.zeros(t["ts"].size, dtype=bool)
    for a, b in PREOPEN:
        m |= (t["hhmm"] >= a) & (t["hhmm"] < b)
    spreads: list[float] = []
    if m.any():
        minute = (t["ts"][m] // 60).astype(np.int64)
        for k in np.unique(minute):
            sel = minute == k
            meds = [float(np.median(t["price"][m][sel & (t["prefix"][m] == c)])) for c in ("TMF", "MXF", "TXF")
                    if (sel & (t["prefix"][m] == c)).any()]
            if len(meds) >= 2:
                spreads.append(max(meds) - min(meds))
    return {"events": int(m.sum()), "minutes": len(spreads),
            "mean_spread": float(np.mean(spreads)) if spreads else 0.0, "max_spread": float(max(spreads)) if spreads else 0.0}


def study_ticks(path: str | Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """逐筆成交上的指標。同時量「只看 TMF」與「TMF/MXF/TXF 三合約混合」兩種資料流——
    tick 策略實際吃的是三合約混合的事件流（見 update.md 發現 4）。

    兩個處理：① 開盤前試算時段（PREOPEN）的行情不是真實成交、三合約價差可達數百點，另外報告、不計入比較；
    ② 依盤別切段（間隔 > 30 分鐘），每段重新算指標（等於每個盤別重新啟動策略），避免跨盤的假跳動。
    回傳 (rows, 涵蓋範圍)。"""
    t = load_trades(path)
    info: dict[str, Any] = {"trades": int(t["ts"].size), "tmf": int(t["is_tmf"].sum()), "hours": 0.0,
                            "preopen": preopen_report(t) if t["ts"].size else {}}
    keep = np.ones(t["ts"].size, dtype=bool)
    for a_, b_ in PREOPEN:
        keep &= ~((t["hhmm"] >= a_) & (t["hhmm"] < b_))
    t = {k: v[keep] for k, v in t.items()}
    if t["ts"].size < 1000:
        return [], info
    slices = split_sessions(t["ts"])
    hours = sum(t["ts"][b_ - 1] - t["ts"][a_] for a_, b_ in slices) / 3600.0
    info["hours"] = hours
    info["sessions"] = len(slices)
    hours = max(hours, 1e-6)
    rows: list[dict[str, Any]] = []
    flow_w = FLOW_WINDOWS[1]
    for stream, mask in (("TMF", t["is_tmf"]), ("三合約混合", np.ones(t["ts"].size, dtype=bool))):
        px, tt = t["price"][mask], t["tick_type"][mask]
        sl = split_sessions(t["ts"][mask])

        def add(group: str, name: str, rule: str, fn: Any, hi: float, lo: float | None, inclusive: bool = False) -> None:
            segs = [fn(px[a_:b_], tt[a_:b_]) for a_, b_ in sl]                 # 每個盤別各算一段
            allx = np.concatenate(segs) if segs else np.empty(0)
            rows.append(_row(group, name, rule, stream, "小時", segs, hi, lo, margin=_margin(hi, lo, allx),
                             whip_k=WHIP_TICK, units=hours, inclusive=inclusive))

        if stream == "TMF":                                    # 即時面板只看 TMF
            add("市場狀態（即時，僅顯示）", f"外盤占比（最近 {flow_w} 筆成交）", f"≥{FLOW_UP} 買方主動／≤{FLOW_DOWN} 賣方主動",
                lambda p, y: flow_ratio(y, flow_w), FLOW_UP, FLOW_DOWN, True)
        g = "策略訊號（逐筆）"
        sc = ScalpStrategy()
        add(g, f"scalp：最近 {sc.momentum_window} 筆外盤占比", f"≥{sc.momentum_threshold} 做多／≤{1 - sc.momentum_threshold:.2f} 做空",
            lambda p, y: flow_ratio(y, sc.momentum_window), sc.momentum_threshold, 1 - sc.momentum_threshold, True)
        r = RSIStrategy()
        add(g, f"rsi：RSI({r.period})", f"<{r.oversold:.0f} 做多／>{r.overbought:.0f} 做空",
            lambda p, y: rsi_series(p, RSIStrategy()), r.overbought, r.oversold)
        b = BollingerStrategy()
        add(g, f"bollinger：價格距中軌 σ 數（{b.period} 筆）", f"跌破 -{b.num_std}σ 做多／突破 +{b.num_std}σ 做空",
            lambda p, y: bollinger_z(p, BollingerStrategy()), b.num_std, -b.num_std)
        m = MomentumStrategy()
        add(g, f"momentum：{m.period} 筆漲跌幅%", f"> +{m.threshold_pct}% 做多／< -{m.threshold_pct}% 做空",
            lambda p, y: momentum_pct(p, MomentumStrategy()), m.threshold_pct, -m.threshold_pct)
        a = MACrossStrategy()
        add(g, f"ma_cross：MA{a.short_period} - MA{a.long_period}", "快線在慢線上＝多、否則空（門檻 0）",
            lambda p, y: ma_gap(p, MACrossStrategy()), 0.0, None)
        k = BreakoutStrategy()
        add(g, f"breakout：價格在前 {k.lookback} 筆區間的位置", "突破上緣(>1)做多／跌破下緣(<0)做空",
            lambda p, y: range_position(p, k.lookback), 1.0, 0.0)
    return rows, info


def study(store: MarketStore | None = None, ticks_path: str | Path | None = None) -> dict[str, Any]:
    store = store or MarketStore()
    tick_rows, tick_info = study_ticks(ticks_path)
    return {"daily": study_daily(store), "minutes": study_minutes(store), "ticks": tick_rows, "tick_info": tick_info}


# ── 報告 ─────────────────────────────────────────────────────────

def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def _line(r: dict[str, Any], with_stream: bool) -> str:
    if r.get("insufficient"):
        return f"  {r['name']}（{r['rule']}）：資料不足——{r['insufficient']}"
    stream = f"[{r['stream']}] " if with_stream else ""
    return (f"  {stream}{r['name']}（{r['rule']}）\n"
            f"      樣本 {r['n']:,}｜切換 {r['flips']:,} 次 = 每{r['unit']} {r['per_unit']:.1f} 次｜來回 {_pct(r['whipsaw'])}｜"
            f"門檻±5%內 {_pct(r['near'])}｜加遲滯後 {r['flips_hyst']:,} 次（可省 {_pct(max(0.0, r['removed']))}）")


def format_report(res: dict[str, Any]) -> str:
    out = ["門檻判斷連續化檢查——指標在硬門檻附近的震盪（只讀本機資料）", ""]
    out.append("【日級：市場狀態判斷】")
    out += [_line(r, False) for r in res["daily"]] or ["  （資料不足）"]
    out.append("")
    out.append("【1 分 K 策略】")
    out += [_line(r, False) for r in res["minutes"]] or ["  （沒有 1 分 K 歷史）"]
    out.append("")
    info = res["tick_info"]
    out.append(f"【逐筆：即時狀態與 tick 策略】成交 {info['trades']:,} 筆（TMF {info['tmf']:,}）、"
               f"連續交易時段共 {info['hours']:.1f} 小時（{info.get('sessions', 0)} 個盤別，各自重算指標）")
    pre = info.get("preopen") or {}
    if pre.get("events"):
        out.append(f"  開盤前試算時段（{'、'.join(f'{a // 100:02d}:{a % 100:02d}~{b // 100:02d}:{b % 100:02d}' for a, b in PREOPEN)}）"
                   f"已排除：{pre['events']:,} 筆，同一分鐘內三合約中位價的差平均 {pre['mean_spread']:.0f} 點、最大 {pre['max_spread']:.0f} 點")
    for r in res["ticks"]:
        out.append(_line(r, True))
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for r in res["ticks"]:
        pairs.setdefault(r["name"], {})[r["stream"]] = r
    mixed = [(n, d["三合約混合"]["flips"] / d["TMF"]["flips"]) for n, d in pairs.items()
             if "TMF" in d and "三合約混合" in d and d["TMF"]["flips"]]
    if mixed:
        out.append("")
        out.append("三合約混合 vs 只看 TMF 的切換次數倍數（>1 ＝ 合約交錯讓門檻更常被誤觸）：")
        out += [f"  {n}：{x:.1f}×" for n, x in mixed]
    return "\n".join(out)


def _main() -> None:  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="門檻判斷連續化檢查（只讀本機資料）")
    ap.add_argument("--ticks", default=None, help="ticks.db 路徑（預設 data/ticks.db）")
    a = ap.parse_args()
    print(format_report(study(ticks_path=a.ticks)))


if __name__ == "__main__":  # pragma: no cover
    _main()
