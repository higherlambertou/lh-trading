"""走勢圖的 K 棒端點 GET /api/market/bars（update.md 發現 42）：flow_1m 的一列變成一根 K 棒、進行中那一分鐘、增量查詢、壞資料。只讀，不碰券商與下單。"""
from __future__ import annotations

import asyncio
import time

import pytest

import api.routes_market as rm
from core.flow_store import FlowRecorder


class FakeStore:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def flow_1m(self, code, since_ts=None, until_ts=None):
        self.calls.append((code, since_ts))
        return [r for r in self.rows if since_ts is None or r["ts"] >= since_ts]


def row(ts, o=100.0, h=102.0, lo=99.0, c=101.0, buy=3, sell=2, unk=1, bn=2, sn=1, un=1):
    return {"ts": ts, "open": o, "high": h, "low": lo, "close": c, "buy_vol": buy, "sell_vol": sell, "unk_vol": unk, "buy_n": bn, "sell_n": sn, "unk_n": un}


NOW = 1_760_000_000.0


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    rec = FlowRecorder()
    monkeypatch.setattr(rm, "flow_recorder", rec)
    return rec


def call(store, monkeypatch, minutes=1500, since=None):
    monkeypatch.setattr(rm.market_state, "store", store)
    return asyncio.run(rm.get_bars(minutes, since))


def test_a_row_becomes_a_bar(env, monkeypatch):
    store = FakeStore([row(int(NOW) - 120)])
    out = call(store, monkeypatch)
    assert out["contract"] == "TMF" and out["interval"] == 60 and out["now"] == NOW
    assert out["bars"] == [{"t": int(NOW) - 120, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 6, "b": 3, "s": 2, "n": 4}]
    assert store.calls == [("TMF", NOW - 1500 * 60)]


def test_since_is_used_for_incremental_queries(env, monkeypatch):
    store = FakeStore([row(int(NOW) - 600), row(int(NOW) - 120)])
    out = call(store, monkeypatch, since=NOW - 300)
    assert [b["t"] for b in out["bars"]] == [int(NOW) - 120]
    assert store.calls == [("TMF", NOW - 300)]


def test_rows_without_prices_are_dropped(env, monkeypatch):
    t = int(NOW) - 600
    rows = [row(t, o=0.0), row(t + 60, c=None), {"ts": t + 120}, row(t + 180)]
    assert [b["t"] for b in call(FakeStore(rows), monkeypatch)["bars"]] == [t + 180]


def test_the_minute_in_progress_is_included(env, monkeypatch):
    t = int(NOW) // 60 * 60
    env._thread = object()                                     # 讓 feed 認為已啟動（不真的開執行緒）
    env._detector.is_trade = lambda *a: True
    env.feed("TMFJ6", 101.0, 2, 10, 1, t + 5)
    env.feed("TMFJ6", 103.0, 1, 11, 2, t + 20)
    out = call(FakeStore([row(t - 60)]), monkeypatch)
    assert out["live"]["t"] == t and out["live"]["o"] == 101.0 and out["live"]["h"] == 103.0 and out["live"]["c"] == 103.0 and out["live"]["v"] == 3
    assert len(out["bars"]) == 1


def test_no_live_bar_when_nothing_traded(env, monkeypatch):
    assert call(FakeStore([]), monkeypatch)["live"] is None


def test_a_stale_live_bar_is_not_returned_for_an_incremental_query(env, monkeypatch):
    t = int(NOW) // 60 * 60
    env._thread = object()
    env._detector.is_trade = lambda *a: True
    env.feed("TMFJ6", 101.0, 1, 10, 1, t + 5)
    assert call(FakeStore([]), monkeypatch, since=NOW + 600)["live"] is None


def test_a_broken_recorder_does_not_break_the_endpoint(env, monkeypatch):
    class Boom:
        def current_row(self):
            raise RuntimeError("壞了")
    monkeypatch.setattr(rm, "flow_recorder", Boom())
    out = call(FakeStore([row(int(NOW) - 60)]), monkeypatch)
    assert out["live"] is None and len(out["bars"]) == 1


def test_query_limits_are_declared():
    import inspect
    src = inspect.getsource(rm.get_bars)
    assert "ge=5" in inspect.getsource(rm) and "le=4000" in inspect.getsource(rm)
    assert "place_order" not in src and "broker" not in src          # 只讀
