"""日內 Hurst（5 分 K 近 N 日）：K 棒合成、去季節性、分辨力、可重現、穩定度、與設定的串接。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from core.daily_summary import Config, build_summary
from core.hurst_analyzer import (
    analyze_intraday, day_returns, day_session_minutes, deseasonalize, five_min_closes,
)
from core.market_store import MarketStore
from test_market_state import make_kbars


def trading_days(n: int, end: date = date(2026, 10, 7)) -> list[str]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def fgn(n: int, hurst: float, rng: np.random.Generator) -> np.ndarray:
    """分數高斯雜訊（Cholesky 法）：hurst>0.5 長記憶趨勢、<0.5 長記憶均值回歸、=0.5 白雜訊。"""
    k = np.arange(n)
    gamma = 0.5 * (np.abs(k + 1) ** (2 * hurst) - 2 * np.abs(k) ** (2 * hurst) + np.abs(k - 1) ** (2 * hurst))
    cov = gamma[np.abs(k[:, None] - k[None, :])]
    return np.linalg.cholesky(cov + 1e-10 * np.eye(n)) @ rng.normal(size=n)


def make_closes(days: list[str], rng: np.random.Generator, hurst: float = 0.5, slots: int = 60) -> dict[str, list[float]]:
    """每天 slots 個 5 分收盤。日內報酬取自同一條 fGn（窗口內串接後恰好還原，長記憶得以保留）；
    每天的第一根不計入（slots 個收盤 → slots-1 筆報酬）。"""
    rets = fgn(len(days) * (slots - 1), hurst, rng) * 0.001
    out = {}
    for i, d in enumerate(days):
        r = rets[i * (slots - 1):(i + 1) * (slots - 1)]
        out[d] = list(20000 * np.exp(np.concatenate([[0.0], np.cumsum(r)])))
    return out


def seed_minutes(store: MarketStore, closes: dict[str, list[float]]) -> None:
    """把「5 分收盤」展開成 end-labeled 的 1 分 K（每個時段 5 根、收盤都等於該時段收盤）。"""
    rows = []
    for day, cs in closes.items():
        for k, c in enumerate(cs):
            for j in range(5):
                t = 8 * 60 + 46 + 5 * k + j
                rows.append({"date": day, "hhmm": (t // 60) * 100 + t % 60, "open": c, "high": c, "low": c,
                             "close": c, "volume": 1})
    store.upsert_bars_1m("TXF", rows)


# ── K 棒處理 ──────────────────────────────────────────────────────

def test_day_session_minutes_keeps_300_bars_per_day_and_drops_the_night():
    rows = day_session_minutes(make_kbars(date(2026, 10, 6), date(2026, 10, 7)))
    # 假資料每天日盤 08:45~13:45 共 301 根（真實 kbars 是 end-labeled 的 300 根）、夜盤 15 根；夜盤要被排除
    assert len(rows) == 2 * 301 and {r["date"] for r in rows} == {"2026-10-06", "2026-10-07"}
    assert all(845 <= r["hhmm"] <= 1345 for r in rows)


def test_five_min_closes_bucket_mapping():
    mins = [{"date": "2026-10-07", "hhmm": 845, "close": -1.0}]                    # 08:45 屬於上一個時段 → 略過
    for i in range(300):                                                           # 08:46 ~ 13:45（end-labeled）
        t = 8 * 60 + 46 + i
        mins.append({"date": "2026-10-07", "hhmm": (t // 60) * 100 + t % 60, "close": float(i)})
    c = five_min_closes(mins)["2026-10-07"]
    assert len(c) == 60 and c[0] == 4.0 and c[1] == 9.0 and c[-1] == 299.0         # 每個時段取最後一根的收盤


def test_five_min_closes_skip_empty_slots_without_filling():
    mins = [{"date": "d", "hhmm": 850, "close": 1.0}, {"date": "d", "hhmm": 910, "close": 3.0}]
    assert five_min_closes(mins)["d"] == [1.0, 3.0]


def test_deseasonalize_normalizes_daily_volatility_and_intraday_shape():
    rng = np.random.default_rng(1)
    season = 1 + 1.5 * np.exp(-np.arange(59) / 8)                                  # 開盤波動大
    rets = [rng.normal(0, 1, 59) * season * v for v in rng.uniform(0.5, 3.0, 20)]  # 每天波動水準不同
    out = deseasonalize(rets)
    per_day = out.reshape(20, 59)
    assert np.allclose(per_day.std(axis=1), 1.0, atol=0.25)
    assert per_day[:, :5].std() == pytest.approx(per_day[:, -5:].std(), rel=0.35)   # 開盤與尾盤強度拉平


# ── 日內 Hurst ────────────────────────────────────────────────────

def test_analyze_intraday_needs_enough_complete_days():
    rng = np.random.default_rng(2)
    days = trading_days(10)
    r = analyze_intraday(make_closes(days, rng), days, window_days=20)
    assert r.state == "UNCERTAIN" and r.value is None and "10 個完整交易日" in r.note
    short_day = make_closes(days, rng)
    short_day[days[-1]] = short_day[days[-1]][:20]                                  # 只有 20 個時段的殘缺日不算
    assert analyze_intraday(short_day, days, window_days=9).window == 9


def test_analyze_intraday_separates_noise_from_real_serial_dependence():
    days = trading_days(25)
    states = [analyze_intraday(make_closes(days, np.random.default_rng(s)), days, 20, n_perm=60).state for s in range(12)]
    assert states.count("RANDOM") >= 9                                              # 純雜訊：絕大多數判隨機漫步

    # 5 分 K 版的 DFA 看的是 20 分鐘~一整天尺度的「長記憶」；真的有長記憶時要能偵測出來
    up = analyze_intraday(make_closes(days, np.random.default_rng(3), hurst=0.65), days, 20, n_perm=60)
    dn = analyze_intraday(make_closes(days, np.random.default_rng(3), hurst=0.35), days, 20, n_perm=60)
    assert up.state == "TREND" and up.z > 2.5 and up.window_label.startswith("近 20 日 5 分K")
    assert dn.state == "REVERT" and dn.z < -2.5


def test_analyze_intraday_is_reproducible_and_stable_when_the_window_slides_one_day():
    days = trading_days(40)
    closes = make_closes(days, np.random.default_rng(4))
    a = analyze_intraday(closes, days[:-1], 20, n_perm=60)
    assert a == analyze_intraday(closes, days[:-1], 20, n_perm=60)                  # 同資料 → 同結果（種子取自日期）
    hs = [analyze_intraday(closes, days[:i], 20, n_perm=40).value for i in range(21, 41)]
    assert np.mean(np.abs(np.diff(hs))) < 0.05                                      # 日間變動小（現行日 K 做法約 0.18）


def test_day_returns_never_cross_the_overnight_gap():
    closes = {"d1": [100.0, 101.0, 102.0], "d2": [200.0, 201.0, 202.0]}
    r = day_returns(closes, ["d1", "d2"])
    assert [len(x) for x in r] == [2, 2]
    assert np.allclose(r[1], np.diff(np.log([200.0, 201.0, 202.0])))                # d2 第一筆不是 d1 收盤→d2 開盤的跳空


# ── 與設定／摘要的串接 ────────────────────────────────────────────

def test_config_parses_hurst_freq(monkeypatch):
    assert Config.from_env().hurst_freq == "D"                                      # 預設維持現行
    for v, want in (("5m", "5m"), ("5M", "5m"), ("5min", "5m"), ("D", "D"), ("nonsense", "D")):
        monkeypatch.setenv("HURST_FREQ", v)
        assert Config.from_env().hurst_freq == want
    monkeypatch.setenv("HURST_DAYS", "30")
    assert Config.from_env().hurst_days == 30


def test_build_summary_with_5m_uses_minute_bars_before_today_only(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    days = trading_days(30)                                                         # 最後一天是 2026-10-07（= 「今天」）
    seed_minutes(store, make_closes(days, np.random.default_rng(5)))
    s = build_summary(store, Config(hurst_freq="5m", hurst_days=20), date(2026, 10, 7), "sim", "pre")
    assert s["hurst"]["window_label"].startswith("近 20 日 5 分K") and s["hurst"]["last_bar"] == days[-2]   # 今天的不算
    assert s["hurst"]["state"] in ("RANDOM", "TREND", "REVERT")
    d = build_summary(store, Config(hurst_freq="D"), date(2026, 10, 7), "sim", "pre")
    assert d["hurst"]["state"] == "UNCERTAIN"                                       # 沒有日 K → 日 K 做法資料不足，兩條路互不影響


def test_build_summary_5m_without_minute_history_is_uncertain_not_a_crash(tmp_path):
    s = build_summary(MarketStore(tmp_path / "e.db"), Config(hurst_freq="5m"), date(2026, 10, 7), "sim", "pre")
    assert s["hurst"]["state"] == "UNCERTAIN" and "資料不足" in s["hurst"]["note"]
