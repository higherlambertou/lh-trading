"""跨合約外盤占比（大台／小台／微台，同一個時間視窗並排）：core/live_state.py 的 TimedFlow 與 snapshot()["xc"]。

為什麼用時間視窗：不同合約的成交頻率差很多（夜盤實測 30 秒內微台 58 筆、大台 10 筆），
同樣「最近 100 筆」在微台是 1~2 分鐘、在大台可能是 10 分鐘，拿來比的是不同長度的時間。
"""
from __future__ import annotations

import asyncio
import time

import pytest

import core.live_state as lsm
from core.live_state import LiveState, TimedFlow, XC_MIN_TRADES

NOW = time.mktime((2026, 10, 7, 11, 0, 0, 0, 0, -1))      # 日盤 11:00（固定「現在」，snapshot 用 monkeypatch 的時鐘）


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(lsm.time, "time", lambda: NOW)


class Feeder:
    """替每個合約維護自己的 total_volume（交易所的日累計量），一筆一筆餵成交。"""

    def __init__(self, ls: LiveState):
        self.ls, self.total = ls, {}

    def trade(self, code: str, ago_sec: float, side: int, vol: int = 1, price: float = 100.0):
        self.total[code] = self.total.get(code, 0) + vol
        self.ls.feed(code, price, vol, self.total[code], side, NOW - ago_sec)

    def many(self, code: str, n: int, ago_sec: float, side: int, vol: int = 1):
        for i in range(n):
            self.trade(code, ago_sec - i * 0.1, side, vol)      # 稍微錯開時間，保持由舊到新


def win(snap, w, code):
    return snap["xc"]["windows"][str(w)][code]


# ── TimedFlow 本身 ────────────────────────────────────────────────

def test_timed_flow_counts_inside_the_window_and_drops_the_old():
    tf = TimedFlow(keep_sec=600)
    for ago, side, vol in [(700, 1, 9), (500, 1, 2), (200, 2, 3), (50, 1, 4)]:
        tf.add(NOW - ago, side, vol)
    assert len(tf._d) == 3                                              # 700 秒前的已被擠掉（保留 600 秒）
    assert tf.stats(NOW, 300) == {"n": 2, "buy_n": 1, "buy_vol": 4, "sell_vol": 3}
    assert tf.stats(NOW, 600) == {"n": 3, "buy_n": 2, "buy_vol": 6, "sell_vol": 3}
    assert tf.stats(NOW, 10) == {"n": 0, "buy_n": 0, "buy_vol": 0, "sell_vol": 0}


def test_window_edge_is_inclusive_and_just_outside_is_excluded():
    tf = TimedFlow(keep_sec=3600)
    tf.add(NOW - 300.5, 2, 1)                                           # 差半秒就出視窗
    tf.add(NOW - 300, 1, 1)                                             # 剛好在 5 分鐘前
    assert tf.stats(NOW, 300)["n"] == 1 and tf.stats(NOW, 301)["n"] == 2


def test_out_of_order_timestamps_are_clamped_so_the_window_stays_sorted():
    """時間戳偶爾亂序（後到的那筆更舊）：當作同一時刻，deque 永遠由舊到新；不能因為最後一筆很舊就漏掉前面視窗內的。"""
    tf = TimedFlow(keep_sec=3600)
    tf.add(NOW - 100, 1, 1)
    tf.add(NOW - 100.0008, 2, 1)                                        # 後到、差幾毫秒更舊
    tf.add(NOW - 400, 1, 1)                                             # 時鐘倒退很多：當作剛剛才到
    stamps = [t for t, _, _ in tf._d]
    assert stamps == sorted(stamps) and stamps[0] == NOW - 100
    assert tf.stats(NOW, 300)["n"] == 3 and tf.stats(NOW, 50)["n"] == 0


# ── 三個時間視窗 ──────────────────────────────────────────────────

