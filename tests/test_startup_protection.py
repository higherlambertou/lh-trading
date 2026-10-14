"""後端（重新）啟動時的持倉保護（core/startup_protection.py）：恢復手動停損、報告沒有保護的持倉、推播。"""
from __future__ import annotations

import asyncio
import logging

import pytest

import core.startup_protection as sp
from core.broker import broker
from core.manual_monitor import ManualOrderMonitor


def pos(code="TMFJ6", direction="Buy", qty=1, price=49000.0):
    return {"code": code, "direction": direction, "quantity": qty, "price": price, "last_price": price}


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message, *, key=None, cooldown=0.0, priority="high", wait=False):
        self.calls.append({"title": title, "message": message, "key": key, "cooldown": cooldown, "priority": priority})
        return True


@pytest.fixture
def env(monkeypatch, tmp_path):
    rec = Recorder()
    mm = ManualOrderMonitor()
    mm.state_path = tmp_path / "w.json"
    monkeypatch.setattr(sp, "notify", rec)
    monkeypatch.setattr(sp, "manual_monitor", mm)
    return rec, mm


def restarted(mm):
    m = ManualOrderMonitor()
    m.state_path = mm.state_path
    return m


def test_restored_stops_are_reported_with_their_stop_price(env, monkeypatch):
    rec, mm = env
    mm.add("TMF", 1, 1, 49000.0, 50, 80, order_id="T1")
    monkeypatch.setattr(sp, "manual_monitor", restarted(mm))
    rep = asyncio.run(sp.protect_after_start([pos()]))
    assert rep["status"] == "ok" and len(rep["restored"]) == 1 and rep["unprotected"] == []
    (c,) = rec.calls
    assert c["title"] == "後端已重啟：持倉的停損監控已恢復" and c["priority"] == "high" and c["cooldown"] == sp.REPEAT_COOLDOWN
    assert "已恢復手動停損監控：TMF 多 1 口，停損 50 點（約 48,950）、停利 80 點。" in c["message"] and c["key"].startswith("startup:")


def test_a_position_nobody_protects_is_an_urgent_warning(env):
    rec, _ = env
    rep = asyncio.run(sp.protect_after_start([pos(qty=2)]))
    assert rep["status"] == "none" and rep["unprotected"] == [{"code": "TMFJ6", "direction": "Buy", "quantity": 2, "total": 2}]
    (c,) = rec.calls
    assert c["title"] == "⚠️ 後端已重啟：持倉沒有停損保護" and c["priority"] == "urgent" and "TMFJ6 多 2 口（這些口數沒有策略在跑、也沒有停損監控覆蓋）" in c["message"]


def test_partly_protected_reports_both(env, monkeypatch):
    rec, mm = env
    mm.add("TMF", 1, 1, 49000.0, 50, 0, order_id="T1")
    monkeypatch.setattr(sp, "manual_monitor", restarted(mm))
    asyncio.run(sp.protect_after_start([pos(qty=1), pos(code="MXFJ6", qty=1)]))
    (c,) = rec.calls
    assert c["title"].startswith("⚠️") and "已恢復手動停損監控：TMF 多 1 口" in c["message"] and "沒有停損保護：MXFJ6 多 1 口" in c["message"]


def test_no_position_means_no_message(env, monkeypatch):
    rec, mm = env
    mm.add("TMF", 1, 1, 49000.0, 50, 0, order_id="T1")
    monkeypatch.setattr(sp, "manual_monitor", restarted(mm))
    rep = asyncio.run(sp.protect_after_start([]))
    assert rep["status"] == "ok" and rep["dropped"][0]["reason"] == "部位已不存在" and rec.calls == []     # 沒有持倉就不打擾


def test_an_unreachable_broker_is_reported_with_the_number_of_saved_stops(env, monkeypatch):
    rec, mm = env
    mm.add("TMF", 1, 1, 49000.0, 50, 0, order_id="T1")
    mm.add("MXF", -1, 1, 49000.0, 40, 0, order_id="T2")
    before = mm.state_path.read_bytes()
    monkeypatch.setattr(sp, "manual_monitor", restarted(mm))
    monkeypatch.setattr(type(broker), "is_connected", property(lambda self: False))
    rep = asyncio.run(sp.protect_after_start(None, wait_sec=0.03))
    assert rep["status"] == "broker_unavailable" and mm.state_path.read_bytes() == before
    (c,) = rec.calls
    assert c["title"] == "⚠️ 後端已重啟，但連不上券商：停損監控還沒恢復" and "落地的 2 個手動停損監控" in c["message"] and c["priority"] == "urgent"


def test_nothing_saved_and_broker_unreachable_stays_quiet(env, monkeypatch):
    rec, _ = env
    monkeypatch.setattr(type(broker), "is_connected", property(lambda self: False))
    assert asyncio.run(sp.protect_after_start(None, wait_sec=0.03))["status"] == "none" and rec.calls == []


def test_the_same_situation_has_the_same_key_and_a_different_one_has_another(env):
    rec, _ = env
    asyncio.run(sp.protect_after_start([pos()]))
    asyncio.run(sp.protect_after_start([pos()]))
    asyncio.run(sp.protect_after_start([pos(qty=2)]))
    assert rec.calls[0]["key"] == rec.calls[1]["key"] != rec.calls[2]["key"]                # 同樣的狀況 10 分鐘內只推一次；變了就再推


def test_errors_are_logged_and_never_raised(env, monkeypatch, caplog):
    rec, mm = env

    async def boom(*a, **k):
        raise RuntimeError("壞了")

    monkeypatch.setattr(mm, "restore", boom)
    with caplog.at_level(logging.ERROR):
        rep = asyncio.run(sp.protect_after_start([pos()]))
    assert rep == {"status": "error"} and "持倉可能沒有停損保護" in caplog.text and rec.calls == []


def test_cancellation_is_not_swallowed(env, monkeypatch):
    _, mm = env

    async def cancelled(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mm, "restore", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sp.protect_after_start([pos()]))


def test_a_strategy_is_never_assumed_to_be_running_at_startup(env):
    """策略不會自動恢復：重啟當下一律當沒有策略，微台持倉也要警告。"""
    rec, _ = env
    assert asyncio.run(sp.protect_after_start([pos(code="TMFK6")]))["unprotected"][0]["code"] == "TMFK6" and len(rec.calls) == 1
