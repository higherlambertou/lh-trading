"""市場狀態判斷系統（Hurst / IV / 每日總結 / 日誌 / scalp 偏向）的測試。
執行：python -m pytest tests -q"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from core import daily_summary as ds
from core.daily_summary import Config, MarketStateService, build_summary, combine
from core.hurst_analyzer import (
    aggregate_daily, analyze, classify_hurst, null_stats, trend_direction, ts_to_local,
)
from core.iv_monitor import (
    _option_price, atm_iv_from_quotes, black76, evaluate_iv, expiry_of, fetch_atm_iv,
    implied_vol, iv_percentile, iv_signal, pick_expiry, read_iv_csv,
)
from core.live_state import LiveState
from core.market_store import MarketStore
from core.trade_log import TradeLog


# ── 工具 ──────────────────────────────────────────────────────────

def _ar1_prices(n_bars: int, phi: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    e = rng.normal(0, 0.01, n_bars + 50)
    x = np.zeros_like(e)
    for t in range(1, len(e)):
        x[t] = phi * x[t - 1] + e[t]
    return 20000 * np.exp(np.cumsum(x[50:]))


def _ns(dt: datetime) -> int:
    return int((dt - datetime(1970, 1, 1)).total_seconds() * 1e9)


def make_kbars(start: date, end: date) -> dict[str, list]:
    """假的 1 分 K：每個平日有日盤 08:45~13:45（301 根）+ 夜盤 15:00~15:09 與次日 02:00~02:04。"""
    out = {k: [] for k in ("ts", "open", "high", "low", "close", "volume")}
    d = start
    while d <= end:
        if d.weekday() < 5:
            base = 20000 + 300 * np.sin(d.toordinal() / 9.0)
            mins = [datetime(d.year, d.month, d.day, 8, 45) + timedelta(minutes=i) for i in range(301)]
            mins += [datetime(d.year, d.month, d.day, 15, 0) + timedelta(minutes=i) for i in range(10)]
            nxt = d + timedelta(days=1)
            mins += [datetime(nxt.year, nxt.month, nxt.day, 2, 0) + timedelta(minutes=i) for i in range(5)]
            for i, m in enumerate(mins):
                px = base + (i % 7) - 3
                out["ts"].append(_ns(m))
                out["open"].append(px)
                out["high"].append(px + 2)
                out["low"].append(px - 2)
                out["close"].append(px + 1)
                out["volume"].append(10)
        d += timedelta(days=1)
    return out


class FakeBroker:
    is_connected = True

    def __init__(self, dead_strikes: set[int] | None = None) -> None:
        self.kbar_calls: list[tuple[str, str, str]] = []
        self.dead_strikes = dead_strikes or set()          # 沒有報價也沒成交的履約價

    async def kbars(self, code, start, end):
        self.kbar_calls.append((code, start, end))
        return make_kbars(date.fromisoformat(start), date.fromisoformat(end))

    async def snapshots(self, codes):
        return [{"code": "TXFR1", "close": 20010.0}]

    async def option_expiries(self, category="TXO"):
        return ["202610", "202611"]

    async def option_strikes(self, month, right, category="TXO"):
        return [19900, 20000, 20100]

    async def option_snapshot(self, month, strike, right, category="TXO"):
        if strike in self.dead_strikes:
            return {"code": f"TXO{strike}", "close": 0.0, "bid": 0.0, "ask": 0.0, "total_volume": 0}
        T = (expiry_of(month) - datetime(2026, 10, 7, 8, 30)).total_seconds() / (365 * 86400)
        px = black76(20000.0, float(strike), T, 0.18, 0.015, right == "C")
        return {"code": f"TXO{strike}", "close": round(px, 4), "bid": 0, "ask": 0}


FIXED = datetime(2026, 10, 7, 9, 0)      # 週三 09:00


@pytest.fixture
def frozen(monkeypatch):
    def freeze(when: datetime = FIXED):
        class _D(date):
            @classmethod
            def today(cls):
                return when.date()

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return when

        monkeypatch.setattr(ds, "date", _D)
        monkeypatch.setattr(ds, "datetime", _DT)
    freeze()
    return freeze


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "m.db")


@pytest.fixture
def svc(store, monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    return MarketStateService(store, Config(iv_auto=False), FakeBroker())


# ── Hurst ─────────────────────────────────────────────────────────

def test_hurst_is_calibrated_on_random_walk():
    hs = [analyze(_ar1_prices(60, 0.0, s)).value for s in range(200)]
    assert abs(np.mean(hs) - 0.5) < 0.04            # 原腳本在這裡是 ~0.55 靠兩個偏誤互相抵銷


def test_hurst_separates_persistent_and_antipersistent():
    up = np.mean([analyze(_ar1_prices(120, 0.4, s), window=120).value for s in range(100)])
    dn = np.mean([analyze(_ar1_prices(120, -0.4, s), window=120).value for s in range(100)])
    assert up > 0.6 and dn < 0.4


def test_analyze_insufficient_data():
    r = analyze([20000, 20100, 20050], window=60)
    assert r.state == "UNCERTAIN" and r.value is None and "資料不足" in r.note


def test_null_stats_is_deterministic_and_cached():
    assert null_stats(59) == null_stats(59)
    m, sd = null_stats(59)
    assert 0.3 < m < 0.9 and 0.05 < sd < 0.3


def test_classify_thresholds_and_min_z():
    assert classify_hurst(0.56) == "TREND"
    assert classify_hurst(0.44) == "REVERT"
    assert classify_hurst(0.55) == "RANDOM" and classify_hurst(0.45) == "RANDOM"
    assert classify_hurst(None) == "UNCERTAIN" and classify_hurst(float("nan")) == "UNCERTAIN"
    assert classify_hurst(0.60, z=0.5, min_z=1.0) == "RANDOM"        # 統計上不顯著
    assert classify_hurst(0.60, z=1.5, min_z=1.0) == "TREND"


def test_trend_direction():
    up = list(np.linspace(19000, 20000, 30))
    assert trend_direction(up) == 1
    assert trend_direction(up[::-1]) == -1
    assert trend_direction([20000.0] * 30) == 0                      # 貼近均線
    assert trend_direction([20000.0] * 5) == 0                       # 資料不足


def test_aggregate_daily_keeps_day_session_only():
    d = date(2026, 10, 6)
    bars = aggregate_daily(make_kbars(d, d))
    assert len(bars) == 1
    b = bars[0]
    assert b["date"] == "2026-10-06" and b["nbars"] == 301          # 夜盤 15 根被排除
    assert b["volume"] == 3010
    assert b["open"] == 20000 + 300 * np.sin(d.toordinal() / 9.0) - 3


def test_ts_to_local_handles_ns_ms_s():
    want = datetime(2026, 10, 6, 9, 30)
    ns = _ns(want)
    assert ts_to_local(ns) == want
    assert ts_to_local(ns / 1e6) == want
    assert ts_to_local(ns / 1e9) == want


# ── IV ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("is_call", [True, False])
@pytest.mark.parametrize("sigma", [0.1, 0.2, 0.45])
def test_implied_vol_roundtrip(sigma, is_call):
    px = black76(20000, 20100, 20 / 365, sigma, 0.015, is_call)
    assert implied_vol(px, 20000, 20100, 20 / 365, 0.015, is_call) == pytest.approx(sigma, abs=1e-5)


def test_implied_vol_no_solution():
    assert implied_vol(1.0, 20000, 19000, 0.05, 0.015, True) is None   # 低於內含值
    assert implied_vol(0, 20000, 20000, 0.05, 0.015, True) is None


def test_atm_iv_from_quotes_recovers_iv_and_forward():
    T, r = 25 / 365, 0.015
    c = black76(20050, 20000, T, 0.18, r, True)
    p = black76(20050, 20000, T, 0.18, r, False)
    iv, f = atm_iv_from_quotes(c, p, 20000, T, r)
    assert iv == pytest.approx(18.0, abs=0.05) and f == pytest.approx(20050, abs=1)


def test_expiry_and_pick_expiry():
    assert expiry_of("202610") == datetime(2026, 10, 21, 13, 30)      # 第三個週三
    months = ["202610", "202611"]
    assert pick_expiry(months, datetime(2026, 10, 7, 8, 30)) == "202610"
    assert pick_expiry(months, datetime(2026, 10, 16, 8, 30)) == "202611"   # 距到期 < 7 天 → 換下月
    assert pick_expiry(months, datetime(2026, 12, 1)) is None


def test_percentile_and_signal_follow_the_doc():
    hist = list(range(1, 101))                                        # 1..100
    assert iv_percentile(50.5, hist) == pytest.approx(50.0)
    assert iv_signal(19.9) == "LOW" and iv_signal(20) == "NORMAL"
    assert iv_signal(80) == "NORMAL" and iv_signal(80.1) == "HIGH"


def test_evaluate_iv_states():
    hist = [float(x) for x in range(10, 110)]
    assert evaluate_iv(None, hist)["state"] == "UNKNOWN"
    short = evaluate_iv(50, hist[:10], min_history=60)
    assert short["state"] == "UNKNOWN" and "累積中 10/60" in short["label"]
    assert evaluate_iv(11, hist)["state"] == "LOW"
    assert evaluate_iv(60, hist)["state"] == "NORMAL"
    assert evaluate_iv(200, hist)["state"] == "HIGH"


def test_read_iv_csv_accepts_chinese_header_and_slash_dates(tmp_path):
    p = tmp_path / "vix.csv"
    p.write_text("日期,臺指選擇權波動率指數\n2026/09/01,18.5\n2026-09-02,19.25\nbad,row\n2026/09/03,abc\n",
                 encoding="utf-8-sig")
    assert read_iv_csv(str(p)) == [("2026-09-01", 18.5), ("2026-09-02", 19.25)]


def test_fetch_atm_iv_with_fake_broker():
    res = asyncio.run(fetch_atm_iv(FakeBroker(), now=datetime(2026, 10, 7, 8, 30)))
    assert res["month"] == "202610" and res["strike"] == 20000        # 20010 最近的履約價
    assert res["iv"] == pytest.approx(18.0, abs=0.05)


def test_option_price_prefers_mid_over_stale_last():
    assert _option_price({"close": 715, "bid": 735, "ask": 755}) == 745       # 最近成交低於買價 = 陳舊
    assert _option_price({"close": 805, "bid": 785, "ask": 805}) == 795
    assert _option_price({"close": 300, "bid": 100, "ask": 200}) == 300       # 價差過寬 → 退回成交價
    assert _option_price({"close": 300, "bid": 0, "ask": 0}) == 300           # 沒報價（盤前/休市）
    assert _option_price({"close": 0, "bid": 0, "ask": 0}) == 0


def test_fetch_atm_iv_skips_strikes_without_quotes():
    # 離現價最近的 20000 沒有報價沒成交 → 往下一檔（20100 比 19900 近）
    res = asyncio.run(fetch_atm_iv(FakeBroker(dead_strikes={20000}), now=datetime(2026, 10, 7, 8, 30)))
    assert res["strike"] == 20100 and res["iv"] == pytest.approx(18.0, abs=0.05)


def test_fetch_atm_iv_raises_when_nothing_is_quoted():
    broker = FakeBroker(dead_strikes={19900, 20000, 20100})
    with pytest.raises(RuntimeError, match="沒有可用報價"):
        asyncio.run(fetch_atm_iv(broker, now=datetime(2026, 10, 7, 8, 30)))


def test_fetch_atm_iv_rejects_absurd_iv():
    class Absurd(FakeBroker):
        async def option_snapshot(self, month, strike, right, category="TXO"):
            return {"code": "x", "close": 3000.0, "bid": 3000.0, "ask": 3000.0}      # 隱含 IV ~190%

    with pytest.raises(RuntimeError, match="沒有可用報價"):
        asyncio.run(fetch_atm_iv(Absurd(), now=datetime(2026, 10, 7, 8, 30)))


class ReplayBroker(FakeBroker):
    """2026-10-07 16:26 在正式盤實際抓到的 snapshot（夜盤時段）：TXF=49840，
    最近的 49850 沒報價沒成交，有量的是 49800 / 49900；49900 Call 最近成交 715 低於買價 735（陳舊）。"""
    QUOTES = {  # (履約價, 買賣權) → (close, bid, ask)
        (49850, "C"): (0, 0, 0), (49850, "P"): (0, 0, 0),
        (49800, "C"): (805, 785, 805), (49800, "P"): (760, 745, 760),
        (49900, "C"): (715, 735, 755), (49900, "P"): (815, 795, 815),
    }

    async def snapshots(self, codes):
        return [{"code": "TXFR1", "close": 49840.0}]

    async def option_strikes(self, month, right, category="TXO"):
        return [49700, 49750, 49800, 49850, 49900, 49950, 50000]

    async def option_snapshot(self, month, strike, right, category="TXO"):
        if (strike, right) not in self.QUOTES:
            raise RuntimeError("找不到選擇權合約")
        close, bid, ask = self.QUOTES[(strike, right)]
        return {"code": f"TXO{strike}", "close": float(close), "bid": float(bid), "ask": float(ask)}


def test_fetch_atm_iv_replays_real_snapshots_from_2026_10_07():
    now = datetime(2026, 10, 7, 16, 26)
    res = asyncio.run(fetch_atm_iv(ReplayBroker(), now=now))
    assert res["strike"] == 49800                                   # 49850 沒報價 → 跳過
    assert res["forward"] == pytest.approx(49840, abs=5)            # 平價推出的遠期價 ≈ TXF 現價
    # 獨立估算：Brenner-Subrahmanyam  σ ≈ 跨式價 / (0.8·F·√T)
    T = (expiry_of("202610") - now).total_seconds() / (365 * 86400)
    approx = 100 * (795 + 752.5) / (0.7979 * res["forward"] * T ** 0.5)
    assert res["iv"] == pytest.approx(approx, abs=0.3) and 19.0 < res["iv"] < 21.0
    # 另一檔 49900（用中價）算出的 IV 應與 49800 一致，證明資料與方法自洽
    other = atm_iv_from_quotes(745, 805, 49900, T, 0.015)[0]
    assert other == pytest.approx(res["iv"], abs=0.3)
    # 若誤用陳舊的最近成交價（49900 Call=715），平價推出的遠期價會偏離 TXF 約 40 點；中價不會
    stale_forward = atm_iv_from_quotes(715, 815, 49900, T, 0.015)[1]
    assert abs(stale_forward - 49840) > 30 and abs(res["forward"] - 49840) < 5

    # 其餘履約價查不到合約（RuntimeError）也不應中斷，而是換下一檔
    class OnlyFar(ReplayBroker):
        QUOTES = {k: v for k, v in ReplayBroker.QUOTES.items() if k[0] == 49900}

    assert asyncio.run(fetch_atm_iv(OnlyFar(), now=now))["strike"] == 49900


# ── 儲存層 ────────────────────────────────────────────────────────

def test_manual_iv_is_not_overwritten_by_auto(store):
    assert store.upsert_iv("2026-10-07", 20.0, "manual")
    assert not store.upsert_iv("2026-10-07", 25.0, "shioaji")
    assert store.iv_on("2026-10-07")["iv"] == 20.0
    assert store.upsert_iv("2026-10-07", 21.0, "manual")              # 手動可再改
    assert store.upsert_iv("2026-10-08", 22.0, "shioaji")
    assert store.upsert_iv("2026-10-08", 23.0, "shioaji")             # 自動可覆蓋自動
    assert store.iv_on("2026-10-08")["iv"] == 23.0


def test_iv_history_and_latest_window(store):
    for i, d in enumerate(["2026-10-01", "2026-10-02", "2026-10-05"]):
        store.upsert_iv(d, 10.0 + i, "csv")
    assert store.iv_history("2026-10-05") == [11.0, 10.0]             # 新→舊，不含當天
    assert store.iv_latest("2026-10-07")["date"] == "2026-10-05"
    assert store.iv_latest("2026-10-20") is None                      # 超過 5 天


def test_bars_roundtrip_respects_before(store):
    bars = [{"date": f"2026-10-0{i}", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
             "volume": 9, "nbars": 301} for i in range(1, 6)]
    store.upsert_bars("TXF", bars)
    assert [b["date"] for b in store.bars("TXF", 10, before="2026-10-04")] == \
        ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert store.count_bars("TXF") == 5 and store.last_bar_date("TXF") == "2026-10-05"


def test_journal_keeps_notes_across_recompute_and_stats(store):
    base = {"phase": "pre", "computed_at": 1.0, "hurst": 0.6, "hurst_z": 1.0, "hurst_state": "TREND",
            "iv": 18.0, "iv_pct": 40.0, "iv_state": "NORMAL", "direction": 1,
            "market_state": "TREND", "strategy_hint": "x", "summary_json": "{}"}
    store.upsert_journal("2026-10-06", "sim", base)
    store.upsert_journal("2026-10-07", "sim", {**base, "market_state": "REVERT"})
    assert store.set_note("2026-10-06", "sim", "看到急拉", "備註A")
    store.upsert_journal("2026-10-06", "sim", {**base, "hurst": 0.7})   # 重算
    row = store.get_journal("2026-10-06", "sim")
    assert row["hurst"] == 0.7 and row["basis"] == "看到急拉" and row["notes"] == "備註A"
    assert not store.set_note("2099-01-01", "sim", "x", None)

    store.add_strategy_day("2026-10-06", "sim", "scalp", 3, 500.0)
    store.add_strategy_day("2026-10-06", "sim", "scalp", 1, -100.0)     # 累加
    store.add_strategy_day("2026-10-07", "sim", "scalp", 2, -300.0)
    store.add_strategy_day("2026-10-08", "sim", "orb", 0, 0.0)          # 沒有日誌列也會補一列
    rows = {r["date"]: r for r in store.list_journal("sim")}
    assert rows["2026-10-06"]["trades"] == 4 and rows["2026-10-06"]["pnl"] == 400.0
    assert rows["2026-10-06"]["result"] == "獲利" and rows["2026-10-06"]["scalp_on"]
    assert rows["2026-10-07"]["result"] == "虧損"
    assert rows["2026-10-08"]["result"] == "未進場" and not rows["2026-10-08"]["scalp_on"]
    assert "summary_json" not in rows["2026-10-06"]

    st = {g["state"]: g for g in store.stats("sim")["by_state"]}
    assert st["TREND"]["wins"] == 1 and st["TREND"]["win_rate"] == 1.0
    assert st["REVERT"]["losses"] == 1 and st["REVERT"]["win_rate"] == 0.0
    assert store.list_journal("live") == []                              # mode 隔離


def test_stats_big_move_by_iv_state(store):
    for i, (iv_state, ratio) in enumerate([("LOW", 2.0), ("LOW", 1.0), ("HIGH", 0.8)]):
        store.upsert_journal(f"2026-10-0{i + 1}", "sim", {"iv_state": iv_state, "market_state": "X"})
        store.set_range_ratio(f"2026-10-0{i + 1}", "sim", ratio)
    by_iv = {g["iv_state"]: g for g in store.stats("sim")["by_iv"]}
    assert by_iv["LOW"]["days"] == 2 and by_iv["LOW"]["big_move_rate"] == 0.5
    assert by_iv["HIGH"]["big_move_days"] == 0


# ── 策略對應表 / 總結 ─────────────────────────────────────────────

@pytest.mark.parametrize("hurst,iv,req,want", [
    ("TREND", "NORMAL", False, ("TREND", ["scalp", "orb"])),
    ("REVERT", "NORMAL", False, ("REVERT", ["scalp", "vwap_revert"])),
    ("RANDOM", "NORMAL", False, ("UNCLEAR", [])),
    ("UNCERTAIN", "NORMAL", False, ("UNCLEAR", [])),
    ("TREND", "LOW", False, ("IV_LOW", [])),                 # IV 低：任何 Hurst 都建議選擇權
    ("REVERT", "HIGH", False, ("IV_HIGH", [])),
    ("TREND", "UNKNOWN", False, ("TREND", ["scalp", "orb"])),   # IV 未就緒 → 只看 Hurst
    ("TREND", "UNKNOWN", True, ("UNCLEAR", [])),                # IV_REQUIRED
])
def test_combine_follows_strategy_table(hurst, iv, req, want):
    state, strategies, hint = combine(hurst, iv, req)
    assert (state, strategies) == want and hint


def _seed_bars(store, prices, end=date(2026, 10, 6)):
    days = []
    d = end
    while len(days) < len(prices):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    store.upsert_bars("TXF", [
        {"date": dd.isoformat(), "open": p, "high": p + 5, "low": p - 5, "close": p, "volume": 1, "nbars": 301}
        for dd, p in zip(reversed(days), prices)])


def test_build_summary_text_and_ivs(store):
    _seed_bars(store, _ar1_prices(60, 0.4, 3))
    for i in range(70):
        store.upsert_iv((date(2026, 7, 1) + timedelta(days=i)).isoformat(), 15.0 + i * 0.1, "csv")
    store.upsert_iv("2026-10-07", 16.0, "manual")                     # 今日 IV 偏低（歷史 15~22）
    s = build_summary(store, Config(), date(2026, 10, 7), "sim", "pre")
    assert s["iv"]["state"] == "LOW" and s["state"] == "IV_LOW"
    assert s["text"].startswith("=== 今日市場狀態 ===") and "Hurst 指數：" in s["text"]
    assert s["hurst"]["window"] == 60 and s["hurst"]["last_bar"] == "2026-10-06"


def test_build_summary_without_iv_falls_back_to_hurst_only(store):
    _seed_bars(store, _ar1_prices(60, 0.4, 3))
    s = build_summary(store, Config(), date(2026, 10, 7), "sim", "pre")
    assert s["iv"]["state"] == "UNKNOWN" and s["state"] in ("TREND", "UNCLEAR", "REVERT")
    assert any("IV 層未就緒" in n for n in s["notes"])
    assert s["direction"] in (-1, 0, 1)


def test_a_lone_iv_value_does_not_change_the_judgement_until_history_exists(store):
    """輸入了今日 ATM IV、但歷史是 0 天：IV 層仍是「累積中」，判斷只看 Hurst（面板『今日判斷』不會變）。
    歷史夠了才會動：同一個 IV 值若比歷史低 → 偏低 → 判斷變成選擇權。"""
    _seed_bars(store, _ar1_prices(60, 0.4, 3))
    without = build_summary(store, Config(), date(2026, 10, 7), "sim", "pre")
    store.upsert_iv("2026-10-07", 19.9, "manual")
    with_iv = build_summary(store, Config(), date(2026, 10, 7), "sim", "pre")
    assert with_iv["iv"]["value"] == 19.9 and with_iv["iv"]["state"] == "UNKNOWN"
    assert with_iv["iv"]["label"] == "累積中 0/60"
    assert (with_iv["state"], with_iv["hint"]) == (without["state"], without["hint"])
    assert with_iv["state_label"].endswith("IV累積中 0/60") and without["state_label"].endswith("IV未知")

    for i in range(60):                                                 # 歷史 60 天、全都比 19.9 高
        store.upsert_iv((date(2026, 7, 1) + timedelta(days=i)).isoformat(), 25.0 + i * 0.1, "csv")
    ready = build_summary(store, Config(), date(2026, 10, 7), "sim", "pre")
    assert ready["iv"]["state"] == "LOW" and ready["state"] == "IV_LOW"
    assert ready["state_label"].endswith("IV偏低")


def test_build_summary_uses_only_bars_before_today(store):
    _seed_bars(store, _ar1_prices(60, 0.0, 1), end=date(2026, 10, 7))   # 最後一根是「今天」
    s = build_summary(store, Config(), date(2026, 10, 7), "sim", "pre")
    assert s["hurst"]["last_bar"] == "2026-10-06" and s["hurst"]["window"] == 59


# ── gate / scalp 偏向 ─────────────────────────────────────────────

def _with_summary(svc, state, direction, age_days=0, **kw):
    d = (date.today() - timedelta(days=age_days)).isoformat()
    svc.summary = {"date": d, "state": state, "direction": direction, "hint": "H",
                   "hurst": {"label": "x"}, **kw}
    return svc


def test_check_strategy_warns_on_mismatch_only(svc):
    _with_summary(svc, "TREND", 1)
    assert svc.check_strategy("vwap_revert") and svc.check_strategy("rsi")
    assert svc.check_strategy("orb") is None and svc.check_strategy("scalp") is None
    _with_summary(svc, "REVERT", 1)
    assert svc.check_strategy("orb") and svc.check_strategy("vwap_revert") is None
    _with_summary(svc, "UNCLEAR", 1)
    assert "不明確" in svc.check_strategy("scalp")
    _with_summary(svc, "IV_LOW", 1)
    assert svc.check_strategy("scalp")
    _with_summary(svc, "UNCLEAR", 1, age_days=10)                     # 過期不警告
    assert svc.check_strategy("scalp") is None
    svc.cfg = Config(gate="off")
    _with_summary(svc, "UNCLEAR", 1)
    assert svc.check_strategy("scalp") is None


@pytest.mark.parametrize("bias,state,direction,want", [
    (1, "TREND", 1, 1), (1, "TREND", -1, -1),       # 順勢：跟日K方向
    (-1, "REVERT", 1, -1), (-1, "REVERT", -1, 1),   # 逆勢：反日K方向
    (2, "TREND", 1, 1), (2, "REVERT", 1, -1),       # 自動：依狀態
    (2, "UNCLEAR", 1, 0), (2, "IV_LOW", 1, 0),      # 自動 + 不明確 → 不進場
    (1, "TREND", 0, 0), (2, "REVERT", 0, 0),        # 無方向 → 不進場
])
def test_bias_direction(svc, bias, state, direction, want):
    _with_summary(svc, state, direction)
    got, reason = svc.bias_direction(bias)
    assert got == want and (want != 0 or reason)


def test_bias_direction_blocks_without_or_with_stale_summary(svc):
    assert svc.bias_direction(1)[0] == 0
    _with_summary(svc, "TREND", 1, age_days=10)
    assert svc.bias_direction(1)[0] == 0


def test_scalp_market_bias_filters_signals(svc, monkeypatch):
    from strategies import scalp as scalp_mod
    monkeypatch.setattr(scalp_mod, "market_state", svc)
    s = scalp_mod.ScalpStrategy()
    assert s.params["market_bias"] == 0
    assert s._apply_market_bias(1) == 1 and s._apply_market_bias(-1) == -1      # 預設不限：行為不變

    s._apply_params({"market_bias": 1})
    _with_summary(svc, "TREND", 1)
    assert s._apply_market_bias(1) == 1 and s._apply_market_bias(-1) == 0       # 只做多
    assert any("偏向擋單" in e for e in s.state.events)
    n = len(s.state.events)
    s._apply_market_bias(-1)
    assert len(s.state.events) == n                                             # 同一理由不洗版

    s._apply_params({"market_bias": -1})
    assert s._apply_market_bias(-1) == -1 and s._apply_market_bias(1) == 0      # 逆勢：只做空

    s._apply_params({"market_bias": 7})                                         # 非法值 → 0
    assert s.market_bias == 0


# ── 服務整合（假 broker）─────────────────────────────────────────

def test_refresh_syncs_bars_persists_journal_and_restart_reloads(svc, store, frozen):
    s = asyncio.run(svc.refresh("pre"))
    assert s["hurst"]["window"] == 60 and s["hurst"]["state"] in ("TREND", "REVERT", "RANDOM")
    assert store.count_bars("TXF") >= 62
    assert store.days_1m("TXF") >= 62                                # 日盤 1 分 K 也一併保存（日內研究用）
    assert len(store.bars_1m("TXF")) == store.days_1m("TXF") * 301    # 假資料日盤 08:45~13:45 共 301 根
    assert len(svc.broker.kbar_calls) >= 3                             # 區間被切成多段查詢
    assert all((date.fromisoformat(e) - date.fromisoformat(b)).days <= 26
               for _, b, e in svc.broker.kbar_calls)
    row = store.get_journal("2026-10-07", "sim")
    assert row["phase"] == "pre" and json.loads(row["summary_json"])["date"] == "2026-10-07"

    # 重啟：載入已存判斷、不重算（盤中判斷維持不變）
    svc2 = MarketStateService(store, Config(iv_auto=False), FakeBroker())
    asyncio.run(svc2.startup())
    assert svc2.summary["hurst"]["value"] == s["hurst"]["value"] and svc2.broker.kbar_calls == []


def test_second_refresh_is_incremental(svc, frozen):
    asyncio.run(svc.refresh("pre"))
    first = svc.broker.kbar_calls[0][1]
    svc.broker.kbar_calls.clear()
    asyncio.run(svc.refresh("manual"))
    assert len(svc.broker.kbar_calls) == 1                             # 只補最近幾天
    assert svc.broker.kbar_calls[0][1] > first


def test_missing_minute_history_triggers_a_full_backfill(svc, store, frozen):
    asyncio.run(svc.refresh("pre"))
    first_calls = len(svc.broker.kbar_calls)
    assert first_calls >= 3
    svc.broker.kbar_calls.clear()
    asyncio.run(svc.refresh("manual"))
    assert len(svc.broker.kbar_calls) == 1                              # 日 K 與 1 分 K 都齊 → 只補最近幾天

    import sqlite3
    with sqlite3.connect(store.path) as c:                              # 模擬「舊版資料庫：有日 K、沒有 1 分 K」
        c.execute("DELETE FROM bars_1m")
    svc.broker.kbar_calls.clear()
    asyncio.run(svc.refresh("manual"))
    assert len(svc.broker.kbar_calls) >= 3 and store.days_1m("TXF") >= 62   # 整段回補


def test_refresh_degrades_when_broker_down(svc, store, frozen):
    svc.broker.is_connected = False
    s = asyncio.run(svc.refresh("pre"))
    assert s["hurst"]["state"] == "UNCERTAIN" and any("券商未連線" in n for n in s["notes"])
    assert s["phase"] == "early"                                       # 沒抓到最新日K → 不算正式盤前判斷
    assert svc.broker.kbar_calls == []


def test_pre_judgment_is_retried_until_fresh_bars_arrive(svc, store, frozen):
    svc.broker.is_connected = False
    asyncio.run(svc.startup())                                         # 09:00 啟動、券商還沒連上
    assert svc.summary["phase"] == "early" and svc._pre_done == ""

    asyncio.run(svc.tick({}))                                          # 5 分鐘內不重試（避免洗版）
    assert svc.broker.kbar_calls == [] and svc._pre_done == ""
    svc._retry_at.clear()

    svc.broker.is_connected = True                                     # 券商連上 → 補算正式盤前判斷
    asyncio.run(svc.tick({}))
    assert svc.summary["phase"] == "pre" and svc._pre_done == "2026-10-07"
    assert store.get_journal("2026-10-07", "sim")["phase"] == "pre"


def test_refresh_survives_kbars_failure(svc, frozen):
    async def boom(*a, **k):
        raise asyncio.TimeoutError()
    svc.broker.kbars = boom
    s = asyncio.run(svc.refresh("pre"))
    assert any("日K同步失敗" in n for n in s["notes"])                 # 不丟例外，改用快取


def test_weekend_does_not_write_journal(svc, store, frozen):
    frozen(datetime(2026, 10, 10, 9, 0))                               # 週六
    asyncio.run(svc.refresh("pre"))
    assert store.list_journal("sim") == []


def test_manual_iv_recomputes_today_and_backfills_past(svc, store, frozen):
    asyncio.run(svc.refresh("pre"))
    s = asyncio.run(svc.set_manual_iv(18.5))
    assert s["phase"] == "manual" and s["iv"]["value"] == 18.5 and s["iv"]["source"] == "manual"
    before = svc.summary
    asyncio.run(svc.set_manual_iv(17.0, "2026-09-01"))                 # 過去日期只回填歷史
    assert svc.summary is before and store.iv_on("2026-09-01")["iv"] == 17.0


def test_tick_runs_pre_once_then_post_close_updates_range_ratio(svc, store, frozen):
    asyncio.run(svc.tick({}))
    assert svc.summary and svc.summary["phase"] == "pre"
    calls = len(svc.broker.kbar_calls)
    asyncio.run(svc.tick({}))
    assert len(svc.broker.kbar_calls) == calls                         # 當天只算一次

    frozen(datetime(2026, 10, 7, 13, 55))
    asyncio.run(svc.tick({}))
    row = store.get_journal("2026-10-07", "sim")
    assert row["range_ratio"] is not None and row["range_ratio"] > 0
    assert svc._post_done == "2026-10-07"


def _fake_strategy(pnl=0.0, trades=0, running=True):
    return SimpleNamespace(state=SimpleNamespace(is_running=running, realized_pnl=pnl), _trades_today=trades)


def test_sample_strategies_accumulates_deltas_across_restarts(svc, store, frozen):
    st = _fake_strategy()
    asyncio.run(svc.sample_strategies({"scalp": st}))                  # 啟動：建列、0 筆
    st.state.realized_pnl, st._trades_today = 200.0, 2
    asyncio.run(svc.sample_strategies({"scalp": st}))
    st.state.realized_pnl, st._trades_today = 150.0, 3                 # 虧了 50、又進一筆
    asyncio.run(svc.sample_strategies({"scalp": st}))
    st.state.is_running = False
    asyncio.run(svc.sample_strategies({"scalp": st}))                  # 停止後無變化不重複累加
    row = store.list_journal("sim")[0]
    assert (row["trades"], row["pnl"]) == (3, 150.0) and row["scalp_on"]

    st.state.is_running = True                                         # 重新啟動：_trades_today 歸零
    st._trades_today = 0
    asyncio.run(svc.sample_strategies({"scalp": st}))
    st._trades_today, st.state.realized_pnl = 1, 250.0
    asyncio.run(svc.sample_strategies({"scalp": st}))
    row = store.list_journal("sim")[0]
    assert (row["trades"], row["pnl"]) == (4, 250.0)


def test_idle_strategies_leave_no_rows(svc, store, frozen):
    asyncio.run(svc.sample_strategies({"orb": _fake_strategy(running=False)}))
    assert store.list_journal("sim") == []


# ── API ───────────────────────────────────────────────────────────

@pytest.fixture
def client(store, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from api import routes_market, routes_strategy
    svc = MarketStateService(store, Config(iv_auto=False), FakeBroker())
    monkeypatch.setenv("SIMULATION", "true")
    monkeypatch.setattr(routes_market, "market_state", svc)
    monkeypatch.setattr(routes_strategy, "market_state", svc)
    return TestClient(main.app), svc, routes_strategy.strategy_engine


def test_api_state_journal_stats_and_validation(client):
    c, svc, _ = client
    assert c.get("/api/market/state").json()["ready"] is False
    assert c.post("/api/market/iv", json={"iv": -1}).status_code == 422
    assert c.post("/api/market/iv", json={"iv": 18, "date": "nope"}).status_code == 422
    assert c.patch("/api/market/journal/2026-01-01", json={"notes": "x"}).status_code == 404
    assert c.get("/api/market/journal").json() == []
    assert c.get("/api/market/stats").json()["by_state"] == []

    r = c.post("/api/market/iv", json={"iv": 18.5})
    assert r.status_code == 200 and r.json()["iv"]["value"] == 18.5
    st = c.get("/api/market/state").json()
    assert st["ready"] is True and st["iv"]["source"] == "manual" and st["config"]["window"] == 60


def test_api_state_endpoints_share_one_shape(client, frozen):
    """GET /state 與 POST /refresh、POST /iv 必須同形狀（含 ready / config）。
    前端曾把 POST 回應直接當狀態用，缺 config 而整頁崩潰（This page couldn't load）。"""
    c, svc, _ = client
    before = c.get("/api/market/state").json()
    assert before["ready"] is False and "config" in before
    for resp in (c.post("/api/market/refresh"), c.post("/api/market/iv", json={"iv": 18.5})):
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True and "config" in body
        assert set(body) == set(c.get("/api/market/state").json())


