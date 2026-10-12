"""即時狀態的附加欄位（五檔價差／深度、交易所均價、基差、交易所累計內外盤、交易所日盤高低、早盤振幅追蹤）。

這些欄位本來就在報價事件裡（2026-10-12 實測），只是以前被丟掉；全部「只顯示」，壞了不能影響原本的買賣力道統計與報價派發。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

import core.live_state as lsm
from core.live_state import LiveState, book_stats, session_of


def ts(h: int, m: int, s: int = 0, day: int = 7) -> float:
    return time.mktime((2026, 10, day, h, m, s, 0, 0, -1))


def book(bid=100.0, ask=101.0, bv=(5, 2, 7, 7, 9), av=(7, 6, 17, 14, 8), dbv=0, dav=0):
    return {"bp": [bid - i for i in range(5)], "ap": [ask + i for i in range(5)], "bv": list(bv), "av": list(av), "dbv": dbv, "dav": dav}


def quote(price=100.0, **kw) -> dict:
    return {"code": "TMFJ6", "close": price, "high": kw.pop("high", 0), "low": kw.pop("low", 0), **kw}


def feed(ls: LiveState, t: float, price=100.0, vol=0, total=0, tick=1, code="TMFJ6", **q):
    ls.feed(code, price, vol, total, tick, t, quote=quote(price, **q))


# ── 五檔統計 ──────────────────────────────────────────────────────

def test_book_stats_spread_depth_and_imbalance():
    b = book_stats(book(100, 103, bv=(10, 0, 0, 0, 0), av=(0, 0, 0, 0, 30)))
    assert b["bid1"] == 100 and b["ask1"] == 103 and b["spread"] == 3 and b["bv1"] == 10 and b["av1"] == 0
    assert b["bv5"] == 10 and b["av5"] == 30 and b["imb5"] == -0.5                        # 賣方掛得比較厚 → 負
    assert book_stats(book(bv=(8, 0, 0, 0, 0), av=(2, 0, 0, 0, 0)))["imb5"] == 0.6


@pytest.mark.parametrize("bid,ask", [(101.0, 101.0), (102.0, 101.0), (0.0, 101.0)])
def test_unusable_spread_is_none_not_negative(bid, ask):
    assert book_stats(book(bid, ask))["spread"] is None


def test_garbage_book_is_none_and_empty_depth_has_no_imbalance():
    assert book_stats(None) is None and book_stats({}) is None
    assert book_stats({"bp": [], "ap": [1], "bv": [1], "av": [1]}) is None
    assert book_stats({"bp": ["x"], "ap": [1], "bv": [1], "av": [1]}) is None
    assert book_stats(book(bv=(0, 0, 0, 0, 0), av=(0, 0, 0, 0, 0)))["imb5"] is None


def test_book_view_averages_only_the_last_window_and_reports_age(monkeypatch):
    ls, t = LiveState(), ts(10, 0)
    feed(ls, t, book=book(100, 104))                                        # 40 秒前：價差 4，已在視窗外
    feed(ls, t + 40, book=book(100, 102))
    feed(ls, t + 45, book=book(100, 103, bv=(30, 0, 0, 0, 0), av=(10, 0, 0, 0, 0)))
    monkeypatch.setattr(lsm.time, "time", lambda: t + 50)
    v = ls.snapshot()["book"]
    assert v["spread"] == 3 and v["n"] == 2 and v["spread_avg"] == 2.5 and v["window_sec"] == 30
    assert v["age"] == 5.0 and v["imb5_avg"] == pytest.approx((v["imb5"] + book_stats(book(100, 102))["imb5"]) / 2, abs=1e-3)
    monkeypatch.setattr(lsm.time, "time", lambda: t + 200)                    # 很久沒事件：視窗內沒有東西，平均為 None，但最新值還在
    v2 = ls.snapshot()["book"]
    assert v2["n"] == 0 and v2["spread_avg"] is None and v2["spread"] == 3 and v2["age"] == 155.0


def test_no_quote_or_no_book_means_no_extra_sections():
    ls = LiveState()
    ls.feed("TMFJ6", 100.0, 1, 1, 1, ts(8, 46))                              # 舊式呼叫（沒有 quote）；從開盤就在跑
    snap = ls.snapshot()
    assert snap["book"] is None and snap["session_flow"] is None and snap["vwap"] is None and snap["basis"] is None
    assert snap["ex_range"] is None and snap["cp"] is not None               # 日盤中，早盤振幅追蹤照常
    assert snap["flow"]["100"]["n"] == 1                                     # 原本的買賣力道統計不受影響


def test_other_contracts_are_ignored_for_the_extra_sections():
    ls = LiveState()
    feed(ls, ts(10, 0), code="MXFJ6", book=book(), avg_price=1.0)
    assert ls.snapshot()["book"] is None and ls.snapshot()["vwap"] is None


# ── 交易所均價、基差、累計內外盤 ────────────────────────────────────

def test_vwap_session_flow_and_basis_during_the_day():
    ls = LiveState()
    feed(ls, ts(10, 0), price=49300.0, avg_price=49353.5, underlying_price=49290.0, bid_side_total_vol=4000, ask_side_total_vol=6000)
    s = ls.snapshot()
    assert s["vwap"] == {"price": 49353.5, "diff": -53.5, "session": "day"}
    assert s["basis"] == {"spot": 49290.0, "value": 10.0, "valid": True}
    assert s["session_flow"] == {"inside": 4000, "outside": 6000, "share": 0.6, "session": "day"}


@pytest.mark.parametrize("h,m,valid", [(8, 50, False), (8, 59, False), (9, 0, True), (13, 29, True), (13, 30, False), (13, 40, False), (21, 0, False)])
def test_basis_is_only_shown_while_the_cash_market_is_open(h, m, valid):
    ls = LiveState()
    feed(ls, ts(h, m), price=49300.0, underlying_price=49290.0)
    assert ls.snapshot()["basis"]["valid"] is valid


def test_zero_values_do_not_overwrite_and_zero_cumulative_volume_is_legal():
    ls = LiveState()
    feed(ls, ts(10, 0), avg_price=49353.5, underlying_price=49290.0)
    feed(ls, ts(10, 1), avg_price=0, underlying_price=0)                      # 交易所還沒算出來：保留上一個
    assert ls.snapshot()["vwap"]["price"] == 49353.5 and ls.snapshot()["basis"]["spot"] == 49290.0
    ls2 = LiveState()
    feed(ls2, ts(8, 46), bid_side_total_vol=0, ask_side_total_vol=0)
    assert ls2.snapshot()["session_flow"] == {"inside": 0, "outside": 0, "share": None, "session": "day"}      # 時段剛開始：累計是 0


def test_night_session_is_labelled_and_never_touches_the_day_range():
    ls = LiveState()
    feed(ls, ts(16, 40), price=49290.0, high=49420.0, low=49280.0, avg_price=49353.0, bid_side_total_vol=8198, ask_side_total_vol=8374)
    s = ls.snapshot()
    assert s["vwap"]["session"] == "night" and s["session_flow"]["session"] == "night"
    assert s["high"] is None and s["ex_range"] is None and s["cp"] is None            # 日盤高低／早盤追蹤只屬於日盤
    assert session_of(1640) == "night" and session_of(300) == "night" and session_of(1000) == "day" and session_of(1400) == "other"


@pytest.mark.parametrize("hhmm,want", [(844, "other"), (845, "day"), (1345, "day"), (1346, "other"), (1459, "other"), (1500, "night"),
                                       (2359, "night"), (0, "night"), (459, "night"), (500, "other")])
def test_session_boundaries(hhmm, want):
    assert session_of(hhmm) == want


def test_events_between_the_sessions_never_touch_the_day_range():
    ls = LiveState()
    feed(ls, ts(14, 10), price=500.0, high=999.0, low=1.0)                       # 13:45~15:00 之間（不是日盤也不是夜盤）
    s = ls.snapshot()
    assert s["ex_range"] is None and s["high"] is None and s["cp"] is None


# ── 交易所的日盤高低 ──────────────────────────────────────────────

def test_exchange_range_is_not_used_when_we_have_been_running_since_the_open(caplog):
    ls = LiveState()
    feed(ls, ts(8, 45, 1), price=100.0, high=100.0, low=100.0)
    assert ls.snapshot()["ex_range"] == {"high": 100.0, "low": 100.0, "mismatch": 0}      # 第一筆：高＝低 也是有效的交易所值
    feed(ls, ts(9, 0), price=110.0, high=110.0, low=100.0)
    feed(ls, ts(9, 5), price=95.0, high=110.0, low=95.0)
    s = ls.snapshot()
    assert (s["high"], s["low"], s["range_source"], s["partial"]) == (110.0, 95.0, "own", False)
    assert s["ex_range"] == {"high": 110.0, "low": 95.0, "mismatch": 0}


def test_after_a_restart_the_exchange_range_completes_the_day(caplog):
    ls = LiveState()
    feed(ls, ts(10, 30), price=105.0, high=120.0, low=90.0)                   # 10:30 才啟動：自己只看到 105
    feed(ls, ts(10, 31), price=106.0, high=120.0, low=90.0)
    s = ls.snapshot()
    assert (s["high"], s["low"], s["range"]) == (120.0, 90.0, 30.0)
    assert s["range_source"] == "exchange" and s["partial"] is False and s["since"] is not None
    assert s["cp"] is None                                                    # 早盤那段我們沒看到 → 不能拿來預測
    assert s["ex_range"]["mismatch"] == 0                                     # 重啟後自己累計的本來就不完整，不拿來和交易所比、不算「不一致」


def test_exchange_range_that_does_not_cover_our_own_prices_is_ignored():
    ls = LiveState()
    feed(ls, ts(10, 30), price=105.0, high=104.0, low=90.0)                   # 交易所的高比我們看到的價格還低：矛盾
    s = ls.snapshot()
    assert s["range_source"] == "own" and s["partial"] is True and s["high"] == 105.0
    ls2 = LiveState()
    feed(ls2, ts(10, 30), price=105.0, high=120.0, low=106.0)                 # 低點也要蓋得住：交易所的低比我們看到的價格還高
    assert ls2.snapshot()["range_source"] == "own" and ls2.snapshot()["partial"] is True


def test_mismatch_with_the_exchange_is_counted_and_logged_a_few_times(caplog):
    ls = LiveState()
    feed(ls, ts(8, 45, 1), price=100.0, high=100.0, low=100.0)
    with caplog.at_level(logging.WARNING, logger="core.live_state"):
        for i in range(8):                                                    # 交易所的高低混進了我們沒看過的價格（例如試算價）
            feed(ls, ts(9, 0, i), price=101.0, high=103.0, low=100.0)
    assert ls.snapshot()["ex_range"]["mismatch"] == 8
    logged = [r for r in caplog.records if "與交易所不一致" in r.getMessage()]
    assert len(logged) == lsm.EX_MISMATCH_LOG_MAX                             # 不洗版


def test_a_new_day_resets_exchange_range_mismatch_count_and_checkpoints():
    ls = LiveState()
    feed(ls, ts(8, 45, 1), price=100.0, high=100.0, low=100.0)
    feed(ls, ts(9, 20), price=130.0, high=190.0, low=100.0)                   # 昨天有一次對不上
    assert ls.snapshot()["ex_range"]["mismatch"] == 1
    feed(ls, ts(8, 45, 1, day=8), price=200.0, high=0, low=0)                 # 隔天第一筆沒帶有效的交易所高低：昨天的不能留著
    s = ls.snapshot()
    assert s["session_day"] == "2026-10-08" and s["ex_range"] is None
    feed(ls, ts(8, 45, 2, day=8), price=200.0, high=200.0, low=200.0)
    assert ls.snapshot()["ex_range"]["mismatch"] == 0                         # 昨天的次數不能帶到今天
    ls = LiveState()
    feed(ls, ts(8, 45, 1), price=100.0, high=100.0, low=100.0)
    feed(ls, ts(9, 20), price=130.0, high=130.0, low=100.0)
    feed(ls, ts(8, 45, 2, day=8), price=200.0, high=200.0, low=200.0)
    s = ls.snapshot()
    assert s["session_day"] == "2026-10-08" and s["high"] == 200.0 and s["ex_range"] == {"high": 200.0, "low": 200.0, "mismatch": 0}
    assert s["cp"]["30"] == {"early": 0.0, "post": None}


# ── 早盤／之後的振幅（給 intraday_context）────────────────────────

def test_checkpoints_split_the_day_at_30_and_60_minutes_from_0845():
    ls = LiveState()
    for h, m, p in [(8, 45, 100.0), (9, 14, 130.0), (9, 15, 90.0), (9, 44, 140.0), (9, 45, 80.0), (11, 0, 150.0)]:
        feed(ls, ts(h, m), price=p)
    cp = ls.snapshot()["cp"]
    assert cp["30"] == {"early": 30.0, "post": 70.0}        # 前 30 分（08:45~09:14）高 130／低 100；之後（09:15 起）高 150／低 80
    assert cp["60"] == {"early": 50.0, "post": 70.0}        # 前 60 分（~09:44）高 140／低 90；之後 80~150


@pytest.mark.parametrize("minute,partial", [(45, False), (50, False), (51, True), (59, True)])
def test_started_after_0850_counts_as_missing_the_open(minute, partial):
    ls = LiveState()
    h, m = (8, minute) if minute < 60 else (9, minute - 60)
    feed(ls, ts(h, m), price=100.0)
    assert ls.snapshot()["partial"] is partial and (ls.snapshot()["cp"] is None) is partial


def test_checkpoints_are_withheld_when_we_missed_the_open():
    ls = LiveState()
    feed(ls, ts(9, 30), price=100.0)
    assert ls.snapshot()["cp"] is None and ls.snapshot()["partial"] is True
    ls2 = LiveState()
    feed(ls2, ts(8, 50), price=100.0)                      # 08:50 以前啟動算數（與既有 partial 的寬限一致）
    assert ls2.snapshot()["cp"] is not None


# ── 不能拖累原本的功能 ────────────────────────────────────────────

def test_extra_processing_failure_never_stops_the_flow_statistics(monkeypatch):
    ls = LiveState()
    monkeypatch.setattr(ls, "_feed_extra", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    feed(ls, ts(10, 0), price=100.0, vol=2, total=10, tick=1, book=book())
    feed(ls, ts(10, 1), price=101.0, vol=1, total=11, tick=2, book=book())
    f = ls.snapshot()["flow"]["100"]
    assert (f["n"], f["buy"], f["sell"]) == (2, 1, 1) and ls.snapshot()["high"] == 101.0


def test_garbage_extra_values_do_not_break_anything():
    ls = LiveState()
    feed(ls, ts(10, 0), price=100.0, vol=1, total=1, tick=1, book={"bp": "x"}, avg_price="n/a", bid_side_total_vol="abc")
    s = ls.snapshot()
    assert s["flow"]["100"]["n"] == 1 and s["book"] is None


# ── QuoteHub／live_snapshot／WebSocket 的接線 ──────────────────────

def test_quote_hub_hands_the_whole_quote_to_live_state_and_the_book_to_the_recorder(monkeypatch):
    import core.quote_hub as qh
    seen_feed, seen_rec = [], []
    monkeypatch.setattr(qh, "live_state", type("L", (), {"feed": lambda self, *a, **k: seen_feed.append((a, k))})())
    monkeypatch.setattr(qh, "tick_recorder", type("R", (), {"record": lambda self, *a, **k: seen_rec.append((a, k))})())
    snap = {"code": "TMFJ6", "close": 49290.0, "volume": 1, "total_volume": 7, "tick_type": 2, "ts": 5.0, "book": book(49290.0, 49293.0),
            "avg_price": 49353.0, "underlying_price": 49301.96, "bid_side_total_vol": 8198, "ask_side_total_vol": 8374}
    qh.QuoteHub()._inject_quote(snap)
    assert seen_feed[0][1]["quote"] is snap
    row = seen_rec[0][1]["book"]
    assert row[:6] == (49290.0, 49293.0, 5, 7, 30, 52) and row[8:] == (8198, 8374, 49353.0, 49301.96)


def test_websocket_messages_do_not_carry_the_five_levels(monkeypatch):
    import core.quote_hub as qh
    hub = qh.QuoteHub()

    async def run():
        hub.setup(asyncio.get_running_loop())
        q: asyncio.Queue = asyncio.Queue()
        hub.add_ws_client(q)
        hub._inject_quote({"code": "TMFJ6", "close": 100.0, "volume": 1, "total_volume": 1, "tick_type": 1, "ts": time.time(),
                           "book": book(), "avg_price": 99.5, "change_price": -3.0})
        return json.loads(await asyncio.wait_for(q.get(), 1))

    msg = asyncio.run(run())
    assert "book" not in msg and msg["close"] == 100.0 and msg["change_price"] == -3.0 and msg["avg_price"] == 99.5


def test_book_row_failure_in_quote_hub_never_blocks_dispatch(monkeypatch):
    import core.quote_hub as qh
    hub, got = qh.QuoteHub(), []

    async def cb(snapshot):
        got.append(snapshot["close"])

    async def run():
        hub.setup(asyncio.get_running_loop())
        hub.subscribe_strategy("t", cb)
        monkeypatch.setattr(qh, "book_row", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
        hub._inject_quote({"code": "TMFJ6", "close": 100.0, "volume": 1, "total_volume": 5, "tick_type": 1, "ts": time.time(), "book": book()})
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert got == [100.0]


def test_live_snapshot_carries_the_context_and_survives_its_failure(monkeypatch, tmp_path):
    import core.daily_summary as ds
    import core.intraday_context as ic
    from core.market_store import MarketStore

    ls = LiveState()
    feed(ls, ts(8, 45, 1), price=100.0)
    monkeypatch.setattr(ds, "live_state", ls)
    data = ic.ContextData("2026-10-07", 500.0, {}, [1.0] * 20, 100, "2026-10-06")
    svc = ds.MarketStateService.__new__(ds.MarketStateService)
    svc.store = MarketStore(str(tmp_path / "m.db"))
    svc.cfg = ds.MarketConfig() if hasattr(ds, "MarketConfig") else type("C", (), {"code": "TXF"})()
    svc.summary = None
    svc._avg_range_cache = ("", None)
    svc.bias_direction = lambda bias: (0, "x")

    async def good(store, code, today):
        return data

    monkeypatch.setattr(ds.intraday_context, "get", good)
    monkeypatch.setattr(ds, "date", type("D", (), {"today": staticmethod(lambda: __import__("datetime").date(2026, 10, 7))}))
    res = asyncio.run(svc.live_snapshot())
    assert res["ctx"]["ready"] is True and "cp" in res and "book" in res and res["ctx"]["session"] in ("day", "night", "other")

    async def bad(store, code, today):
        raise RuntimeError("boom")

    monkeypatch.setattr(ds.intraday_context, "get", bad)
    res2 = asyncio.run(svc.live_snapshot())
    assert res2["ctx"] is None and res2["flow"]["100"]["n"] == 0 and res2["ready"] is True       # 時段脈絡壞了，其他欄位照常
