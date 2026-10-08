"""POST /api/strategy/{name}/stop：持倉時預設拒絕。

背景（2026-10-08）：BaseStrategy.stop() 會取消報價訂閱（策略不再檢查停損停利）並取消帳戶內所有未成交委託
（含 scalp 掛在券商的停利單、手動掛的限價單）；端點原本不檢查持倉、前端按鈕也沒有警告，
手冊還寫「停止後停損停利照常執行」——持倉時按停止，部位立刻沒有任何保護。

第二層（同日稍晚）：策略自己記的部位可能是錯的——2026-10-08 成交回報漏接，scalp 以為自己空手，按停止沒被攔下，
帳上留著 1 口沒人管的多單。所以除了 state.position，也要向券商確認實際持倉；查不到就先不停止。"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
import strategies.base as strategy_base
from api import routes_strategy
from api.routes_strategy import strategy_engine

NAMES = sorted(strategy_engine.strategies)


@pytest.fixture(autouse=True)
def broker_positions(monkeypatch):
    """假的券商部位查詢（預設空手）。測試絕不能碰真的 broker；要模擬券商有部位／查詢失敗的測試自己改它。"""
    box = SimpleNamespace(items=[], error=None, calls=0)

    async def fake_list_positions():
        box.calls += 1
        if box.error is not None:
            raise box.error
        return [dict(p) for p in box.items]

    monkeypatch.setattr(strategy_base.broker, "list_positions", fake_list_positions)
    monkeypatch.setattr(routes_strategy, "STOP_POSITION_CHECK", True)
    return box


def tmf(direction="Buy", qty=1, price=48981.0, code="TMFJ6"):
    return {"code": code, "direction": direction, "quantity": qty, "price": price}


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


# ── 第二層：看券商實際持倉 ───────────────────────────────────────

def test_a_position_the_strategy_does_not_know_about_also_blocks_the_stop(rig, broker_positions):
    """2026-10-08：策略以為空手（position == 0），券商帳上其實有 1 口多單。"""
    c, arm, calls = rig
    s = arm("scalp", 0)
    broker_positions.items = [tmf("Buy", 1, 48981.0)]
    r = c.post("/api/strategy/scalp/stop")
    assert r.status_code == 409 and calls == [] and s.state.is_running is True
    detail = r.json()["detail"]
    assert "沒有記錄持倉" in detail and "券商帳上有 TMF 多 1 口" in detail and "48981" in detail
    assert "停損停利" in detail and "未成交委託" in detail and "force=true" in detail


def test_broker_short_and_hedged_months_are_reported_as_such(rig, broker_positions):
    c, arm, calls = rig
    arm("scalp", 0)
    broker_positions.items = [tmf("Sell", 2)]
    assert "空 2 口" in c.post("/api/strategy/scalp/stop").json()["detail"]
    broker_positions.items = [tmf("Buy", 1, code="TMFJ6"), tmf("Sell", 1, code="TMFK6")]      # 淨 0、總 2 口
    detail = c.post("/api/strategy/scalp/stop").json()["detail"]
    assert "多空相抵" in detail and "總 2 口" in detail and calls == []


def test_other_contracts_on_the_account_do_not_block(rig, broker_positions):
    c, arm, calls = rig
    arm("scalp", 0)
    broker_positions.items = [tmf(code="MXFJ6"), tmf(code="TXO20000J6")]
    assert c.post("/api/strategy/scalp/stop").status_code == 200 and calls == ["scalp"]


def test_force_stops_with_a_warning_that_names_the_broker_position(rig, broker_positions, caplog):
    c, arm, calls = rig
    arm("scalp", 0)
    broker_positions.items = [tmf("Buy", 1)]
    with caplog.at_level(logging.WARNING, logger=routes_strategy.logger.name):
        r = c.post("/api/strategy/scalp/stop?force=true")
    body = r.json()
    assert r.status_code == 200 and calls == ["scalp"]
    assert "券商帳上有持倉" in body["warning"] and "多 1 口" in body["warning"] and "沒有任何停損停利保護" in body["warning"]
    assert "強制停止" in caplog.text and "broker" in caplog.text


@pytest.mark.parametrize("err", [RuntimeError("Worker 尚未連線"), asyncio.TimeoutError()])
def test_cannot_verify_the_broker_means_do_not_stop_unless_forced(rig, broker_positions, err):
    c, arm, calls = rig
    arm("scalp", 0)
    broker_positions.error = err
    r = c.post("/api/strategy/scalp/stop")
    assert r.status_code == 409 and calls == []
    assert "無法確認券商帳上有沒有 TMF 部位" in r.json()["detail"] and "force=true" in r.json()["detail"]
    r = c.post("/api/strategy/scalp/stop?force=true")                   # 出口：真的要停還是停得掉（策略失控時不能被擋死）
    assert r.status_code == 200 and calls == ["scalp"] and "無法確認" in r.json()["warning"]


def test_a_flat_strategy_on_a_flat_account_stops_with_exactly_one_broker_query(rig, broker_positions):
    c, arm, calls = rig
    arm("scalp", 0)
    assert c.post("/api/strategy/scalp/stop").json() == {"status": "stopped", "name": "scalp"}
    assert broker_positions.calls == 1


def test_when_the_strategy_already_knows_it_holds_a_position_the_broker_is_not_queried(rig, broker_positions):
    c, arm, _ = rig
    arm("scalp", 1)
    assert c.post("/api/strategy/scalp/stop").status_code == 409
    assert broker_positions.calls == 0


def test_the_switch_off_restores_the_strategy_only_check(rig, broker_positions, monkeypatch):
    c, arm, calls = rig
    monkeypatch.setattr(routes_strategy, "STOP_POSITION_CHECK", False)
    arm("scalp", 0)
    broker_positions.items = [tmf()]
    assert c.post("/api/strategy/scalp/stop").status_code == 200 and broker_positions.calls == 0


def test_shutdown_never_queries_the_broker(rig, broker_positions):
    _, arm, calls = rig
    arm("scalp", 0)
    broker_positions.error = RuntimeError("broker down")                # 即使券商查不到，關機也不能被擋
    asyncio.run(strategy_engine.stop_all())
    assert "scalp" in calls and broker_positions.calls == 0