def test_api_refresh_refuses_while_strategy_running(client, frozen):
    c, svc, engine = client
    engine.strategies["scalp"].state.is_running = True
    try:
        r = c.post("/api/market/refresh")
        assert r.status_code == 409 and "scalp" in r.json()["detail"]
        assert c.post("/api/market/refresh?force=true").status_code == 200
    finally:
        engine.strategies["scalp"].state.is_running = False


def test_api_journal_note_roundtrip(client, frozen):
    c, svc, _ = client
    c.post("/api/market/refresh")
    day = svc.summary["date"]
    assert c.patch(f"/api/market/journal/{day}", json={"basis": "急拉後回測", "notes": "N"}).status_code == 200
    row = c.get("/api/market/journal").json()[0]
    assert row["date"] == day and row["basis"] == "急拉後回測" and row["notes"] == "N"
    assert row["market_state"] and "summary_json" not in row


# ── 盤中即時狀態（live_snapshot）／成交紀錄標記 ───────────────────

def _live_with(monkeypatch, *, flow: str = "none", prices=(), day: int = 7) -> LiveState:
    """建一個餵好資料的 LiveState 並換進 daily_summary。flow: buy / sell / mixed / none（各 100 筆真實成交）。"""
    ls = LiveState()
    t0 = time.mktime((2026, 10, day, 10, 0, 0, 0, 0, -1))
    for i in range(100 if flow != "none" else 0):
        side = 1 if flow == "buy" else 2 if flow == "sell" else (1 if i % 2 else 2)
        ls.feed("TMFJ6", 100.0, 1, i + 1, side, t0 + i)
    for j, p in enumerate(prices):
        ls.feed("TMFJ6", p, 0, 0, 0, t0 + 200 + j)
    monkeypatch.setattr(ds, "live_state", ls)
    return ls


