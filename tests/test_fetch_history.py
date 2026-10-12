"""core/hurst_study.py 的 fetch_history：向券商抓歷史 1 分 K（唯讀、獨立登入）。

這個函式會用到券商登入與流量，所以重點是『行為』：只登入一次、一定登出、連線數太多就不登入、分段涵蓋的範圍剛好
（不漏、不重複、不超過 until）、抓很多段時兩段之間有間隔。用假的 shioaji 模組驗證，不碰真券商。"""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta

import pytest

import core.hurst_study as hs
from core.market_store import MarketStore
from test_market_state import make_kbars

TODAY = date.today()


class Runaway(BaseException):                      # 不是 Exception：fetch_history 會吞掉 Exception 並重試，這個要能直接衝出來讓測試失敗而不是卡死
    pass


class FakeAPI:
    def __init__(self, fail_all=False):
        self.calls, self.logins, self.logouts, self.fail_all = [], 0, 0, fail_all
        self.Contracts = types.SimpleNamespace(Futures=types.SimpleNamespace(TXF=types.SimpleNamespace(TXFR1="CONTRACT")))

    def login(self, **kw):
        self.logins += 1

    def logout(self):
        self.logouts += 1

    def kbars(self, contract, start, end, timeout):
        self.calls.append((start, end))
        if len(self.calls) > 300:
            raise Runaway("分段迴圈沒有往前走")
        if self.fail_all:
            raise TimeoutError("伺服器逾時")
        raw = make_kbars(date.fromisoformat(start), date.fromisoformat(end) - timedelta(days=1))      # end 是開區間
        return types.SimpleNamespace(ts=raw["ts"], Open=raw["open"], High=raw["high"], Low=raw["low"], Close=raw["close"], Volume=raw["volume"])


@pytest.fixture
def rig(monkeypatch, tmp_path):
    api = FakeAPI()
    created = []

    def shioaji(simulation):
        assert simulation is True                                           # 一律模擬盤登入（唯讀），不碰正式盤的連線
        created.append(api)
        return api

    monkeypatch.setitem(sys.modules, "shioaji", types.SimpleNamespace(Shioaji=shioaji))
    monkeypatch.setenv("SHIOAJI_API_KEY", "k")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
    monkeypatch.setattr("core.broker_guard.room_for_login", lambda *a, **k: (True, "ok"))
    sleeps = []
    monkeypatch.setattr(hs.time, "sleep", lambda s: sleeps.append(s))
    return types.SimpleNamespace(api=api, created=created, sleeps=sleeps, store=MarketStore(tmp_path / "ms.db"))


def test_it_logs_in_once_logs_out_and_covers_the_range_without_gaps_or_overlaps(rig):
    until = TODAY - timedelta(days=40)
    bars, mins = hs.fetch_history(100, store=rig.store, until=until)
    assert rig.api.logins == 1 and rig.api.logouts == 1 and len(rig.created) == 1
    spans = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in rig.api.calls]
    assert spans[0][0] == TODAY - timedelta(days=100)
    assert spans[-1][1] == until + timedelta(days=1)                                                   # 不超過 until
    for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
        assert a2 == b1 + timedelta(days=1)                                                             # 下一段接在上一段後面一天，不漏不重複
    assert all((b - a).days <= 25 for a, b in spans)                                                   # 每段不超過 25 天（券商上限 30）
    assert bars > 20 and mins == bars * 301
    assert rig.store.days_1m("TXF") == bars


def test_without_until_it_runs_up_to_today(rig):
    hs.fetch_history(30, store=rig.store)
    assert date.fromisoformat(rig.api.calls[-1][1]) == TODAY + timedelta(days=1)


def test_a_pause_is_taken_between_chunks_when_asked(rig):
    hs.fetch_history(80, store=rig.store, until=TODAY - timedelta(days=5), pause=1.5)
    assert len(rig.sleeps) == len(rig.api.calls) and all(s == 1.5 for s in rig.sleeps)
    rig.sleeps.clear()
    hs.fetch_history(30, store=rig.store, until=TODAY - timedelta(days=5))
    assert rig.sleeps == []                                                                             # 短區間不用停


def test_it_always_logs_out_even_when_every_request_fails(rig):
    rig.api.fail_all = True
    bars, mins = hs.fetch_history(60, store=rig.store, until=TODAY - timedelta(days=10))
    assert (bars, mins) == (0, 0) and rig.api.logins == 1 and rig.api.logouts == 1
    assert len(rig.api.calls) >= 4 and rig.api.calls[0::2] == rig.api.calls[1::2]                       # 每一段失敗後重試一次（同一段連續兩次）


def test_it_does_not_log_in_when_the_backend_already_holds_many_connections(rig, monkeypatch):
    monkeypatch.setattr("core.broker_guard.room_for_login", lambda *a, **k: (False, "目前 4 條連線，不登入"))
    with pytest.raises(RuntimeError, match="不登入"):
        hs.fetch_history(60, store=rig.store)
    assert rig.api.logins == 0 and rig.created == []


def test_old_data_does_not_replace_newer_rows(rig):
    rig.store.upsert_bars_1m("TXF", [{"date": TODAY.isoformat(), "hhmm": 846, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 7}])
    hs.fetch_history(60, store=rig.store, until=TODAY - timedelta(days=30))
    assert any(r["date"] == TODAY.isoformat() and r["volume"] == 7 for r in rig.store.bars_1m("TXF"))   # 只抓到 until，更新的資料不動
