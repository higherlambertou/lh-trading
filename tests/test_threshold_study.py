"""core/threshold_study.py：硬門檻附近的震盪量測（門檻判斷連續化檢查）。"""
from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta

import numpy as np
import pytest

import core.threshold_study as th
from core.iv_monitor import iv_percentile
from core.market_store import MarketStore
from strategies.bollinger import BollingerStrategy
from strategies.ma_cross import MACrossStrategy
from strategies.momentum import MomentumStrategy
from strategies.rsi import RSIStrategy


# ── 分類與遲滯 ────────────────────────────────────────────────────

def test_hard_labels_three_state_two_state_and_inclusive():
    x = [0.3, 0.45, 0.5, 0.55, 0.7]
    assert th.label_hard(x, 0.55, 0.45).tolist() == [-1, 0, 0, 0, 1]                 # 等於門檻不算（> / <）
    assert th.label_hard(x, 0.55, 0.45, inclusive=True).tolist() == [-1, -1, 0, 1, 1]  # >= / <=（如 scalp）
    assert th.label_hard([-1, 0, 1], 0.0).tolist() == [-1, -1, 1]                    # 單一門檻只有兩態，等於算空


def test_hysteresis_needs_to_clear_the_margin_to_enter_and_to_leave():
    # 門檻 0.55/0.45、遲滯帶 0.025：進 +1 要 > 0.575，離開要 < 0.525
    y = th.label_hyst([0.6, 0.54, 0.52, 0.56, 0.58, 0.43, 0.4], 0.55, 0.45, 0.025)
    assert y.tolist() == [1, 1, 0, 0, 1, 0, -1]
    # 已在 +1 時一口氣跌破 lo-margin，直接跳到 -1（不必經過 0）
    assert th.label_hyst([0.6, 0.3], 0.55, 0.45, 0.025).tolist() == [1, -1]


def test_hysteresis_on_a_single_threshold_ignores_small_wiggles():
    x = [0.2, -0.2, 0.2, -0.2]
    assert th.label_hard(x, 0.0).tolist() == [1, -1, 1, -1]
    assert th.label_hyst(x, 0.0, None, 0.0).tolist() == [1, -1, 1, -1]               # 沒有遲滯帶＝跟硬門檻一樣
    assert th.label_hyst(x, 0.0, None, 0.5).tolist() == [1, 1, 1, 1]                 # 有遲滯帶就不會被小幅來回洗掉


# ── 震盪統計 ──────────────────────────────────────────────────────

def test_edge_stats_flags_a_series_that_chatters_around_the_threshold():
    x = np.array([0.549, 0.551] * 50)                                                # 0.549 與 0.551 本質上一樣，硬門檻卻天天翻
    s = th.edge_stats(x, 0.55, 0.45, margin=0.025, whip_k=3)
    assert s["flips"] == 99 and s["flips_hyst"] == 0
    assert s["near"] == 1.0 and s["whipsaw"] > 0.95 and s["removed"] == 1.0


def test_edge_stats_does_not_penalise_clean_regime_changes():
    x = np.array([0.3] * 20 + [0.7] * 20 + [0.3] * 20)
    s = th.edge_stats(x, 0.55, 0.45, margin=0.025, whip_k=3)
    assert s["flips"] == 2 and s["flips_hyst"] == 2 and s["whipsaw"] == 0.0 and s["near"] == 0.0 and s["removed"] == 0.0


def test_segments_do_not_count_flips_across_gaps_and_nan_is_skipped():
    a, b = np.array([1.0, 1.0]), np.array([-1.0, -1.0])
    assert th.edge_stats([a, b], 0.0, margin=0.0)["flips"] == 0                      # 段與段之間（如不同盤別）不算切換
    assert th.edge_stats(np.concatenate([a, b]), 0.0, margin=0.0)["flips"] == 1
    with_nan = np.array([np.nan, 1.0, np.nan, -1.0, 1.0])
    s = th.edge_stats(with_nan, 0.0, margin=0.0)
    assert s["n"] == 3 and s["flips"] == 2


def test_per_unit_rates_and_empty_input():
    s = th.edge_stats(np.array([1.0, -1.0, 1.0, -1.0, 1.0]), 0.0, margin=0.0, units=2.0)
    assert s["flips"] == 4 and s["per_unit"] == 2.0 and s["dwell"] == pytest.approx(1.25)
    e = th.edge_stats(np.empty(0), 0.0, margin=0.0)
    assert e["n"] == 0 and e["flips"] == 0 and e["dwell"] is None


def test_margin_is_five_percent_of_threshold_or_of_series_std_near_zero():
    assert th._margin(0.55, 0.45) == pytest.approx(0.025)
    assert th._margin(70.0, 30.0) == pytest.approx(2.5)
    x = np.random.default_rng(0).normal(0, 4.0, 500)
    assert th._margin(0.0, None, x) == pytest.approx(0.05 * x.std())                 # 門檻在 0：改用指標標準差
    assert th._margin(0.001, -0.001, np.random.default_rng(1).normal(0, 0.02, 500)) > 0.0005