def test_three_time_windows_for_the_main_contract():
    ls, f = LiveState(), Feeder(LiveState())
    f.ls = ls
    f.many("TMFJ6", 10, 2400, 1, vol=5)                                 # 40 分鐘前：早就不在任何視窗
    f.many("TMFJ6", 10, 1200, 2, vol=1)                                 # 20 分鐘前：只在 30 分視窗；內盤 10 口
    f.many("TMFJ6", 10, 600, 1, vol=2)                                  # 10 分鐘前：15、30 分視窗；外盤 20 口
    f.many("TMFJ6", 10, 120, 2, vol=3)                                  # 2 分鐘前：三個視窗；內盤 30 口
    s = ls.snapshot()
    assert (win(s, 5, "TMF")["n"], win(s, 5, "TMF")["share"]) == (10, 0.0)                     # 只有最後那 10 筆內盤
    assert (win(s, 15, "TMF")["n"], win(s, 15, "TMF")["share"]) == (20, 0.4)                   # 外盤 20／（20+30）
    assert (win(s, 30, "TMF")["n"], win(s, 30, "TMF")["share"]) == (30, pytest.approx(20 / 60, abs=1e-3))
    assert win(s, 30, "TMF")["vol"] == 60
    assert s["xc"]["min_trades"] == XC_MIN_TRADES and set(s["xc"]["windows"]) == {"5", "15", "30"}


def test_share_is_by_lots_not_by_trade_count():
    ls = LiveState()
    f = Feeder(ls)
    f.trade("TXFJ6", 100, 1, vol=50)                                    # 一筆 50 口外盤大單
    f.many("TXFJ6", 9, 90, 2, vol=1)                                    # 九筆 1 口內盤
    c = win(ls.snapshot(), 5, "TXF")
    assert c["n"] == 10 and c["share"] == pytest.approx(50 / 59, abs=1e-3)        # 筆數占比會是 10%，口數占比是 85%


def test_too_few_trades_shows_no_share():
    ls = LiveState()
    f = Feeder(ls)
    f.many("MXFJ6", XC_MIN_TRADES - 1, 100, 1)
    c = win(ls.snapshot(), 5, "MXF")
    assert c["n"] == XC_MIN_TRADES - 1 and c["share"] is None and c["vol"] == XC_MIN_TRADES - 1      # 數字還在，但占比不顯示
    f.trade("MXFJ6", 50, 1)
    assert win(ls.snapshot(), 5, "MXF")["share"] == 1.0                                              # 第 10 筆進來就顯示


# ── 微台 − 大台 ───────────────────────────────────────────────────

def test_gap_is_micro_minus_big_and_none_when_either_side_is_thin():
    ls = LiveState()
    f = Feeder(ls)
    f.many("TMFJ6", 10, 100, 1, vol=4)                                  # 微台：全外盤 → 1.0
    f.many("TXFJ6", 8, 100, 1, vol=1)                                   # 大台才 8 筆 → 不顯示
    f.many("TXFJ6", 2, 90, 2, vol=1)
    s = ls.snapshot()
    assert win(s, 5, "TMF")["share"] == 1.0 and win(s, 5, "TXF")["share"] == pytest.approx(0.8, abs=1e-3)
    assert s["xc"]["windows"]["5"]["gap"] == pytest.approx(0.2, abs=1e-3)       # 1.0 − 0.8
    ls2, f2 = LiveState(), None
    f2 = Feeder(ls2)
    f2.many("TMFJ6", 10, 100, 1)
    f2.many("TXFJ6", 5, 100, 2)                                         # 大台成交太少
    assert ls2.snapshot()["xc"]["windows"]["5"]["gap"] is None


def test_gap_is_negative_when_the_big_contract_is_more_aggressive():
    ls = LiveState()
    f = Feeder(ls)
    f.many("TMFJ6", 10, 100, 2)                                         # 微台全內盤 → 0.0
    f.many("TXFJ6", 10, 100, 1)                                         # 大台全外盤 → 1.0
    assert ls.snapshot()["xc"]["windows"]["5"]["gap"] == -1.0


# ── 小台／大台不能干擾微台原本的東西 ───────────────────────────────

def test_other_contracts_only_feed_the_comparison():
    ls = LiveState()
    f = Feeder(ls)
    f.many("TXFJ6", 12, 100, 1, vol=2)
    f.many("MXFJ6", 12, 100, 2, vol=1)
    s = ls.snapshot()
    assert s["last"] is None and s["high"] is None and s["session_day"] == "" and s["cp"] is None      # 沒有微台的行情
    assert s["flow"]["100"]["n"] == 0 and s["book"] is None
    assert win(s, 5, "TXF")["n"] == 12 and win(s, 5, "MXF")["n"] == 12 and win(s, 5, "TMF")["n"] == 0


