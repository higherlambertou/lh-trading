"""scalp 的入場狀態機：進場把關（max_qty 是持倉上限）與入場單逾時處理。

背景（2026-10-08 21:19，update.md 發現 21）：shioaji 1.5.3 的成交回報解析壞掉，scalp 認不出自己的成交，只剩「逾時查詢」這條備援，
而備援有兩個缺陷：① 新單送出時舊單的 _entry_trade／_entry_tick_count 還在，await 期間的 tick 立刻對舊單觸發取消、把狀態切到冷卻；
② 逾時處理沒有重入防護，同一毫秒跑了十幾個。結果：成交 1 口後，一分鐘內又送了 15 張單。
以前沒有任何針對狀態機的測試——這裡用假的券商（可以讓下單／查詢／取消卡住，模擬 await 期間有 tick 與回報插進來）逐一重現。"""
from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

import strategies.base as base_mod
import strategies.scalp as scalp_mod
from strategies.base import is_working_status


class FakeBroker:
    def __init__(self):
        self.positions: list[dict] = []
        self.trades: list[dict] = []
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.counters: Counter = Counter()
        self._n = 0
        self.place_gate = None          # asyncio.Event：讓 place_order 卡住，模擬 await 期間
        self.status_gate = None
        self.cancel_gate = None
        self.cancel_error: Exception | None = None
        self.positions_error: Exception | None = None
        self.trades_error: Exception | None = None
        self.on_cancel = None           # cancel_order 被呼叫時的副作用（例如單子其實已經成交）

    async def place_order(self, **kw):
        self.counters["place"] += 1
        if self.place_gate is not None:
            await self.place_gate.wait()
        self._n += 1
        tid = f"T{self._n}"
        self.placed.append({"trade_id": tid, **kw})
        return {"trade_id": tid, "status": "PendingSubmit"}

    async def list_trades_with_status(self):
        self.counters["trades"] += 1
        if self.trades_error is not None:
            raise self.trades_error
        if self.status_gate is not None:
            await self.status_gate.wait()
        return [dict(t) for t in self.trades]

    async def list_positions(self):
        self.counters["positions"] += 1
        if self.positions_error is not None:
            raise self.positions_error
        return [dict(p) for p in self.positions]

    async def cancel_order(self, trade_id):
        self.counters["cancel"] += 1
        self.cancelled.append(trade_id)
        if self.on_cancel is not None:
            self.on_cancel()
        if self.cancel_gate is not None:
            await self.cancel_gate.wait()
        if self.cancel_error is not None:
            raise self.cancel_error
        return True


def pos(direction="Buy", qty=1, price=48981.0, code="TMFJ6"):
    return {"code": code, "direction": direction, "quantity": qty, "price": price}


def order(tid="T9", status="Submitted", qty=1, deal=0, price=0.0, code="TMFJ6"):
    return {"id": tid, "status": status, "quantity": qty, "deal_quantity": deal, "avg_deal_price": price, "code": code}


def quote(price=48980.0, tt=1, code="TMFJ6"):
    return {"code": code, "close": price, "tick_type": tt, "volume": 1, "total_volume": 1}


async def settle(n=30):
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.fixture
def rig(monkeypatch):
    fake = FakeBroker()
    for name in ("place_order", "list_trades_with_status", "list_positions", "cancel_order"):
        monkeypatch.setattr(scalp_mod.broker, name, getattr(fake, name))
    monkeypatch.setattr(scalp_mod, "ENTRY_POSITION_CHECK", True)
    s = scalp_mod.ScalpStrategy()
    s.tp_pts, s.sl_pts, s.cancel_after_ticks, s.cooldown_ticks, s.max_qty = 100, 100, 3, 2, 1
    s.state.last_price = 48980.0
    return s, fake


def pending_entry(s, tid="T9", direction=1, qty=1):
    """把策略放進「已送出入場單、等成交」的狀態。"""
    trade = {"trade_id": tid, "status": "PendingSubmit"}
    s._phase, s._entry_trade, s._direction, s._entry_qty = "pending", trade, direction, qty
    s._pending_entry_price = 48980.0
    s._entry_tick_count = 0
    return trade