# ── 指標序列 ──────────────────────────────────────────────────────

def test_flow_ratio_counts_only_directional_events():
    # 有方向的事件 [1,1,2,2,1]，窗口 2 → 外盤占比 1, .5, 0, .5；0 與 3 略過
    assert th.flow_ratio([1, 1, 2, 0, 2, 3, 1], 2).tolist() == [1.0, 0.5, 0.0, 0.5]
    assert th.flow_ratio([1, 2], 5).size == 0


def test_daily_series_helpers():
    close = [10, 11, 12, 13, 14]
    assert th.ma_deviation(close, 3).tolist() == pytest.approx([12 / 11 - 1, 13 / 12 - 1, 14 / 13 - 1])
    assert th.ma_deviation(close, 9).size == 0
    high, low = [12, 12, 12, 12, 12, 12, 24], [10, 10, 10, 10, 10, 10, 10]           # 前 6 天振幅 2、第 7 天振幅 14
    assert th.amplitude_ratio(high, low, lookback=20, min_prev=5)[-1] == pytest.approx(7.0)


def test_iv_percentile_series_uses_the_same_percentile_as_the_system():
    ivs = [15 + (i * 7 % 11) for i in range(60)]
    got = th.iv_percentile_series(ivs, lookback=252, min_hist=30)
    assert got.size == 30
    assert got[-1] == pytest.approx(iv_percentile(ivs[-1], ivs[:-1][::-1], 252))


def test_strategy_series_reuse_the_strategies_own_formulas():
    up = list(range(100, 140))
    r = th.rsi_series(up, RSIStrategy())
    assert np.isnan(r[:14]).all() and r[14:] == pytest.approx(100.0)                 # 一路上漲 → RSI 100
    z = th.bollinger_z([100.0] * 30, BollingerStrategy())
    assert np.isnan(z[:19]).all() and z[19:] == pytest.approx(0.0)                   # 沒波動 → 在中軌上
    g = th.ma_gap(up, MACrossStrategy())
    assert np.isnan(g[:19]).all() and (g[19:] > 0).all()                             # 上漲 → 快線在慢線上
    m = th.momentum_pct([100.0] * 10 + [101.0], MomentumStrategy())
    assert m[-1] == pytest.approx(1.0) and np.isnan(m[:10]).all()
    p = th.range_position([10, 12, 11, 13, 9, 14], 4)                                # 前 4 筆 [10,12,11,13]：9 在下緣外、14 在上緣外
    assert np.isnan(p[:4]).all() and p[4] < 0 and p[5] > 1                           # p[5] 比的是 [12,11,13,9]


def test_vwap_deviation_matches_the_strategy_formula():
    bars = [{"high": 102, "low": 98, "close": 100, "volume": 10}, {"high": 112, "low": 108, "close": 110, "volume": 30}]
    d = th.vwap_deviation(bars, warmup=2)
    vwap = (100 * 10 + 110 * 30) / 40                                                # 典型價 = (h+l+c)/3 → 100、110
    assert d.tolist() == pytest.approx([110 - vwap]) and th.vwap_deviation(bars, warmup=3).size == 0


# ── 逐筆資料：盤別切段、盤前試算、合約交錯 ──────────────────────────