def _summary(svc, state: str, direction: int) -> None:
    svc.summary = {"date": "2026-10-07", "state": state, "direction": direction, "hint": "H",
                   "hurst": {"label": "x", "state": state}, "iv": {"state": "UNKNOWN"}}


@pytest.mark.parametrize("state,direction,flow,want,coherence,word", [
    ("TREND", 1, "buy", 1, 1, "協調"),           # 盤前偏向做多、現在買方主動
    ("TREND", 1, "sell", 1, -1, "矛盾"),
    ("REVERT", 1, "sell", -1, 1, "協調"),        # 均值回歸 + 日K偏多 → 偏向做空；賣方主動 = 協調
    ("REVERT", 1, "buy", -1, -1, "矛盾"),
    ("TREND", -1, "sell", -1, 1, "協調"),
    ("TREND", 1, "mixed", 1, 0, "中性"),
    ("UNCLEAR", 1, "buy", 0, None, "不偏向"),     # 今日判斷不操作 → 沒有可比較的方向
    ("TREND", 1, "none", 1, None, "尚無"),        # 還沒有成交資料
])
def test_live_coherence_with_pre_open_judgement(svc, frozen, monkeypatch, state, direction, flow, want, coherence, word):
    _live_with(monkeypatch, flow=flow)
    _summary(svc, state, direction)
    res = asyncio.run(svc.live_snapshot())
    assert res["pre"]["want"] == want and res["coherence"] == coherence and word in res["coherence_text"]