# ── 進場把關：max_qty 是持倉上限 ─────────────────────────────────

def test_flat_account_enters_with_max_qty_lots(rig):
    s, fake = rig
    s.max_qty = 3
    asyncio.run(s._do_enter(48980.0, 1))
    assert [(o["action"], o["quantity"]) for o in fake.placed] == [("Buy", 3)]
    assert s._phase == "pending" and s._entry_trade["trade_id"] == "T1"
    assert fake.counters["positions"] == 1 and fake.counters["trades"] == 1


def test_a_position_the_strategy_does_not_know_about_blocks_entry(rig):
    """2026-10-08 的情況：券商有 1 口，策略以為自己空手（state.position == 0）。"""
    s, fake = rig
    fake.positions = [pos()]
    assert s.state.position == 0
    asyncio.run(s._do_enter(48980.0, 1))
    assert fake.placed == [] and s._phase == "idle"
    assert "券商已有 TMF 部位 +1 口" in s.state.errors[-1] and "最大口數 1" in s.state.errors[-1]


def test_short_position_and_opposite_signal_also_blocked(rig):
    s, fake = rig
    fake.positions = [pos("Sell", 1)]
    asyncio.run(s._do_enter(48980.0, 1))
    assert fake.placed == [] and "-1 口" in s.state.errors[-1]


def test_max_qty_is_a_cap_not_just_an_order_size(rig):
    s, fake = rig
    s.max_qty = 3
    fake.positions = [pos(qty=1)]
    asyncio.run(s._do_enter(48980.0, 1))
    assert fake.placed == []                                  # 已有 1 口，再進 3 口會超過上限 3


def test_exposure_is_gross_so_hedged_months_still_count(rig):
    s, fake = rig
    fake.positions = [pos("Buy", 1, code="TMFJ6"), pos("Sell", 1, code="TMFK6")]     # 淨 0、總 2 口
    asyncio.run(s._do_enter(48980.0, 1))
    assert fake.placed == []


def test_other_contracts_positions_are_ignored(rig):
    s, fake = rig
    fake.positions = [pos(code="MXFJ6"), pos(code="TXFJ6")]
    asyncio.run(s._do_enter(48980.0, 1))
    assert len(fake.placed) == 1


def test_finished_orders_do_not_block_but_working_ones_do(rig):
    """那 15 張被拒的單（Failed）不能擋住進場；還在場上的（含部分成交）要擋。"""
    s, fake = rig
    fake.trades = ([order(f"F{i}", "Failed") for i in range(15)]
                   + [order("a", "Filled", deal=1), order("b", "Cancelled"), order("c", "Inactive")])
    asyncio.run(s._do_enter(48980.0, 1))
    assert len(fake.placed) == 1
    for status in ("Submitted", "PendingSubmit", "PreSubmitted", "PartFilled", ""):
        s2 = scalp_mod.ScalpStrategy()
        s2.max_qty = 1
        fake.placed.clear()
        fake.trades = [order("w", status, qty=2, deal=1)]
        asyncio.run(s2._do_enter(48980.0, 1))
        assert fake.placed == [], status
        assert "未成交委託 1 筆" in s2.state.errors[-1], status


def test_working_orders_of_other_contracts_do_not_block(rig):
    s, fake = rig
    fake.trades = [order("o1", "Submitted", code="TXO20000J6"), order("o2", "Submitted", code="MXFJ6")]
    asyncio.run(s._do_enter(48980.0, 1))
    assert len(fake.placed) == 1
    s2 = scalp_mod.ScalpStrategy()
    fake.placed.clear()
    fake.trades = [order("o3", "Submitted", code="")]                                  # 看不出合約 → 保守地擋
    asyncio.run(s2._do_enter(48980.0, 1))
    assert fake.placed == []