def ts_of(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def make_ticks(path, *, n=3000, seed=1):
    """09:00 起每 0.5 秒一筆 TMF 隨機漫步、0.1 秒後一筆 MXF（價位 +4 點，造成交錯雜訊）；另加 08:31 的盤前試算（三合約差 250 點）。"""
    rng = np.random.default_rng(seed)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE ticks (code TEXT NOT NULL, ts REAL NOT NULL, price REAL NOT NULL, volume INTEGER NOT NULL, "
              "tick_type INTEGER NOT NULL, total_volume INTEGER NOT NULL DEFAULT 0)")
    rows, price, t0 = [], 20000.0, ts_of("2026-10-07 09:00:00")
    for i in range(n):
        price += int(rng.choice([-1, 0, 1]))
        rows.append(("TMFJ6", t0 + i * 0.5, price, 1, int(rng.integers(1, 3)), 0))
        rows.append(("MXFJ6", t0 + i * 0.5 + 0.1, price + 4, 1, int(rng.integers(1, 3)), 0))
    pre = ts_of("2026-10-08 08:31:00")
    for i in range(20):
        rows.append(("TMFJ6", pre + i, 20000.0, 1, 1, 0))
        rows.append(("MXFJ6", pre + i + 0.1, 20250.0, 1, 1, 0))
    rows.append(("TMFJ6", t0, 20000.0, 0, 1, 0))                                     # volume=0 的報價更新不算成交
    c.executemany("INSERT INTO ticks VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def test_split_sessions_cuts_at_long_gaps():
    ts = np.array([0, 1, 2, 5000, 5001, 20000.0])
    assert th.split_sessions(ts, gap=1800) == [(0, 3), (3, 5), (5, 6)]
    assert th.split_sessions(np.empty(0)) == []


def test_load_trades_keeps_only_trades_and_reports_the_preopen_window(tmp_path):
    db = tmp_path / "t.db"
    make_ticks(db, n=50)
    t = th.load_trades(db)
    assert t["ts"].size == 50 * 2 + 40 and (np.diff(t["ts"]) >= 0).all()             # 依時間排序、volume=0 被濾掉
    pre = th.preopen_report(t)
    assert pre["events"] == 40 and pre["max_spread"] == pytest.approx(250.0) and pre["minutes"] >= 1


def test_study_ticks_drops_preopen_and_shows_contract_interleaving_noise(tmp_path):
    db = tmp_path / "t.db"
    make_ticks(db)
    rows, info = th.study_ticks(db)
    assert info["preopen"]["events"] == 40 and info["sessions"] == 1                 # 盤前被排除，只剩 09:00 那一個盤別
    assert info["hours"] == pytest.approx(3000 * 0.5 / 3600, abs=0.01)
    by = {(r["name"].split("：")[0], r["stream"]): r for r in rows}
    assert by[("ma_cross", "三合約混合")]["flips"] > by[("ma_cross", "TMF")]["flips"]   # 價位交錯讓均線訊號更常翻
    assert ("外盤占比（最近 100 筆成交）", "TMF") in by and ("外盤占比（最近 100 筆成交）", "三合約混合") not in by
    assert all(r["unit"] == "小時" and r["n"] > 0 for r in rows)
    # 沒排除的話 momentum 會被 250 點的假跳動打到——排除後不該有任何切換
    assert by[("momentum", "三合約混合")]["flips"] == 0


def test_study_ticks_with_too_little_data_returns_nothing(tmp_path):
    db = tmp_path / "t.db"
    make_ticks(db, n=10)
    rows, info = th.study_ticks(db)
    assert rows == [] and info["trades"] > 0


# ── 日級與 1 分 K ────────────────────────────────────────────────

def seed_store(tmp_path, days=75):
    store = MarketStore(tmp_path / "m.db")
    rng = np.random.default_rng(3)
    d0, price, daily, minutes = date(2026, 6, 1), 20000.0, [], []
    for i in range(days):
        d = (d0 + timedelta(days=i)).isoformat()
        price *= 1 + float(rng.normal(0, 0.008))
        daily.append({"date": d, "open": price, "high": price * 1.006, "low": price * 0.994, "close": price,
                      "volume": 1000, "nbars": 300})
        if i >= days - 3:
            for k in range(60):
                p = price + float(rng.normal(0, 10))
                minutes.append({"date": d, "hhmm": 845 + (k // 60) * 100 + k % 60, "open": p, "high": p + 3, "low": p - 3,
                                "close": p, "volume": 20})
    store.upsert_bars(th.CODE, daily)
    store.upsert_bars_1m(th.CODE, minutes)
    return store


def test_study_daily_covers_the_market_state_thresholds_and_flags_missing_iv(tmp_path):
    rows = th.study_daily(seed_store(tmp_path))
    names = [r["name"] for r in rows]
    assert any(n.startswith("Hurst") for n in names) and any(n.startswith("日K方向") for n in names)
    assert any(n.startswith("日盤振幅比") for n in names)
    iv = next(r for r in rows if r["name"] == "IV 百分位")
    assert "insufficient" in iv and "0 筆" in iv["insufficient"]                      # IV 歷史不夠 → 明講資料不足，不硬算
    h = next(r for r in rows if r["name"].startswith("Hurst"))
    assert h["unit"] == "日" and 0 <= h["whipsaw"] <= 1 and h["flips_hyst"] >= 0


def test_study_minutes_measures_vwap_revert_per_day(tmp_path):
    rows = th.study_minutes(seed_store(tmp_path))
    assert len(rows) == 1 and rows[0]["name"].startswith("vwap_revert") and rows[0]["unit"] == "日"
    assert rows[0]["n"] == 3 * (60 - 10 + 1)                                          # 3 天，每天暖機 10 根後才有值
    assert th.study_minutes(MarketStore(tmp_path / "empty.db")) == []


def test_report_renders_every_section(tmp_path):
    db = tmp_path / "t.db"
    make_ticks(db)
    res = th.study(seed_store(tmp_path), db)
    text = th.format_report(res)
    for part in ("日級", "1 分 K", "逐筆", "開盤前試算時段", "資料不足", "三合約混合 vs 只看 TMF"):
        assert part in text