@pytest.mark.parametrize("prices,ratio,label", [
    ((100.0, 116.0), 1.6, "大波動"),      # 振幅 16 / 近 20 日均 10
    ((100.0, 108.0), 0.8, "正常"),
    ((100.0, 105.0), 0.5, "清淡"),
])
def test_live_range_ratio_and_labels(svc, store, frozen, monkeypatch, prices, ratio, label):
    _seed_bars(store, [20000.0] * 25)                    # 每根日K振幅 10 → 近 20 日均 10
    _live_with(monkeypatch, prices=prices)
    _summary(svc, "TREND", 1)
    res = asyncio.run(svc.live_snapshot())
    assert res["avg_range"] == 10.0 and res["range_ratio"] == ratio and res["range_label"] == label


def test_live_range_is_hidden_when_it_is_not_todays_session(svc, store, frozen, monkeypatch):
    _seed_bars(store, [20000.0] * 25)
    _live_with(monkeypatch, prices=(100.0, 150.0), day=6)         # 資料是昨天日盤的
    _summary(svc, "TREND", 1)
    res = asyncio.run(svc.live_snapshot())
    assert res["range"] is None and res["range_ratio"] is None and res["session_day"] == "2026-10-06"


def test_live_endpoint_shape_and_empty_state(client, frozen, monkeypatch):
    c, svc, _ = client
    monkeypatch.setattr(ds, "live_state", LiveState())
    empty = c.get("/api/market/live").json()
    assert empty["ready"] is False and empty["flow"]["100"]["share"] is None
    _live_with(monkeypatch, flow="buy", prices=(100.0, 110.0))
    _summary(svc, "TREND", 1)
    body = c.get("/api/market/live").json()
    assert body["ready"] is True and body["flow"]["100"]["share"] == 1.0
    assert {"range", "avg_range", "range_ratio", "pre", "coherence", "coherence_text", "thresholds"} <= set(body)


