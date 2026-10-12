"""回測工具：用「真實的策略類別」在 1 分 K 上跑參數網格，並做樣本內／樣本外、滾動式（walk-forward）與「沒有技巧」對照。

為什麼不重寫策略邏輯：重寫一份很容易跟正式程式悄悄不一致，回測結果就不能信。這裡直接驅動 strategies/ 裡的類別，
連風控（可開倉時段、當日虧損上限、進場次數）都走真的 place_order，只把送單到券商那一步換成空操作。

怎麼餵資料：
  - K 棒策略（orb、vwap_revert）：每根 1 分 K 呼叫 _on_bar_async。
  - 逐 tick 策略（ma_cross、breakout、rsi、bollinger、momentum）：把每根 K 棒的收盤價當一個 tick 餵進去＝「分鐘版」，
    參數單位變成分鐘，**不是**正式環境的 tick 版（tick 版每小時切換上千次，見 update.md 發現 11，只會更糟）。
  - 盤中停損停利用 K 棒的高低點判斷，先看不利方向（同一根兩個價位都碰到算停損先到），成交在價位上。
  - 進場價 = 訊號 K 棒的收盤；每筆來回成本 --cost 點（手續費＋稅＋滑價，預設 4 點是**假設**，請用自己的）；收盤前強制平倉（日內單）。

用法（只讀本機資料庫，不連券商）：
    python -m core.backtest --strategy orb                     # 內建參數網格，樣本內／外各一欄
    python -m core.backtest --strategy all --null 6            # 全部策略＋6 組「打亂資料」的對照
    python -m core.backtest --strategy vwap_revert --walk-forward 120,20
    python -m core.backtest --strategy orb --grid my_grid.json --cost 3 --json out.json
    python -m core.backtest --strategy ma_cross --source flow --session night   # 用 flow_1m（TMF，含夜盤）

--grid 檔案格式（一個物件或物件的陣列）：
    {"strategy": "orb",
     "params": {"or_minutes": [15, 30], "buffer_pts": [5, 20]},          # 策略專屬參數，各軸取笛卡兒積
     "base":   {"stop_loss_pts": [50, 100], "atr_trail_mult": [0, 3]}}    # 直接設在策略物件上的屬性
    params／base 也可以寫成「物件的陣列」＝一組一組明列（參數有搭配關係時用，例如 [{"short_period":5,"long_period":20}, …]）。

怎麼讀結果（最容易誤判的地方）：
  - 挑 N 組參數裡「樣本內最好的」，它的樣本內成績本來就會被高估；一定要看樣本外，以及「沒有技巧的打亂資料」同樣流程挑出來的最佳組合長什麼樣。
  - t 值（以「天」為單位）要 |t| > 2 才算有一點證據；挑了很多組時要更高。
  - 報告最後會寫出「以這份資料的天數，每天平均邊際大於多少點才看得出來」——比成本小的優勢，這份資料看不出來。
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import math
import os
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np

DEFAULT_COST = 4.0
STRATEGIES = ("ma_cross", "breakout", "rsi", "bollinger", "momentum", "orb", "vwap_revert")
# 這些在策略物件上是普通屬性（正式環境由 start() 設定），不走 _apply_params；寫在 params 或 base 都要真的生效，不能被靜默忽略
BASE_KEYS = ("stop_loss_pts", "take_profit_pts", "daily_max_loss", "max_trades_per_day", "trade_start_hhmm", "trade_end_hhmm",
             "atr_period", "atr_trail_mult")
SESSION_GAP_SEC = 1800                  # flow_1m 來源：兩根 K 棒相隔超過 30 分鐘就算不同時段（日盤／夜盤）

Days = dict[str, list[tuple]]           # 日期（或時段）→ [(ts_start, open, high, low, close, volume), …]


# ── 參數組 ─────────────────────────────────────────────────────────

@dataclass
class Combo:
    strategy: str
    params: dict[str, Any] = field(default_factory=dict)      # 策略專屬參數（走 _apply_params）
    base: dict[str, Any] = field(default_factory=dict)        # 直接設在策略物件上的屬性（stop_loss_pts、atr_trail_mult…）

    @property
    def label(self) -> str:
        parts = [f"{k}={v}" for k, v in {**self.params, **self.base}.items()]
        return f"{self.strategy} " + " ".join(parts)


def _make(name: str):
    if name == "ma_cross":
        from strategies.ma_cross import MACrossStrategy as C
    elif name == "breakout":
        from strategies.breakout import BreakoutStrategy as C
    elif name == "rsi":
        from strategies.rsi import RSIStrategy as C
    elif name == "bollinger":
        from strategies.bollinger import BollingerStrategy as C
    elif name == "momentum":
        from strategies.momentum import MomentumStrategy as C
    elif name == "orb":
        from strategies.orb import ORBStrategy as C
    elif name == "vwap_revert":
        from strategies.vwap_revert import VWAPRevertStrategy as C
    else:
        raise ValueError(f"不支援的策略：{name}（可用：{', '.join(STRATEGIES)}；scalp 是限價單狀態機，沒辦法用 K 棒回測）")
    return C()


def validate_combo(combo: Combo) -> None:
    """參數名稱打錯會被 _apply_params 靜默忽略（用預設值跑），回測結果就是錯的——先擋下來。"""
    s = _make(combo.strategy)
    known = set(s.params)
    for k in combo.params:
        if k not in known:
            raise ValueError(f"{combo.strategy} 沒有參數 {k!r}（可用：{', '.join(sorted(known))}）")
    for k in combo.base:
        if not hasattr(s, k):
            raise ValueError(f"{combo.strategy} 沒有屬性 {k!r}")


def _alternatives(spec: Any) -> list[dict[str, Any]]:
    """dict of lists → 笛卡兒積；list of dicts → 照列；None → [{}]。"""
    if spec is None:
        return [{}]
    if isinstance(spec, list):
        return [dict(d) for d in spec] or [{}]
    keys = list(spec)
    axes = [v if isinstance(v, (list, tuple)) else [v] for v in spec.values()]
    return [dict(zip(keys, vals)) for vals in itertools.product(*axes)] or [{}]


def expand_grid(strategy: str, params: Any = None, base: Any = None) -> list[Combo]:
    out = [Combo(strategy, p, b) for p in _alternatives(params) for b in _alternatives(base)]
    for c in out:
        validate_combo(c)
    return out


def preset_grid(name: str) -> list[Combo]:
    """內建網格：每個策略 10～48 組，涵蓋文件建議值附近的合理範圍（刻意不大，挑得越多組，樣本內最佳的水分越多）。"""
    tp0 = {"take_profit_pts": [0]}
    if name == "orb":
        return expand_grid(name, {"or_minutes": [15, 30, 60], "buffer_pts": [5, 20]},
                           {**tp0, "stop_loss_pts": [50, 100, 200], "atr_trail_mult": [0.0, 3.0]})
    if name == "vwap_revert":
        return expand_grid(name, {"dev_pts": [30, 60, 100, 150], "exit_ratio": [0.25, 0.5]},
                           {**tp0, "stop_loss_pts": [60, 150, 300], "atr_trail_mult": [0.0, 3.0]})
    if name == "ma_cross":
        pairs = [{"short_period": s, "long_period": l} for s, l in ((3, 10), (5, 20), (10, 30), (10, 60), (20, 60), (30, 120))]
        return expand_grid(name, pairs, {**tp0, "stop_loss_pts": [0, 100]})
    if name == "breakout":
        return expand_grid(name, {"lookback": [10, 20, 30, 60, 120]}, {**tp0, "stop_loss_pts": [0, 100]})
    if name == "rsi":
        alts = [{"period": p, "oversold": lo, "overbought": hi} for p in (7, 14, 28) for lo, hi in ((30, 70), (20, 80))]
        return expand_grid(name, alts, {**tp0, "stop_loss_pts": [0, 100]})
    if name == "bollinger":
        return expand_grid(name, {"period": [20, 40, 60], "num_std": [2.0, 2.5]}, {**tp0, "stop_loss_pts": [0, 100]})
    if name == "momentum":
        return expand_grid(name, {"period": [5, 15, 30, 60], "threshold_pct": [0.05, 0.1, 0.2]}, {**tp0, "stop_loss_pts": [0, 100]})
    raise ValueError(f"沒有內建網格：{name}")


def load_grid_file(path: str) -> list[Combo]:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Combo] = []
    for item in spec if isinstance(spec, list) else [spec]:
        out += expand_grid(item["strategy"], item.get("params"), item.get("base"))
    return out


# ── 資料 ──────────────────────────────────────────────────────────

def load_days(db_path: Optional[str] = None, source: str = "bars", code: Optional[str] = None,
              session: str = "day", start: Optional[str] = None, end: Optional[str] = None) -> Days:
    """讀本機 market_state.db 的 1 分 K。

    source="bars"：bars_1m（券商 kbars，預設合約 TXF，只有日盤；hhmm 是 K 棒**結束**時間，這裡換成起始時間）。
    source="flow"：flow_1m（TMF 真實成交彙總，日盤＋夜盤；用相隔 30 分鐘以上切成一個個時段，key 以 D／N 結尾）。
    start／end：含頭含尾的日期（YYYY-MM-DD）。"""
    from core.market_store import MarketStore
    ms = MarketStore(db_path)
    days: Days = {}
    if source == "bars":
        if session not in ("day", "all"):
            raise ValueError("bars_1m 只有日盤；要夜盤請用 --source flow")
        for r in ms.bars_1m(code or "TXF"):
            end_t = datetime.strptime(f"{r['date']} {r['hhmm'] // 100:02d}:{r['hhmm'] % 100:02d}", "%Y-%m-%d %H:%M")
            days.setdefault(r["date"], []).append(
                ((end_t - timedelta(minutes=1)).timestamp(), float(r["open"]), float(r["high"]), float(r["low"]),
                 float(r["close"]), int(r["volume"] or 0)))
    elif source == "flow":
        rows = sorted(ms.flow_1m(code or "TMF"), key=lambda r: r["ts"])
        seg: list[dict] = []
        segments: list[list[dict]] = []
        for r in rows:
            if seg and r["ts"] - seg[-1]["ts"] > SESSION_GAP_SEC:
                segments.append(seg)
                seg = []
            seg.append(r)
        if seg:
            segments.append(seg)
        for sg in segments:
            t0 = datetime.fromtimestamp(sg[0]["ts"])
            hhmm = t0.hour * 100 + t0.minute
            night = hhmm >= 1500 or hhmm < 500
            if (session == "day" and night) or (session == "night" and not night):
                continue
            base_key = f"{t0:%Y-%m-%d}{'N' if night else 'D'}"
            key, i = base_key, 2
            while key in days:                         # 同一天內資料有缺口時會切出不只一段，key 不能互相蓋掉
                key, i = f"{base_key}{i}", i + 1
            days[key] = [
                (float(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                 int(r["buy_vol"] + r["sell_vol"] + r["unk_vol"])) for r in sg]
    else:
        raise ValueError(f"未知的資料來源：{source}（bars 或 flow）")
    keep = {d: b for d, b in days.items() if b and (not start or d[:10] >= start) and (not end or d[:10] <= end)}
    return dict(sorted(keep.items()))


def surrogate_days(days: Days, seed: int) -> Days:
    """沒有技巧的對照資料：打亂每天 K 棒的「形狀」（開盤跳空、高低、收盤相對開盤）順序後重建價格路徑。
    保留波動大小、成交量的時段分布與每天的根數，但破壞序列相關（趨勢、均值回歸）；這份資料上的任何策略都沒有預測力。"""
    rng = random.Random(seed)
    out: Days = {}
    for d, bars in days.items():
        shapes, prev_c = [], bars[0][1]
        for ts, o, h, l, c, v in bars:
            shapes.append((o - prev_c, h - o, l - o, c - o))
            prev_c = c
        rng.shuffle(shapes)
        px, nb = bars[0][1], []
        for (ts, o, h, l, c, v), (gap, dh, dl, dc) in zip(bars, shapes):
            no = px + gap
            nb.append((ts, no, no + dh, no + dl, no + dc, v))
            px = no + dc
        out[d] = nb
    return out


# ── 引擎 ──────────────────────────────────────────────────────────

def _patch_enter():
    """把「送單到券商」換成空操作、關掉 logging；風控（_risk_ok）照真的跑。回傳還原函式。"""
    import strategies.base as base
    from core.broker import broker
    prev_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    had = "place_order" in vars(broker)
    old_inst = vars(broker).get("place_order")
    old_margin = base.BaseStrategy._margin_ok

    async def _noop_place(**kw):
        return {"trade_id": "bt", "status": "Filled"}

    async def _margin_ok(self):
        return True

    broker.place_order = _noop_place
    base.BaseStrategy._margin_ok = _margin_ok

    def undo():
        if had:
            broker.place_order = old_inst
        else:
            vars(broker).pop("place_order", None)
        base.BaseStrategy._margin_ok = old_margin
        logging.disable(prev_disable)
    return undo


@contextmanager
def patched():
    undo = _patch_enter()
    try:
        yield
    finally:
        undo()


async def _intrabar(s, high: float, low: float) -> None:
    """K 棒內的停損停利：只看價位有沒有被碰到；兩個都碰到算停損先到；成交價 = 價位本身。"""
    pos = s.state.position
    if pos == 0:
        return
    e, sl, tp = s.state.entry_price, s.stop_loss_pts, s.take_profit_pts
    if pos > 0:
        if sl > 0 and low <= e - sl:
            await s._check_sl_tp(e - sl)
        elif tp > 0 and high >= e + tp:
            await s._check_sl_tp(e + tp)
    else:
        if sl > 0 and high >= e + sl:
            await s._check_sl_tp(e + sl)
        elif tp > 0 and low <= e - tp:
            await s._check_sl_tp(e - tp)


def build_strategy(combo: Combo):
    """依參數組建出設定好的策略物件。"""
    s = _make(combo.strategy)
    merged = {**combo.params, **combo.base}
    for k, v in merged.items():
        if k in BASE_KEYS or k in combo.base:
            setattr(s, k, v)
    s._apply_params({k: v for k, v in merged.items() if k not in BASE_KEYS and k not in combo.base})
    return s


async def simulate_day(combo: Combo, bars: list[tuple]) -> list[float]:
    """跑一天，回傳每筆已平倉的「毛損益（點，未扣成本）」。每天用全新的策略物件（＝每天早上重新啟動）。"""
    from core.bar_builder import Bar
    from strategies.bar_base import BarStrategy
    s = build_strategy(combo)
    holder = {"now": datetime.fromtimestamp(bars[0][0])}
    s._now = lambda: holder["now"]
    s._roll_risk_day()
    trades: list[float] = []
    orig = s._add_realized

    def rec(amount: float) -> None:
        trades.append(amount / s.point_value)
        orig(amount)

    s._add_realized = rec
    is_bar = isinstance(s, BarStrategy)
    for ts, o, h, l, c, v in bars:
        holder["now"] = datetime.fromtimestamp(ts + 60)            # 下單發生在 K 棒收完的時候
        await _intrabar(s, h, l)
        if is_bar:
            await s._on_bar_async(Bar("TMFJ6", int(ts), o, h, l, c, int(v)))
        else:
            await s._on_quote_async({"code": "TMFJ6", "close": c, "tick_type": 0, "volume": 0, "total_volume": 0})
    if s.state.position != 0:                                       # 收盤強制平倉
        trades.append((bars[-1][4] - s.state.entry_price) * (1 if s.state.position > 0 else -1))
    return trades


@dataclass
class Result:
    combo: Combo
    days: list[str]
    n: np.ndarray            # 每天已平倉筆數
    net: np.ndarray          # 每天扣成本後的點數損益
    wins: np.ndarray         # 每天獲利筆數（扣成本後）


def _run_combo(combo: Combo, days: Days, cost: float) -> Result:
    async def go():
        n, net, wins = [], [], []
        for d in days:
            t = await simulate_day(combo, days[d])
            x = [g - cost for g in t]
            n.append(len(t))
            net.append(float(sum(x)))
            wins.append(sum(1 for v in x if v > 0))
        return n, net, wins
    n, net, wins = asyncio.run(go())
    return Result(combo, list(days), np.array(n, float), np.array(net, float), np.array(wins, float))


_POOL: dict[str, Any] = {}


def _pool_init(days: Days, cost: float) -> None:
    _POOL["days"], _POOL["cost"] = days, cost
    _patch_enter()                                       # 子行程跑完就結束，不需要還原


def _pool_task(combo: Combo) -> Result:
    return _run_combo(combo, _POOL["days"], _POOL["cost"])


def auto_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def run_grid(combos: list[Combo], days: Days, cost: float = DEFAULT_COST, workers: int = 1) -> list[Result]:
    if not days:
        raise ValueError("沒有資料可回測（日期範圍內沒有 K 棒）")
    if workers <= 1 or len(combos) < 4:
        with patched():
            return [_run_combo(c, days, cost) for c in combos]
    with ProcessPoolExecutor(max_workers=workers, initializer=_pool_init, initargs=(days, cost)) as ex:
        return list(ex.map(_pool_task, combos, chunksize=2))


# ── 統計 ──────────────────────────────────────────────────────────

def t_stat(x: np.ndarray) -> float:
    if len(x) < 2:
        return 0.0
    sd = float(np.std(x, ddof=1))
    return float(np.mean(x) / (sd / math.sqrt(len(x)))) if sd > 0 else 0.0


def split_index(n_days: int, ratio: float) -> int:
    return max(1, min(n_days - 1, int(n_days * ratio)))


def mde(sd_per_day: float, n_days: int, z: float = 2.8) -> float:
    """80% 檢定力、雙尾 5% 下，這份資料能看出的最小「每天平均邊際」（點）。"""
    return z * sd_per_day / math.sqrt(max(n_days, 1))


def window_stats(res: Result, lo: int, hi: int, cost: float = DEFAULT_COST) -> dict[str, float]:
    n, x, w = res.n[lo:hi], res.net[lo:hi], res.wins[lo:hi]
    trades = float(n.sum())
    return {"days": float(len(x)), "trades": trades, "perday": float(x.mean()) if len(x) else 0.0,
            "sd": float(np.std(x, ddof=1)) if len(x) > 1 else 0.0, "t": t_stat(x),
            "pertrade": float(x.sum() / trades) if trades else 0.0,
            "gross_pertrade": float((x.sum() + cost * trades) / trades) if trades else 0.0,
            "winrate": float(w.sum() / trades) if trades else 0.0}


@dataclass
class Row:
    combo: Combo
    is_: dict[str, float]
    oos: dict[str, float]
    all: dict[str, float]


def summarize(results: list[Result], ratio: float = 0.6, cost: float = DEFAULT_COST) -> list[Row]:
    nd = len(results[0].days)
    cut = split_index(nd, ratio)
    return [Row(r.combo, window_stats(r, 0, cut, cost), window_stats(r, cut, nd, cost), window_stats(r, 0, nd, cost))
            for r in results]


def walk_forward(results: list[Result], train: int, test: int, metric: str = "t") -> dict[str, Any]:
    """滾動式驗證：每個視窗用前 train 天挑出最好的參數組，拿去跑接下來 test 天（這 test 天不參與挑選）。
    把所有「接下來 test 天」接起來就是誠實的樣本外成績——包含了「挑參數」這個動作本身的運氣。"""
    nd = len(results[0].days)
    if train < 5 or test < 1 or train + test > nd:
        raise ValueError(f"walk-forward 視窗不合理：train={train}、test={test}、資料只有 {nd} 天")
    score = (lambda x: t_stat(x)) if metric == "t" else (lambda x: float(np.mean(x)))
    series, ns, picks = [], [], []
    start = train
    while start < nd:
        end = min(start + test, nd)
        best = max(range(len(results)), key=lambda i: score(results[i].net[start - train:start]))
        series.append(results[best].net[start:end])
        ns.append(results[best].n[start:end])
        picks.append((results[best].days[start], results[best].combo.label))
        start = end
    oos = np.concatenate(series)
    return {"days": int(len(oos)), "perday": float(oos.mean()), "t": t_stat(oos), "trades": float(np.concatenate(ns).sum()),
            "positive_windows": sum(1 for s in series if s.sum() > 0), "windows": len(series),
            "distinct_picks": len({p[1] for p in picks}), "picks": picks}


def null_calibration(combos: list[Combo], days: Days, cost: float, n_sur: int, ratio: float = 0.6,
                     walk: Optional[tuple[int, int]] = None, workers: int = 1, seed0: int = 1000) -> list[dict[str, float]]:
    """在「沒有技巧」的打亂資料上跑同樣的流程：挑出樣本內最佳組合，看它的樣本外；（可選）也跑 walk-forward。"""
    out = []
    for k in range(n_sur):
        res = run_grid(combos, surrogate_days(days, seed0 + k), cost, workers)
        rows = summarize(res, ratio, cost)
        best = max(rows, key=lambda r: r.is_["t"])
        item = {"best_is_t": best.is_["t"], "oos_t_of_best": best.oos["t"], "best_all_t": max(r.all["t"] for r in rows)}
        if walk:
            wf = walk_forward(res, *walk)
            item["wf_t"], item["wf_perday"] = wf["t"], wf["perday"]
        out.append(item)
    return out


# ── 報告 ──────────────────────────────────────────────────────────

def analyze(strategy: str, combos: list[Combo], days: Days, cost: float, ratio: float, n_sur: int,
            walk: Optional[tuple[int, int]], workers: int) -> dict[str, Any]:
    results = run_grid(combos, days, cost, workers)
    rows = summarize(results, ratio, cost)
    nd = len(days)
    keys = list(days)
    cut = split_index(nd, ratio)
    out: dict[str, Any] = {
        "strategy": strategy, "combos": len(combos), "days": nd, "first": keys[0], "last": keys[-1],
        "split": {"is_days": cut, "oos_days": nd - cut, "oos_from": keys[cut]}, "cost": cost,
        "median_gross_pertrade": float(np.median([r.all["gross_pertrade"] for r in rows])),
        "median_net_pertrade": float(np.median([r.all["pertrade"] for r in rows])),
        "positive_all": sum(r.all["perday"] > 0 for r in rows),
        "is_positive": sum(r.is_["perday"] > 0 for r in rows),
        "is_positive_oos_positive": sum(r.is_["perday"] > 0 and r.oos["perday"] > 0 for r in rows),
        "best_all_t": max(r.all["t"] for r in rows),
        "mde_perday": mde(float(np.median([r.all["sd"] for r in rows])), nd),
        "rows": [{"label": r.combo.label, "is": r.is_, "oos": r.oos, "all": r.all} for r in rows],
    }
    out["best_is_t"] = max(r.is_["t"] for r in rows)
    if walk:
        wf = walk_forward(results, *walk)
        out["walk_forward"] = {k: v for k, v in wf.items() if k != "picks"} | {"train": walk[0], "test": walk[1],
                                                                                 "picks": [list(p) for p in wf["picks"]]}
    if n_sur:
        out["null"] = null_calibration(combos, days, cost, n_sur, ratio, walk, workers)
    return out


def format_report(a: dict[str, Any], top: int = 3) -> str:
    L: list[str] = []
    sp = a["split"]
    L.append(f"══ {a['strategy']}：{a['combos']} 組參數，{a['days']} 天（{a['first']} → {a['last']}），每筆成本 {a['cost']:g} 點 ══")
    L.append(f"樣本內 {sp['is_days']} 天、樣本外 {sp['oos_days']} 天（{sp['oos_from']} 起）")
    L.append(f"扣成本前每筆損益（各組中位數）{a['median_gross_pertrade']:+.1f} 點；扣成本後 {a['median_net_pertrade']:+.1f} 點")
    L.append(f"全期淨損益為正的組數 {a['positive_all']}/{a['combos']}；樣本內為正的 {a['is_positive']} 組裡，樣本外仍為正的 {a['is_positive_oos_positive']} 組；"
             f"全期最佳 t={a['best_all_t']:+.2f}")
    L.append(f"樣本內最好的 {top} 組（t 值是以「天」為單位）：")
    L.append(f"  {'參數':<64}{'筆/天':>6}{'勝率':>6} | {'樣本內 點/天':>11}{'t':>7} | {'樣本外 點/天':>11}{'t':>7} | 全期每筆")
    nd = a["days"]
    for r in sorted(a["rows"], key=lambda r: -r["is"]["t"])[:top]:
        L.append(f"  {r['label']:<64}{r['all']['trades'] / nd:>6.1f}{r['all']['winrate']:>6.0%} | "
                 f"{r['is']['perday']:>+11.1f}{r['is']['t']:>+7.2f} | {r['oos']['perday']:>+11.1f}{r['oos']['t']:>+7.2f} | {r['all']['pertrade']:>+7.1f}")
    wf = a.get("walk_forward")
    if wf:
        L.append(f"滾動式驗證（每次用前 {wf['train']} 天挑最好的、跑接下來 {wf['test']} 天）：樣本外 {wf['days']} 天，"
                 f"{wf['perday']:+.1f} 點/天（t={wf['t']:+.2f}），{wf['trades'] / max(wf['days'], 1):.1f} 筆/天；"
                 f"{wf['positive_windows']}/{wf['windows']} 個視窗賺錢；挑過 {wf['distinct_picks']} 種不同的組合")
    nu = a.get("null")
    if nu:
        bt = [x["best_is_t"] for x in nu]
        oo = [x["oos_t_of_best"] for x in nu]
        L.append(f"沒有技巧的對照（{len(nu)} 組打亂資料、同樣流程）：樣本內最佳 t 介於 {min(bt):+.1f} ~ {max(bt):+.1f}（真實資料 {a['best_is_t']:+.2f}），"
                 f"那些最佳組合到樣本外 t 平均 {float(np.mean(oo)):+.2f}")
        if wf and "wf_t" in nu[0]:
            wt = [x["wf_t"] for x in nu]
            L.append(f"  滾動式驗證的對照：打亂資料上樣本外 t 介於 {min(wt):+.1f} ~ {max(wt):+.1f}（真實資料 {wf['t']:+.2f}）")
    L.append(f"偵測力：日損益標準差約 {a['mde_perday'] / 2.8 * math.sqrt(nd):.0f} 點，{nd} 天只能看出每天平均 +{a['mde_perday']:.0f} 點（1 點＝10 元）以上的邊際")
    return "\n".join(L)


# ── CLI ───────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.backtest", description="用真實策略類別在 1 分 K 上回測參數網格（只讀本機資料庫）")
    ap.add_argument("--strategy", default="all", help=f"{'|'.join(STRATEGIES)}|all（預設 all）")
    ap.add_argument("--grid", help="自訂參數網格的 JSON 檔（見模組說明）；不給就用內建網格")
    ap.add_argument("--cost", type=float, default=DEFAULT_COST, help="每筆來回成本（點）。預設 4 是假設，請用自己的")
    ap.add_argument("--source", choices=("bars", "flow"), default="bars", help="bars＝bars_1m（日盤）；flow＝flow_1m（TMF，含夜盤）")
    ap.add_argument("--session", choices=("day", "night", "all"), default="day")
    ap.add_argument("--code", help="合約代碼（bars 預設 TXF、flow 預設 TMF）")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", help="結束日期 YYYY-MM-DD")
    ap.add_argument("--db", help="market_state.db 路徑（預設 data/market_state.db）")
    ap.add_argument("--split", type=float, default=0.6, help="樣本內占的比例（預設 0.6，依時間先後切）")
    ap.add_argument("--null", type=int, default=0, help="用幾組打亂資料做『沒有技巧』的對照（建議 6 以上）")
    ap.add_argument("--walk-forward", help="TRAIN,TEST 天數，例如 120,20")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--workers", type=int, default=0, help="平行行程數；0＝自動")
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    args = ap.parse_args(argv)

    try:
        if args.grid:
            combos = load_grid_file(args.grid)
        else:
            names = STRATEGIES if args.strategy == "all" else (args.strategy,)
            combos = [c for n in names for c in preset_grid(n)]
        if args.strategy != "all":
            combos = [c for c in combos if c.strategy == args.strategy] if not args.grid else combos
        walk = tuple(int(x) for x in args.walk_forward.split(",")) if args.walk_forward else None
        if walk and len(walk) != 2:
            raise ValueError("--walk-forward 要寫成 TRAIN,TEST，例如 120,20")
        days = load_days(args.db, args.source, args.code, args.session, args.start, args.end)
    except (ValueError, KeyError, OSError) as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 2
    if not days:
        print("錯誤：日期範圍內沒有 K 棒", file=sys.stderr)
        return 2
    workers = args.workers or auto_workers()
    by: dict[str, list[Combo]] = {}
    for c in combos:
        by.setdefault(c.strategy, []).append(c)
    reports = []
    for name, cs in by.items():
        try:
            a = analyze(name, cs, days, args.cost, args.split, args.null, walk, workers)
        except ValueError as e:
            print(f"錯誤：{e}", file=sys.stderr)
            return 2
        reports.append(a)
        print(format_report(a, args.top))
        print()
    if args.json:
        Path(args.json).write_text(json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完整結果已寫入 {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
