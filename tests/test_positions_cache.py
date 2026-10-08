"""部位／已實現損益的背景刷新（api/routes_position.py）。

背景：這兩個快取原本沒有任何寫入者，儀表板的〈部位面板〉因此永遠顯示「目前無持倉」。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import api.routes_position as rp
from api.routes_strategy import strategy_engine
from core.broker import broker
from core.shioaji_worker import _extract_profit_loss

POS = {"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": 100.0, "last_price": 101.0, "pnl": 10.0}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setitem(rp._cache, "positions", [])
    monkeypatch.setitem(rp._cache, "pnl", [])
    monkeypatch.setitem(rp._cache, "positions_at", 0.0)
    monkeypatch.setitem(rp._cache, "pnl_at", 0.0)
    monkeypatch.setattr(rp, "_pnl_try_at", 0.0)


def test_refresh_updates_cache_and_meta(monkeypatch):
    async def fake():
        return [POS]

    monkeypatch.setattr(broker, "list_positions", fake)
    assert rp.get_meta()["positions_age_sec"] == -1                 # 從未刷新
    assert asyncio.run(rp.refresh_positions()) is True
    assert rp.get_positions() == [POS]
    assert 0 <= rp.get_meta()["positions_age_sec"] < 5


def test_failed_refresh_keeps_old_data_and_timestamp(monkeypatch):
    async def ok():
        return [POS]

    async def boom():
        raise asyncio.TimeoutError()

    monkeypatch.setattr(broker, "list_positions", ok)
    asyncio.run(rp.refresh_positions())
    at = rp._cache["positions_at"]
    monkeypatch.setattr(broker, "list_positions", boom)
    assert asyncio.run(rp.refresh_positions()) is False
    assert rp.get_positions() == [POS] and rp._cache["positions_at"] == at     # 保留舊值，時效不更新


def test_flat_account_is_a_real_empty_list_not_missing_data(monkeypatch):
    async def flat():
        return []

    monkeypatch.setattr(broker, "list_positions", flat)
    asyncio.run(rp.refresh_positions())
    assert rp.get_positions() == [] and rp.get_meta()["positions_age_sec"] >= 0   # 前端靠時效區分「無持倉」與「沒資料」


def test_refresh_pnl_and_extraction(monkeypatch):
    row = _extract_profit_loss(SimpleNamespace(
        id=7, code="TMFJ6", direction=SimpleNamespace(value="Buy"), quantity=2, entry_price=100.0,
        cover_price=110.0, pnl=200.0, fee=30.0, tax=4.0, date="2026-10-08"))
    assert row == {"id": 7, "code": "TMFJ6", "direction": "Buy", "quantity": 2, "price": 100.0, "cover_price": 110.0,
                   "pnl": 200.0, "fee": 30.0, "tax": 4.0, "date": "2026-10-08", "dseq": "7"}

    async def fake():
        return [row]

    monkeypatch.setattr(broker, "list_profit_loss", fake)
    assert asyncio.run(rp.refresh_pnl()) is True
    assert rp.get_pnl() == [row] and rp.get_meta()["pnl_age_sec"] >= 0


def test_pnl_refresh_backs_off_while_a_strategy_is_running():
    assert rp._pnl_interval() == rp.PNL_REFRESH_IDLE_SEC
    s = strategy_engine.strategies["scalp"]
    s.state.is_running = True
    try:
        assert rp._pnl_interval() == rp.PNL_REFRESH_BUSY_SEC        # 策略執行中降頻，別搶 worker 的時間
    finally:
        s.state.is_running = False


def test_loop_throttles_pnl_queries_even_when_they_fail(monkeypatch):
    calls = {"pos": 0, "pnl": 0, "sleeps": 0}

    async def pos():
        calls["pos"] += 1
        return [POS]

    async def pnl():
        calls["pnl"] += 1
        raise RuntimeError("simulation 不支援")

    async def fake_sleep(sec):
        calls["sleeps"] += 1
        if calls["sleeps"] >= 6:                                    # 前 1 次是等待啟動，之後每輪一次
            raise asyncio.CancelledError()

    monkeypatch.setattr(broker, "list_positions", pos)
    monkeypatch.setattr(broker, "list_profit_loss", pnl)
    monkeypatch.setattr(broker, "_is_connected", True)
    monkeypatch.setattr(rp, "asyncio", SimpleNamespace(sleep=fake_sleep, wait_for=asyncio.wait_for))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rp.positions_refresh_loop())
    assert calls["pos"] == 5                                        # 每輪都刷新部位
    assert calls["pnl"] == 1                                        # 損益只嘗試一次（失敗也不會每輪重打 worker）