@pytest.mark.parametrize("which", ["positions_error", "trades_error"])
@pytest.mark.parametrize("err", [RuntimeError("boom"), asyncio.TimeoutError()])
def test_unable_to_verify_means_no_entry(rig, which, err):
    s, fake = rig
    setattr(fake, which, err)
    asyncio.run(s._do_enter(48980.0, 1))
    assert fake.placed == [] and s._phase == "idle"
    assert "無法確認券商部位與委託" in s.state.errors[-1]


def test_switch_off_restores_the_old_behaviour(rig, monkeypatch):
    s, fake = rig
    monkeypatch.setattr(scalp_mod, "ENTRY_POSITION_CHECK", False)
    fake.positions = [pos()]
    asyncio.run(s._do_enter(48980.0, 1))
    assert len(fake.placed) == 1 and fake.counters["positions"] == 0 and fake.counters["trades"] == 0


def test_a_blocked_entry_goes_quiet_instead_of_querying_the_broker_on_every_signal(rig, monkeypatch):
    s, fake = rig
    clock = {"t": 1000.0}
    monkeypatch.setattr(scalp_mod, "time", SimpleNamespace(monotonic=lambda: clock["t"]))   # 只換掉 scalp 看到的 time，不動 asyncio 的時鐘
    fake.positions = [pos()]

    async def signals(n):
        for _ in range(n):
            await s._do_enter(48980.0, 1)

    asyncio.run(signals(100))                                   # 同一個瞬間來了 100 次訊號
    assert fake.counters["positions"] == 1 and len(s.state.errors) == 1
    clock["t"] += 2.0
    asyncio.run(signals(10))
    assert fake.counters["positions"] == 1                      # 3 秒靜默期內不再查
    clock["t"] += 1.5                                           # 3.5 秒後
    asyncio.run(signals(1))
    assert fake.counters["positions"] == 2 and len(s.state.errors) == 1       # 再查一次，但同一個理由 30 秒內只記一次
    clock["t"] += 40
    asyncio.run(signals(1))
    assert len(s.state.errors) == 2                             # 超過 30 秒才再記一次
    fake.positions = []                                          # 部位清掉之後恢復進場
    clock["t"] += 4
    asyncio.run(signals(1))
    assert len(fake.placed) == 1


def test_a_failed_placement_backs_off_instead_of_retrying_on_every_tick(rig, monkeypatch):
    """下單丟例外（逾時、斷線…）時，下一個 tick 又會觸發訊號：不能每個 tick 都重試（連帶每次 2 次券商查詢，會撞到帳務查詢上限）。"""
    s, fake = rig
    clock = {"t": 1000.0}
    monkeypatch.setattr(scalp_mod, "time", SimpleNamespace(monotonic=lambda: clock["t"]))

    async def boom(**kw):
        fake.counters["place"] += 1
        raise RuntimeError("下單逾時")

    monkeypatch.setattr(scalp_mod.broker, "place_order", boom)

    async def signals(n):
        for _ in range(n):
            await s._do_enter(48980.0, 1)

    asyncio.run(signals(50))
    assert fake.counters["place"] == 1 and fake.counters["positions"] == 1 and fake.counters["trades"] == 1
    assert s._phase == "idle" and "掛單失敗" in s.state.errors[-1]
    clock["t"] += 3.5
    asyncio.run(signals(1))
    assert fake.counters["place"] == 2                                    # 過了靜默期才再試一次


def test_a_hundred_signals_with_an_unknown_position_never_place_an_order(rig):
    """事件重現：券商有部位、策略不知道、訊號每個 tick 都成立。以前會一直送單。"""
    s, fake = rig
    fake.positions = [pos()]
    s.momentum_window = 5

    async def storm():
        for _ in range(100):
            await s.on_quote(quote(tt=1))

    asyncio.run(storm())
    assert fake.placed == [] and fake.counters["place"] == 0


# ── 入場單逾時：兩個缺陷 ─────────────────────────────────────────

