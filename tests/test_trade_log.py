"""成交紀錄（core/trade_log.py）與各下單呼叫點 hook 的測試。
執行：python -m pytest tests -q"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

import core.broker as broker_mod
from core.bar_builder import Bar
from core.manual_monitor import ManualWatch, manual_monitor
from core.trade_log import TradeLog
from strategies.bar_base import BarStrategy
from strategies.base import BaseStrategy
from strategies.scalp import ScalpStrategy


def drain(t: TradeLog) -> None:
    """等 writer thread 把佇列寫完（stop 會 flush，再 start 讓後續還能繼續記）。"""
    t.stop()
    t.start()


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    t = TradeLog(tmp_path / "t.db")
    t.start()
    yield t
    t.stop()


def order(t: TradeLog, trade_id="", action="Buy", qty=1, order_type="IOC", **kw):
    t.record_order(contract="TMF", action=action, qty=qty, price_type="MKT", order_type=order_type,
                   trade_id=trade_id, status="PendingSubmit", **kw)


def deal(t: TradeLog, trade_id: str, price: float, qty: int = 1):
    t.record_event({"state": "FuturesDeal", "trade_id": trade_id, "price": price, "quantity": qty})


# ── 記錄與讀取 ────────────────────────────────────────────────────

def test_fills_join_orders_and_slippage_is_positive_when_worse(tlog):
    with tlog.context(strategy="scalp", reason="entry", signal_price=100.0):
        order(tlog, "B_WORSE", "Buy")
        order(tlog, "B_BETTER", "Buy")
    with tlog.context(strategy="scalp", reason="sl", signal_price=100.0, ref_price=100.0):
        order(tlog, "S_WORSE", "Sell")
    deal(tlog, "B_WORSE", 102.0)      # 買貴了 2 點 → +2
    deal(tlog, "B_BETTER", 99.0)      # 買便宜 1 點 → -1
    deal(tlog, "S_WORSE", 98.0)       # 賣低了 2 點 → +2（賣出時成交價低於基準才是對我方不利）
    drain(tlog)
    rows = {r["trade_id"]: r for r in tlog.orders()}
    assert rows["B_WORSE"]["slip_signal"] == 2.0 and rows["B_BETTER"]["slip_signal"] == -1.0
    assert rows["S_WORSE"]["slip_signal"] == 2.0 and rows["S_WORSE"]["slip_ref"] == 2.0
    assert rows["B_WORSE"]["avg_fill"] == 102.0 and rows["B_WORSE"]["outcome"] == "filled"
    assert rows["S_WORSE"]["strategy"] == "scalp" and rows["S_WORSE"]["reason"] == "sl"


def test_outcomes(tlog):
    order(tlog, "PART", qty=2)
    deal(tlog, "PART", 100.0, 1)
    order(tlog, "REJ")
    tlog.record_event({"state": "FuturesOrder", "trade_id": "REJ", "op_type": "New", "op_code": "99", "op_msg": "price"})
    order(tlog, "CXL", order_type="ROD")
    tlog.record_event({"state": "FuturesOrder", "trade_id": "CXL", "op_type": "Cancel", "op_code": "00"})
    tlog.record_order(contract="TMF", action="Buy", qty=1, status="error", error="RuntimeError('boom')")
    tlog.record_order(contract="TMF", action="Buy", qty=1, order_type="IOC", trade_id="OLD", status="PendingSubmit",
                      ts=time.time() - 100)                      # IOC 超過 30 秒仍無任何回報
    order(tlog, "RESTING", order_type="ROD")                     # 掛單中
    drain(tlog)
    out = {r["trade_id"] or r["status"]: r["outcome"] for r in tlog.orders()}
    assert out == {"PART": "partial", "REJ": "rejected", "CXL": "cancelled", "error": "error",
                   "OLD": "unfilled", "RESTING": "open"}


def test_context_propagates_across_await_and_nests(tlog):
    async def go():
        with tlog.context(strategy="a", reason="entry", signal_price=1.0):
            await asyncio.sleep(0)
            with tlog.context(reason="tp", ref_price=9.0):       # 內層覆蓋 reason、保留 strategy
                await asyncio.sleep(0)
                order(tlog, "T1")
            order(tlog, "T2")                                    # 回到外層
        order(tlog, "T3")                                        # 沒有脈絡也要記
    asyncio.run(go())
    drain(tlog)
    r = {x["trade_id"]: x for x in tlog.orders()}
    assert (r["T1"]["strategy"], r["T1"]["reason"], r["T1"]["ref_price"]) == ("a", "tp", 9.0)
    assert (r["T2"]["strategy"], r["T2"]["reason"], r["T2"]["ref_price"]) == ("a", "entry", None)
    assert (r["T3"]["strategy"], r["T3"]["reason"], r["T3"]["signal_price"]) == ("", "", None)


def test_market_tag_is_stamped_at_order_time(tlog):
    order(tlog, "BEFORE")
    tlog.set_market_tag({"market_state": "REVERT", "hurst_state": "REVERT", "iv_state": "UNKNOWN",
                         "direction": 1, "as_of": "2026-10-07"})
    order(tlog, "AFTER")
    tlog.set_market_tag({"market_state": "TREND"})               # 之後改判斷，不影響已記的
    drain(tlog)
    r = {x["trade_id"]: x for x in tlog.orders()}
    assert r["BEFORE"]["market_state"] is None
    assert (r["AFTER"]["market_state"], r["AFTER"]["direction"], r["AFTER"]["tag_date"]) == ("REVERT", 1, "2026-10-07")


def test_mode_isolation_for_orders_and_fill_join(tlog, monkeypatch):
    monkeypatch.setenv("SIMULATION", "false")
    order(tlog, "SAME_ID")                                       # live 的單
    monkeypatch.setenv("SIMULATION", "true")
    deal(tlog, "SAME_ID", 123.0)                                 # sim 的成交，id 碰巧相同
    drain(tlog)
    live = tlog.orders(mode="live")
    assert len(live) == 1 and live[0]["fill_qty"] == 0           # 不可跨 mode 配對
    assert tlog.orders(mode="sim") == []


def test_summary_groups_and_percentiles(tlog):
    for i, fill in enumerate([99, 98, 97, 96, 90]):              # 賣出停損，訊號價=設定價=100
        with tlog.context(strategy="scalp", reason="sl", signal_price=100.0, ref_price=100.0):
            order(tlog, f"S{i}", "Sell")
        deal(tlog, f"S{i}", float(fill))
    order(tlog, "NOCTX")
    drain(tlog)
    s = tlog.summary("sim")
    g = next(x for x in s["groups"] if (x["strategy"], x["reason"]) == ("scalp", "sl"))
    assert g["n"] == 5 and g["outcomes"] == {"filled": 5}
    assert g["slip_signal"] == {"n": 5, "mean": 4.0, "median": 3.0, "p95": 10.0, "max": 10.0}
    assert g["slip_ref"]["max"] == 10.0
    assert any(x["strategy"] == "" for x in s["groups"])         # 沒脈絡的單照樣出現在彙總裡


# ── 永不影響下單 ──────────────────────────────────────────────────

def test_record_never_raises_and_never_blocks(tmp_path, monkeypatch):
    t = TradeLog(tmp_path / "x.db")
    t.record_order(contract=None, action=None, qty="not-a-number")          # 壞資料：吞掉
    t.record_event({"state": object(), "price": "abc", "quantity": "x"})
    import core.trade_log as tl
    monkeypatch.setattr(tl, "QUEUE_MAX", 3)
    small = TradeLog(tmp_path / "y.db")                                     # 沒啟動 writer、佇列很小
    t0 = time.perf_counter()
    for i in range(50):
        order(small, str(i))
    assert time.perf_counter() - t0 < 0.5 and small._dropped == 47         # 滿了就丟、不阻塞


def test_disabled_by_env_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_LOG", "false")
    t = TradeLog(tmp_path / "off.db")
    t.start()
    order(t, "A")
    deal(t, "A", 1.0)
    t.stop()
    assert not (tmp_path / "off.db").exists() and t.orders() == []


def test_flush_failure_is_reported_not_raised(tmp_path):
    conn = sqlite3.connect(tmp_path / "z.db")
    conn.close()                                                            # 已關閉的連線
    assert TradeLog._flush(conn, [("order", ("x",) * 22)]) is False


# ── broker 掛鉤：回傳值與例外行為完全不變 ─────────────────────────

@pytest.fixture
def wired(tlog, monkeypatch):
    """把 broker 的 _acall 換成假的、trade_log 換成測試實例。"""
    calls: list[tuple[str, dict]] = []

    async def fake_acall(method, timeout=8.0, **kw):
        calls.append((method, kw))
        return {"trade_id": f"T{len(calls)}", "status": "PendingSubmit", "code": "TXO49800J6"}

    monkeypatch.setattr(broker_mod, "trade_log", tlog)
    monkeypatch.setattr(broker_mod.broker, "_acall", fake_acall)
    return calls


def test_broker_place_order_records_and_returns_unchanged(wired, tlog):
    res = asyncio.run(broker_mod.broker.place_order("tmf", "Buy", 2, price_type="MKT", order_type="IOC"))
    assert res == {"trade_id": "T1", "status": "PendingSubmit", "code": "TXO49800J6"}
    assert wired[0][1]["contract_code"] == "TMF" and wired[0][1]["quantity"] == 2
    drain(tlog)
    r = tlog.orders()[0]
    assert (r["trade_id"], r["contract"], r["qty"], r["order_type"], r["status"]) == ("T1", "TMF", 2, "IOC", "PendingSubmit")
    assert r["latency_ms"] >= 0


@pytest.mark.parametrize("exc,status", [(RuntimeError("boom"), "error"), (asyncio.TimeoutError(), "timeout")])
def test_broker_failures_are_recorded_and_reraised(tlog, monkeypatch, exc, status):
    async def failing(method, timeout=8.0, **kw):
        raise exc

    monkeypatch.setattr(broker_mod, "trade_log", tlog)
    monkeypatch.setattr(broker_mod.broker, "_acall", failing)
    with pytest.raises(type(exc)):
        asyncio.run(broker_mod.broker.place_order("TMF", "Sell", 1))
    drain(tlog)
    r = tlog.orders()[0]
    assert r["status"] == status and r["trade_id"] == "" and r["outcome"] == status


def test_broker_option_order_and_order_events_are_recorded(wired, tlog):
    asyncio.run(broker_mod.broker.place_option_order("202610", 49800, "C", "TXO", "Buy", 1, 800.0, "IOC"))
    broker_mod.broker._dispatch({"type": "order_event", "state": "FuturesDeal",
                                 "trade_id": "T1", "price": 801.0, "quantity": 1})
    drain(tlog)
    r = tlog.orders()[0]
    assert r["contract"] == "TXO49800J6" and r["limit_price"] == 800.0 and r["order_type"] == "IOC"
    assert r["fill_qty"] == 1 and r["avg_fill"] == 801.0


# ── 各下單呼叫點的脈絡 ────────────────────────────────────────────

class Dummy(BaseStrategy):
    name = "dummy"

    async def on_quote(self, quote):
        pass


def rows_by_reason(tlog):
    drain(tlog)
    return {r["reason"]: r for r in tlog.orders()}


def test_stop_loss_and_take_profit_carry_reason_and_reference_price(wired, tlog):
    s = Dummy()
    s.take_profit_pts, s.stop_loss_pts = 20, 10
    s.state.position, s.state.entry_price, s.state.last_price = 1, 100.0, 121.0
    assert asyncio.run(s._check_sl_tp(121.0)) is True                         # 多單停利
    s.state.position, s.state.entry_price, s.state.last_price = -1, 100.0, 111.0
    assert asyncio.run(s._check_sl_tp(111.0)) is True                         # 空單停損
    r = rows_by_reason(tlog)
    assert (r["tp"]["action"], r["tp"]["ref_price"], r["tp"]["signal_price"], r["tp"]["strategy"]) == ("Sell", 120.0, 121.0, "dummy")
    assert (r["sl"]["action"], r["sl"]["ref_price"], r["sl"]["signal_price"]) == ("Buy", 110.0, 111.0)


def test_entry_and_reverse_legs_are_labelled(wired, tlog):
    s = Dummy()
    s.state.position, s.state.entry_price, s.state.last_price = 1, 100.0, 99.0
    asyncio.run(s._go(-1, 99.0))                                              # 多單反手做空：先平倉再進場
    drain(tlog)
    got = sorted((r["reason"], r["action"]) for r in tlog.orders())
    assert got == [("entry", "Sell"), ("reverse_close", "Sell")]


def test_atr_trail_exit_carries_stop_level(wired, tlog):
    class B(BarStrategy):
        name = "b"

        async def on_bar(self, bar):
            pass

    b = B()
    b.state.position, b.state.entry_price, b._trail_stop = 1, 100.0, 95.0
    asyncio.run(b._trail_exit(Bar("TMFJ6", 0, 100, 100, 94, 94, 1)))
    r = rows_by_reason(tlog)["trail"]
    assert (r["strategy"], r["action"], r["ref_price"]) == ("b", "Sell", 95.0)


def test_scalp_entry_tp_and_sl_orders(wired, tlog):
    s = ScalpStrategy()
    asyncio.run(s._lmt("Buy", 100.4, qty=1, kind="entry", signal_price=100.0))
    s._phase, s._direction, s._entry_qty, s._last_entry_price, s.sl_pts = "holding", 1, 1, 100.0, 60
    asyncio.run(s._do_sl())
    r = rows_by_reason(tlog)
    assert (r["entry"]["price_type"], r["entry"]["order_type"], r["entry"]["limit_price"], r["entry"]["signal_price"]) == ("LMT", "ROD", 100.0, 100.0)
    assert (r["sl"]["strategy"], r["sl"]["action"], r["sl"]["ref_price"]) == ("scalp", "Sell", 40.0)


def test_manual_monitor_exit_carries_reason(wired, tlog):
    w = ManualWatch(id="w1", contract="TMF", direction=1, quantity=1, entry_price=100.0,
                    stop_loss_pts=10, take_profit_pts=0)
    manual_monitor._watches["w1"] = w
    asyncio.run(manual_monitor._close(w, "sl", 90.0, 89.0))
    r = rows_by_reason(tlog)["sl"]
    assert (r["strategy"], r["action"], r["ref_price"], r["signal_price"], r["order_type"]) == ("manual_monitor", "Sell", 90.0, 89.0, "IOC")
    assert "w1" not in manual_monitor._watches                                # 既有行為不變


def test_manual_order_route_is_labelled_manual(wired, tlog):
    from fastapi.testclient import TestClient
    import main
    res = TestClient(main.app).post("/api/order/place", json={"action": "Buy", "quantity": 1, "contract": "TMF"})
    assert res.status_code == 200 and res.json()["trade_id"] == "T1"
    r = rows_by_reason(tlog)["manual"]
    assert (r["strategy"], r["action"], r["contract"]) == ("manual", "Buy", "TMF")


def test_a_broken_trade_log_never_breaks_order_placement(monkeypatch):
    """成交紀錄壞掉（連 record_order 都丟例外）：下單回應照常回傳，既有行為不變。"""
    class Broken:
        def record_order(self, **kw):
            raise RuntimeError("log broke")

        def record_event(self, ev):
            raise RuntimeError("log broke")

    async def fake_acall(method, timeout=8.0, **kw):
        return {"trade_id": "T1", "status": "PendingSubmit"}

    monkeypatch.setattr(broker_mod, "trade_log", Broken())
    monkeypatch.setattr(broker_mod.broker, "_acall", fake_acall)
    res = asyncio.run(broker_mod.broker.place_order("TMF", "Buy", 1))
    assert res == {"trade_id": "T1", "status": "PendingSubmit"}

    # 成交回報也一樣：紀錄壞掉，策略仍要收到回報
    got = []

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(broker_mod.broker, "_loop", loop)
        monkeypatch.setattr(broker_mod.broker, "_order_callback", lambda m: got.append(m["trade_id"]))
        broker_mod.broker._dispatch({"type": "order_event", "state": "FuturesDeal", "trade_id": "T1",
                                     "price": 1.0, "quantity": 1})
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert got == ["T1"]