def test_main_contract_state_is_unchanged_by_the_comparison_feature():
    """同一串微台成交：逐筆視窗（20/100/300 筆）與新的時間視窗看到同一批成交。"""
    ls = LiveState()
    f = Feeder(ls)
    f.many("TMFJ6", 15, 100, 1, vol=2)
    f.many("TMFJ6", 5, 50, 2, vol=1)
    s = ls.snapshot()
    assert s["flow"]["100"]["n"] == 20 and s["flow"]["100"]["vol_share"] == pytest.approx(30 / 35, abs=1e-3)
    assert win(s, 5, "TMF")["n"] == 20 and win(s, 5, "TMF")["share"] == pytest.approx(30 / 35, abs=1e-3)       # 兩種算法同一個口數占比


def test_duplicates_quote_updates_and_unknown_direction_are_not_counted_for_other_contracts():
    ls = LiveState()
    t = NOW - 100
    ls.feed("TXFJ6", 100.0, 3, 10, 1, t)                                # 真實成交：外盤 3 口
    ls.feed("TXFJ6", 100.0, 3, 10, 1, t + 1)                            # 重複回報（total_volume 沒增加）
    ls.feed("TXFJ6", 100.0, 0, 10, 1, t + 2)                            # 報價更新（volume=0）
    ls.feed("TXFJ6", 100.0, 2, 12, 0, t + 3)                            # 成交但方向不明（tick_type=0）
    ls.feed("TXFJ6", 100.0, 1, 13, 2, t + 4)                            # 內盤 1 口
    c = win(ls.snapshot(), 5, "TXF")
    assert c["n"] == 2 and c["vol"] == 4


def test_unknown_contract_codes_and_zero_prices_are_ignored():
    ls = LiveState()
    ls.feed("TXOJ6C49000", 100.0, 1, 1, 1, NOW - 10)                    # 選擇權（TXO 不是 TXF 開頭）
    ls.feed("ZZZ", 100.0, 1, 1, 1, NOW - 10)
    ls.feed("TXFJ6", 0.0, 1, 1, 1, NOW - 10)                            # 沒有價格
    assert all(win(ls.snapshot(), 5, p)["n"] == 0 for p in ("TMF", "MXF", "TXF"))


def test_memory_is_bounded_by_the_longest_window():
    ls = LiveState()
    f = Feeder(ls)
    for i in range(0, 7200, 5):                                         # 兩小時、每 5 秒一筆
        f.trade("TMFJ6", 7200 - i, 1)
    assert len(ls._xc["TMF"]._d) <= 30 * 60 // 5 + 1


# ── 與 QuoteHub 的接線 ────────────────────────────────────────────

def test_quote_hub_feeds_every_contract_into_the_comparison(monkeypatch):
    import core.quote_hub as qh
    ls = LiveState()
    monkeypatch.setattr(qh, "live_state", ls)
    hub = qh.QuoteHub()
    for i, code in enumerate(("TMFJ6", "MXFJ6", "TXFJ6")):
        hub._inject_quote({"code": code, "close": 100.0, "volume": 2, "total_volume": 10 + i, "tick_type": 1, "ts": NOW - 30})
    s = ls.snapshot()
    assert [win(s, 5, p)["n"] for p in ("TMF", "MXF", "TXF")] == [1, 1, 1]


def test_live_snapshot_carries_the_comparison(monkeypatch, tmp_path):
    import core.daily_summary as ds
    from core.market_store import MarketStore
    ls = LiveState()
    Feeder(ls).many("TMFJ6", 10, 100, 1)
    monkeypatch.setattr(ds, "live_state", ls)
    svc = ds.MarketStateService.__new__(ds.MarketStateService)
    svc.store = MarketStore(str(tmp_path / "m.db"))
    svc.cfg = type("C", (), {"code": "TXF"})()
    svc.summary = None
    svc._avg_range_cache = ("", None)
    svc.bias_direction = lambda bias: (0, "x")
    res = asyncio.run(svc.live_snapshot())
    assert res["xc"]["windows"]["5"]["TMF"]["share"] == 1.0 and "gap" in res["xc"]["windows"]["5"]