def test_stale_entry_state_must_not_trigger_a_cancel_while_the_new_order_is_being_placed(rig):
    """缺陷①：新單的 await 期間，殘留的舊 _entry_trade／_entry_tick_count 讓逾時檢查對「舊單」動手，把狀態切到冷卻。"""
    s, fake = rig
    s._entry_trade, s._entry_tick_count, s._phase = {"trade_id": "OLD"}, 99, "idle"
    fake.trades = [order("OLD", "Cancelled")]

    async def scenario():
        fake.place_gate = asyncio.Event()
        task = asyncio.create_task(s._do_enter(48980.0, 1))
        await settle()
        assert fake.counters["place"] == 1 and s._phase == "pending"      # 下單卡住中
        for _ in range(10):
            await s.on_quote(quote())                                      # 這段時間 tick 照常進來
        assert fake.cancelled == [] and s._phase == "pending"            # 不能對舊單取消、也不能切到冷卻
        fake.place_gate.set()
        await task

    asyncio.run(scenario())
    assert s._entry_trade["trade_id"] == "T1" and s._phase == "pending"
    assert fake.counters["trades"] == 1                                    # 只有進場把關查過一次


def test_timeout_handling_is_single_flight(rig):
    """缺陷②：逾時之後每個 tick 都會進來，同一張單不能被同時查詢、取消十幾次。"""
    s, fake = rig
    pending_entry(s)
    s._entry_tick_count = s.cancel_after_ticks
    fake.trades = [order("T9", "Submitted")]

    async def scenario():
        fake.status_gate = asyncio.Event()
        tasks = [asyncio.create_task(s.on_quote(quote())) for _ in range(12)]
        await settle()
        assert fake.counters["trades"] == 1                                 # 只有一個在查
        fake.status_gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert fake.cancelled == ["T9"] and s._phase == "cooldown"


@pytest.mark.parametrize("cancel_fails", [True, False], ids=["cancel-fails", "cancel-succeeds"])
def test_a_fill_that_arrives_while_the_cancel_is_in_flight_is_not_overwritten(rig, cancel_fails):
    """取消請求還在路上時成交回報到了：不論取消之後報錯或成功，都不能把 holding 蓋回冷卻。"""
    s, fake = rig
    pending_entry(s)
    s._entry_tick_count = s.cancel_after_ticks
    fake.trades = [order("T9", "Submitted")]

    async def scenario():
        fake.cancel_gate = asyncio.Event()
        task = asyncio.create_task(s._cancel_entry())
        await settle()
        assert fake.cancelled == ["T9"]
        await s.on_order_event({"state": "FuturesDeal", "trade_id": "T9", "price": 48981.0, "quantity": 1})
        assert s._phase == "holding"
        if cancel_fails:
            fake.cancel_error = RuntimeError("StatusCode: 400, Detail: 無原委託內容")
        fake.cancel_gate.set()
        await task

    asyncio.run(scenario())
    assert s._phase == "holding" and s.state.position == 1
    assert [(o["action"], o["price"]) for o in fake.placed] == [("Sell", 49081)]      # 成交時掛的停利單


def test_cancel_failure_means_check_again_not_assume_cancelled(rig):
    """取消失敗（單子其實已成交）→ 再查一次，查到成交就接管並掛停利。以前直接當成已取消、留下沒人管的部位。"""
    s, fake = rig
    pending_entry(s)
    s._entry_tick_count = s.cancel_after_ticks
    fake.trades = [order("T9", "Submitted")]
    fake.cancel_error = RuntimeError("StatusCode: 400, Detail: 無原委託內容")
    fake.on_cancel = lambda: setattr(fake, "trades", [order("T9", "Filled", deal=1, price=48981.0)])
    asyncio.run(s._cancel_entry())
    assert s._phase == "holding" and s.state.position == 1 and s.state.entry_price == 48981.0
    assert [(o["action"], o["price"], o["quantity"]) for o in fake.placed] == [("Sell", 49081, 1)]


def test_timeout_finds_the_fill_directly(rig):
    s, fake = rig
    pending_entry(s, direction=-1)
    s._entry_tick_count = s.cancel_after_ticks
    fake.trades = [order("T9", "Filled", deal=1, price=48990.0)]
    asyncio.run(s._cancel_entry())
    assert fake.cancelled == []                                              # 已成交就不用取消
    assert s._phase == "holding" and s.state.position == -1 and s.state.entry_price == 48990.0
    assert [(o["action"], o["price"]) for o in fake.placed] == [("Buy", 48890)]


