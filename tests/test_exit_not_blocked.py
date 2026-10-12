"""平倉單不受風控限制（update.md 發現 24）。

背景（2026-10-12）：BaseStrategy.place_order 用「部位方向」判斷是不是平倉單、平倉單才略過風控；但停損停利、ATR 移動停損、
回歸平倉都是先把 state.position 歸零（擋重入）才下單，推算出來是「進場單」，吃到 _risk_ok()：
過了可開倉時段（orb 預設 13:30）、當日虧損上限觸發、進場次數已滿時，停損停利反而送不出去，部位還原後沒人管。
修法：呼叫端明講 reduce_only=True；反手的平倉腿也一樣，進場腿被擋時回到空手（不還原成舊部位，否則會有幽靈部位）。"""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime
from pathlib import Path

import pytest

import strategies.base as base
from core.bar_builder import Bar
from strategies.ma_cross import MACrossStrategy
from strategies.orb import ORBStrategy
from strategies.vwap_revert import VWAPRevertStrategy

ROOT = Path(__file__).resolve().parent.parent
EXIT_KINDS = {"tp", "sl", "trail", "revert_exit", "reverse_close", "exit"}


@pytest.fixture
def rig(monkeypatch):
    placed: list[dict] = []
    box = {"fail_next": 0, "gate": None}

    async def fake_broker_place(**kw):
        if box["gate"] is not None:
            await box["gate"].wait()
        if box["fail_next"] > 0:
            box["fail_next"] -= 1
            raise RuntimeError("broker down")
        placed.append(kw)
        return {"trade_id": "X", "status": "Submitted"}

    async def margin_ok(self):
        return True

    monkeypatch.setattr(base.broker, "place_order", fake_broker_place)
    monkeypatch.setattr(base.BaseStrategy, "_margin_ok", margin_ok)

    def make(cls, hhmm=1000, **attrs):
        s = cls()
        now = datetime(2026, 10, 12, hhmm // 100, hhmm % 100)
        s._now = lambda: now
        s._roll_risk_day()                 # 先換日，之後設的風控狀態才不會被換日歸零
        for k, v in attrs.items():
            setattr(s, k, v)
        return s

    return placed, make, box


def hold(s, pos=1, entry=100.0):
    s.state.position, s.state.entry_price = pos, entry


def run(coro):
    return asyncio.run(coro)


# ── 停損停利 ──────────────────────────────────────────────────────

def test_stop_loss_executes_outside_the_trade_window(rig):
    placed, make, _ = rig
    s = make(ORBStrategy, hhmm=1340, stop_loss_pts=10)          # orb 預設可開倉到 13:30
    hold(s)
    run(s._check_sl_tp(85.0))
    assert s.state.position == 0 and len(placed) == 1 and placed[0]["action"] == "Sell"
    assert s.state.realized_pnl == -150.0 and not s.state.errors


def test_stop_loss_executes_after_the_daily_loss_limit_triggered(rig):
    placed, make, _ = rig
    s = make(MACrossStrategy, stop_loss_pts=10, daily_max_loss=100)
    s.state.realized_pnl = -500.0
    hold(s)
    assert s._risk_ok()[0] is False                              # 風控現況：已不能開新倉
    run(s._check_sl_tp(85.0))
    assert s.state.position == 0 and len(placed) == 1            # 但停損照常送出


def test_take_profit_executes_after_the_max_trades_limit(rig):
    placed, make, _ = rig
    s = make(MACrossStrategy, take_profit_pts=10, max_trades_per_day=2)
    s._trades_today = 2
    hold(s)
    run(s._check_sl_tp(120.0))
    assert s.state.position == 0 and len(placed) == 1 and s._trades_today == 2     # 平倉不算進場次數


def test_exits_do_not_count_as_entries(rig):
    placed, make, _ = rig
    s = make(MACrossStrategy, stop_loss_pts=10)
    run(s._go(1, 100.0))
    assert s._trades_today == 1
    run(s._check_sl_tp(85.0))
    assert s._trades_today == 1                                  # 以前停損也被算成一次「進場」，次數上限會提早到


def test_entries_are_still_blocked_by_risk_limits(rig):
    placed, make, _ = rig
    s = make(MACrossStrategy, max_trades_per_day=1)
    s._trades_today = 1
    run(s._go(1, 100.0))
    assert placed == [] and s.state.position == 0 and "進場已達 1 次上限" in s.state.errors[-1]
    s2 = make(ORBStrategy, hhmm=1340)
    run(s2._go(1, 100.0))
    assert placed == [] and s2.state.position == 0 and "非可開倉時段" in s2.state.errors[-1]
    s3 = make(MACrossStrategy, hhmm=1000, daily_max_loss=100)
    s3.state.realized_pnl = -500.0
    run(s3._go(1, 100.0))
    assert placed == [] and s3.state.position == 0


# ── ATR 移動停損、回歸平倉 ────────────────────────────────────────

def test_atr_trailing_stop_executes_outside_the_trade_window(rig):
    placed, make, _ = rig
    s = make(ORBStrategy, hhmm=1340, atr_trail_mult=2.0)
    hold(s)
    s._atr, s._trail_stop = 5.0, 98.0
    bar = Bar("TMFJ6", int(datetime(2026, 10, 12, 13, 40).timestamp()), 99, 99, 90, 90.0, 10)
    assert run(s._check_atr_trail(bar)) is True
    assert s.state.position == 0 and len(placed) == 1 and placed[0]["action"] == "Sell"


def test_vwap_revert_exit_executes_outside_the_trade_window(rig):
    placed, make, _ = rig
    s = make(VWAPRevertStrategy, hhmm=1340, trade_end_hhmm=1330)
    hold(s, pos=-1)
    s._cum_pv, s._cum_vol, s._bars_today, s._day = 100.0 * 1000, 1000.0, 50, "2026-10-12"
    bar = Bar("TMFJ6", int(datetime(2026, 10, 12, 13, 40).timestamp()), 101, 101, 99, 100.0, 10)
    run(s.on_bar(bar))
    assert s.state.position == 0 and len(placed) == 1 and placed[0]["action"] == "Buy"


# ── 反手 ──────────────────────────────────────────────────────────

def test_a_normal_reversal_closes_then_opens(rig):
    placed, make, _ = rig
    s = make(MACrossStrategy)
    hold(s, pos=1, entry=100.0)
    run(s._go(-1, 95.0))
    assert [p["action"] for p in placed] == ["Sell", "Sell"]
    assert s.state.position == -1 and s.state.entry_price == 95.0
    assert s.state.realized_pnl == -50.0 and s._trades_today == 1


@pytest.mark.parametrize("limit", ["max_trades", "window", "daily_loss"])
def test_a_reversal_whose_entry_is_blocked_ends_flat_not_with_a_phantom_position(rig, limit):
    """舊部位的平倉腿照樣送出；只有新進場被擋 → 空手。以前（兩腿一起被擋）部位會續抱；若只放行平倉腿卻還原成舊部位，
    就會變成券商已經空手、策略卻以為還有部位（之後的「平倉」單會在券商端開出反向部位）。"""
    placed, make, _ = rig
    if limit == "max_trades":
        s = make(MACrossStrategy, max_trades_per_day=1)
        s._trades_today = 1
    elif limit == "window":
        s = make(MACrossStrategy, hhmm=1340, trade_end_hhmm=1330)
    else:
        s = make(MACrossStrategy, daily_max_loss=100)
        s.state.realized_pnl = -500.0
    hold(s, pos=1, entry=100.0)
    before = s.state.realized_pnl
    run(s._go(-1, 95.0))
    assert len(placed) == 1 and placed[0]["action"] == "Sell"                 # 只有平倉腿
    assert s.state.position == 0 and s.state.entry_price == 0.0               # 空手，不是舊部位也不是新部位
    assert s.state.realized_pnl == before - 50.0                              # 舊部位的損益只記一次
    assert "進場下單失敗" in s.state.errors[-1]


def test_a_failed_close_leg_keeps_the_old_position(rig):
    placed, make, box = rig
    s = make(MACrossStrategy)
    hold(s, pos=1, entry=100.0)
    box["fail_next"] = 1                                                       # 第一張（平倉腿）被券商拒絕
    run(s._go(-1, 95.0))
    assert placed == [] and s.state.position == 1 and s.state.entry_price == 100.0
    assert s.state.realized_pnl == 0.0 and "進場下單失敗" in s.state.errors[-1]


def test_a_failed_entry_leg_after_a_successful_close_ends_flat(rig):
    placed, make, box = rig
    s = make(MACrossStrategy)
    hold(s, pos=1, entry=100.0)

    async def scenario():
        orig = base.broker.place_order
        calls = {"n": 0}

        async def second_fails(**kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("broker rejected the new order")
            return await orig(**kw)

        base.broker.place_order = second_fails
        try:
            await s._go(-1, 95.0)
        finally:
            base.broker.place_order = orig

    run(scenario())
    assert len(placed) == 1 and s.state.position == 0 and s.state.realized_pnl == -50.0


def test_a_flat_account_whose_entry_fails_stays_flat(rig):
    placed, make, box = rig
    s = make(MACrossStrategy)
    box["fail_next"] = 1
    run(s._go(1, 100.0))
    assert placed == [] and s.state.position == 0 and s.state.entry_price == 0.0


def test_a_second_signal_during_a_reversal_is_ignored(rig):
    """反手的 await 期間，同方向的第二個訊號進來：部位已經先改成新方向，不會重複下單。"""
    placed, make, box = rig
    s = make(MACrossStrategy)
    hold(s, pos=1, entry=100.0)

    async def scenario():
        box["gate"] = asyncio.Event()
        t1 = asyncio.create_task(s._go(-1, 95.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await s._go(-1, 94.0)                                                  # 重入：什麼都不該發生
        box["gate"].set()
        await t1

    run(scenario())
    assert len(placed) == 2 and s.state.position == -1 and s.state.entry_price == 95.0


# ── 防止以後又寫錯 ────────────────────────────────────────────────

def test_every_closing_order_in_the_strategies_is_marked_reduce_only():
    """新增策略時，平倉的 place_order 一定要帶 reduce_only=True，進場的不可以帶。"""
    offenders, seen = [], 0
    for f in sorted((ROOT / "strategies").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "place_order"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            kind = kw.get("kind")
            if kind is None:
                continue
            seen += 1
            ro = kw.get("reduce_only")
            is_true = isinstance(ro, ast.Constant) and ro.value is True
            if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                if kind.value in EXIT_KINDS and not is_true:
                    offenders.append(f"{f.name}:{node.lineno} kind={kind.value} 沒有 reduce_only=True")
                if kind.value == "entry" and ro is not None:
                    offenders.append(f"{f.name}:{node.lineno} 進場單不可以是 reduce_only")
            elif not is_true:      # kind 是變數（目前只有 _check_sl_tp 的 tp／sl）：一律視為平倉，要帶 reduce_only
                offenders.append(f"{f.name}:{node.lineno} kind 是變數，必須明講 reduce_only=True")
    assert seen >= 6 and not offenders, offenders


def test_scalp_stop_loss_stays_reduce_only_and_never_hits_the_risk_check(rig):
    from strategies.scalp import ScalpStrategy
    placed, make, _ = rig
    s = make(ScalpStrategy, hhmm=1340, trade_end_hhmm=1330, sl_pts=10)
    s.state.position, s.state.entry_price = 1, 100.0
    s._phase, s._direction, s._entry_qty, s._last_entry_price = "holding", 1, 1, 100.0
    run(s._do_sl())
    assert s.state.position == 0 and len(placed) == 1 and placed[0]["action"] == "Sell"
