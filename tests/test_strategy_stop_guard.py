"""POST /api/strategy/{name}/stop：持倉時預設拒絕。

背景（2026-10-08）：BaseStrategy.stop() 會取消報價訂閱（策略不再檢查停損停利）並取消帳戶內所有未成交委託
（含 scalp 掛在券商的停利單、手動掛的限價單）；端點原本不檢查持倉、前端按鈕也沒有警告，
手冊還寫「停止後停損停利照常執行」——持倉時按停止，部位立刻沒有任何保護。"""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

import main
from api import routes_strategy
from api.routes_strategy import strategy_engine

NAMES = sorted(strategy_engine.strategies)


@pytest.fixture
def rig(monkeypatch):
    """假裝 name 這個策略正在執行、持有 position 口；stop() 換成不碰券商的替身，記錄有沒有被呼叫。"""
    calls: list[str] = []

    async def no_pnl():
        return None

    monkeypatch.setattr(routes_strategy, "_sample_pnl", no_pnl)

    def arm(name: str, position: int, entry: float = 49500.0):
        s = strategy_engine.strategies[name]
        monkeypatch.setattr(s.state, "is_running", True)
        monkeypatch.setattr(s.state, "position", position)
        monkeypatch.setattr(s.state, "entry_price", entry)

        async def fake_stop():
            calls.append(name)
            s.state.is_running = False

        monkeypatch.setattr(s, "stop", fake_stop)
        return s

    return TestClient(main.app), arm, calls


def test_flat_strategy_stops_normally(rig):
    c, arm, calls = rig
    arm("orb", 0)
    r = c.post("/api/strategy/orb/stop")
    assert r.status_code == 200 and r.json() == {"status": "stopped", "name": "orb"} and calls == ["orb"]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("position,side", [(2, "多 2 口"), (-1, "空 1 口")])
def test_holding_a_position_blocks_the_stop_with_a_clear_reason(rig, name, position, side):
    c, arm, calls = rig
    s = arm(name, position)
    r = c.post(f"/api/strategy/{name}/stop")
    assert r.status_code == 409 and calls == [] and s.state.is_running is True       # 沒停、還在跑、停損停利繼續
    detail = r.json()["detail"]
    assert side in detail and "49500" in detail                                         # 說清楚持有什麼
    assert "停損停利" in detail and "未成交委託" in detail and "force=true" in detail    # 後果與出口


def test_force_stops_anyway_and_warns_loudly(rig, caplog):
    c, arm, calls = rig
    arm("scalp", -3)
    with caplog.at_level(logging.WARNING, logger=routes_strategy.logger.name):
        r = c.post("/api/strategy/scalp/stop?force=true")
    body = r.json()
    assert r.status_code == 200 and calls == ["scalp"] and body["status"] == "stopped"
    assert "強制停止" in body["warning"] and "空 3 口" in body["warning"] and "沒有任何停損停利保護" in body["warning"]
    assert "強制停止" in caplog.text and "scalp" in caplog.text                         # 留紀錄：事後查得到誰在持倉時停掉


def test_force_on_a_flat_strategy_adds_no_warning(rig):
    c, arm, calls = rig
    arm("rsi", 0)
    assert c.post("/api/strategy/rsi/stop?force=true").json() == {"status": "stopped", "name": "rsi"}


def test_existing_errors_are_unchanged(rig):
    c, arm, calls = rig
    assert c.post("/api/strategy/nope/stop").status_code == 404
    assert c.post("/api/strategy/orb/stop").status_code == 400          # 沒在跑
    assert calls == []


def test_a_blocked_stop_leaves_the_position_state_untouched(rig):
    c, arm, _ = rig
    s = arm("vwap_revert", 1)
    c.post("/api/strategy/vwap_revert/stop")
    assert (s.state.position, s.state.entry_price, s.state.is_running) == (1, 49500.0, True)


def test_shutdown_is_never_blocked_by_an_open_position(rig):
    """系統關機走 stop_all()（不經端點）。持倉不能擋住關機，否則 watchdog 的 2 秒寬限一到就變成強殺。"""
    _, arm, calls = rig
    arm("scalp", 2)
    asyncio.run(strategy_engine.stop_all())
    assert "scalp" in calls