def test_normal_timeout_cancels_and_cools_down(rig):
    s, fake = rig
    trade = pending_entry(s)
    fake.trades = [order("T9", "Submitted")]
    asyncio.run(s._cancel_entry())
    assert fake.cancelled == ["T9"] and s._phase == "cooldown" and s._entry_trade is trade


def test_rejected_order_whose_cancel_fails_just_cools_down(rig):
    """被拒的單（Failed）取消會得到「無原委託內容」——沒成交，照原流程進冷卻。"""
    s, fake = rig
    pending_entry(s)
    fake.trades = [order("T9", "Failed")]
    fake.cancel_error = RuntimeError("無原委託內容")
    asyncio.run(s._cancel_entry())
    assert s._phase == "cooldown" and fake.placed == [] and s.state.position == 0


def test_partial_fill_then_cancel_adopts_the_filled_lots_only(rig):
    s, fake = rig
    s.max_qty = 3
    pending_entry(s, qty=3)
    fake.trades = [order("T9", "Cancelled", qty=3, deal=2, price=48985.0)]
    asyncio.run(s._cancel_entry())
    assert s._phase == "holding" and s.state.position == 2 and s._entry_qty == 2          # 只接管實際成交的 2 口
    assert [(o["action"], o["quantity"]) for o in fake.placed] == [("Sell", 2)]            # 停利單口數 = 成交口數


def test_still_working_partial_fill_is_cancelled_not_adopted_as_full(rig):
    s, fake = rig
    s.max_qty = 3
    pending_entry(s, qty=3)
    fake.trades = [order("T9", "PartFilled", qty=3, deal=1, price=48985.0)]
    asyncio.run(s._cancel_entry())
    assert fake.cancelled == ["T9"] and s._phase == "cooldown" and s.state.position == 0


def test_a_failure_report_during_the_lookup_is_not_overwritten(rig):
    """查詢期間「入場單被拒」的回報已經把狀態切到冷卻：逾時處理不能再蓋一次（也不能對已清掉的單動手）。"""
    s, fake = rig
    pending_entry(s)
    s._entry_tick_count = s.cancel_after_ticks
    fake.trades = [order("T9", "Submitted")]

    async def scenario():
        fake.status_gate = asyncio.Event()
        task = asyncio.create_task(s._cancel_entry())
        await settle()
        await s.on_order_event({"state": "FuturesOrder", "trade_id": "T9", "op_type": "New", "op_code": "88", "op_msg": "保證金不足"})
        assert s._phase == "cooldown" and s._entry_trade is None
        before = s._cooldown_count
        fake.status_gate.set()
        await task
        assert s._cooldown_count == before and fake.cancelled == []

    asyncio.run(scenario())


def test_a_backoff_cooldown_set_by_a_failure_report_survives_a_successful_cancel(rig):
    s, fake = rig
    pending_entry(s)
    s._consec_failures = 2                                                     # 已連敗 2 次：這次失敗後冷卻加倍
    fake.trades = [order("T9", "Submitted")]

    async def scenario():
        fake.cancel_gate = asyncio.Event()
        task = asyncio.create_task(s._cancel_entry())
        await settle()
        await s.on_order_event({"state": "FuturesOrder", "trade_id": "T9", "op_type": "New", "op_code": "88", "op_msg": "保證金不足"})
        backoff_count = s._cooldown_count
        assert backoff_count < 0                                               # 退避：要等比一般冷卻更久
        fake.cancel_gate.set()
        await task
        assert s._cooldown_count == backoff_count and s._phase == "cooldown"

    asyncio.run(scenario())


# ── 端到端：成交之後不會再送單 ────────────────────────────────────