def test_live_state_failure_never_blocks_quote_dispatch(monkeypatch):
    """即時狀態只是顯示用：feed 丟例外，報價仍要派發給策略（真錢路徑不能被它拖累）。"""
    import core.quote_hub as qh
    hub, got = qh.QuoteHub(), []

    async def cb(snapshot):
        got.append(snapshot["close"])

    async def run():
        hub.setup(asyncio.get_running_loop())
        hub.subscribe_strategy("t", cb)
        monkeypatch.setattr(qh.live_state, "feed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        hub._inject_quote({"code": "TMFJ6", "close": 100.0, "volume": 1, "total_volume": 5,
                           "tick_type": 1, "ts": time.time()})
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert got == [100.0]


def test_quote_hub_feeds_live_state_and_exposes_last_price_by_prefix(monkeypatch):
    import core.quote_hub as qh
    ls = LiveState()
    monkeypatch.setattr(qh, "live_state", ls)
    hub = qh.QuoteHub()
    hub._inject_quote({"code": "TMFJ6", "close": 49700.0, "volume": 2, "total_volume": 10, "tick_type": 1, "ts": time.time()})
    hub._inject_quote({"code": "TXFJ6", "close": 49701.0, "volume": 1, "total_volume": 7, "tick_type": 2, "ts": time.time()})
    assert ls.snapshot()["flow"]["100"]["n"] == 1                      # 只追蹤 TMF
    assert hub.last_price_by_prefix("TMF") == 49700.0 and hub.last_price_by_prefix("TXF") == 49701.0
    assert hub.last_price_by_prefix("MXF") is None