def test_with_working_callbacks_the_fill_is_recognised_and_no_more_entries_are_sent(rig):
    s, fake = rig
    s.momentum_window = 5

    async def scenario():
        await s._do_enter(48980.0, 1)
        tid = s._entry_trade["trade_id"]
        await s.on_order_event({"state": "FuturesOrder", "trade_id": tid, "op_type": "New", "op_code": "00", "op_msg": ""})
        fake.positions = [pos()]
        await s.on_order_event({"state": "FuturesDeal", "trade_id": tid, "price": 48981.0, "quantity": 1})
        for _ in range(200):                                                  # 買方力道一直成立的 200 個 tick
            await s.on_quote(quote(48990.0, tt=1))

    asyncio.run(scenario())
    assert s._phase == "holding" and s.state.position == 1
    assert [(o["action"], o["price"]) for o in fake.placed] == [("Buy", 48980), ("Sell", 49081)]   # 1 張入場 + 1 張停利，沒有第 3 張


def test_with_broken_callbacks_the_timeout_path_still_ends_with_one_entry_and_one_tp(rig):
    """2026-10-08 的重現：回報全部漏接，成交只能靠逾時查詢發現。以前這裡會一直重進。"""
    s, fake = rig
    s.momentum_window = 5

    async def scenario():
        await s._do_enter(48980.0, 1)
        fake.trades = [order("T1", "Filled", deal=1, price=48981.0)]       # 券商端已成交，但一個回報都沒進來
        fake.positions = [pos()]
        for _ in range(200):
            await s.on_quote(quote(48990.0, tt=1))

    asyncio.run(scenario())
    assert s._phase == "holding" and s.state.position == 1
    assert [(o["action"], o["price"]) for o in fake.placed] == [("Buy", 48980), ("Sell", 49081)]


# ── 共用：委託狀態與部位查詢 ──────────────────────────────────────

@pytest.mark.parametrize("status,working", [
    ("Submitted", True), ("PendingSubmit", True), ("PreSubmitted", True), ("PartFilled", True), ("", True), (None, True),
    ("Filled", False), ("Cancelled", False), ("Failed", False), ("Inactive", False)])
def test_is_working_status(status, working):
    assert is_working_status(status) is working


def test_start_up_cleanup_only_cancels_orders_that_are_still_on_the_market(rig):
    s, fake = rig
    fake.trades = [order(f"id-{st}", st) for st in
                   ("Submitted", "PendingSubmit", "PreSubmitted", "PartFilled", "Filled", "Cancelled", "Failed", "Inactive")]
    asyncio.run(s._cancel_all_pending())
    assert sorted(fake.cancelled) == sorted(f"id-{st}" for st in ("Submitted", "PendingSubmit", "PreSubmitted", "PartFilled"))


def test_start_up_sync_adopts_the_broker_position(rig):
    s, fake = rig
    fake.positions = [pos("Buy", 2, 49000.0), pos(code="MXFJ6", qty=5)]
    asyncio.run(s._sync_position_from_broker())
    assert s.state.position == 2 and s.state.entry_price == 49000.0
    assert s._phase == "holding" and s._entry_qty == 2 and s._need_tp_resubmit


def test_start_up_sync_with_no_position_and_with_a_query_failure(rig):
    s, fake = rig
    asyncio.run(s._sync_position_from_broker())
    assert s.state.position == 0 and s._phase == "idle"
    s.state.position = 5
    fake.positions_error = RuntimeError("boom")
    asyncio.run(s._sync_position_from_broker())
    assert s.state.position == 5                                              # 查詢失敗：維持原狀、不丟例外


def test_start_up_resets_the_gate_quiet_period(rig):
    s, _ = rig
    s._gate_hold_until, s._cancelling = 10 ** 12, True
    s._on_position_synced(0, 0.0)
    assert s._gate_hold_until == 0.0 and s._cancelling is False


def test_the_param_label_says_it_is_a_position_cap():
    labels = {p["key"]: p["label"] for p in scalp_mod.ScalpStrategy().param_schema}
    assert "持倉上限" in labels["max_qty"]
    assert base_mod.is_working_status is is_working_status