def test_summary_updates_tag_the_trade_log(svc, frozen, monkeypatch, tmp_path):
    t = TradeLog(tmp_path / "tag.db")
    monkeypatch.setattr(ds, "trade_log", t)
    asyncio.run(svc.refresh("pre"))
    assert t._tag["market_state"] == svc.summary["state"]
    assert t._tag["as_of"] == "2026-10-07" and t._tag["hurst_state"] == svc.summary["hurst"]["state"]


def test_tradelog_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from api import routes_tradelog
    monkeypatch.setenv("SIMULATION", "true")
    t = TradeLog(tmp_path / "api.db")
    monkeypatch.setattr(routes_tradelog, "trade_log", t)
    t.start()
    with t.context(strategy="scalp", reason="sl", signal_price=100.0, ref_price=100.0):
        t.record_order(contract="TMF", action="Sell", qty=1, order_type="IOC", trade_id="A", status="PendingSubmit")
    t.record_event({"state": "FuturesDeal", "trade_id": "A", "price": 97.0, "quantity": 1})
    t.stop()
    c = TestClient(main.app)
    rows = c.get("/api/tradelog/orders?limit=10").json()
    assert rows[0]["strategy"] == "scalp" and rows[0]["slip_ref"] == 3.0 and rows[0]["outcome"] == "filled"
    assert c.get("/api/tradelog/orders?strategy=nope").json() == []
    g = c.get("/api/tradelog/summary?days=1").json()["groups"][0]
    assert (g["strategy"], g["reason"], g["slip_ref"]["max"]) == ("scalp", "sl", 3.0)
