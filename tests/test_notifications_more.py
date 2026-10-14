"""新增的通知（update.md 發現 34）：停損停利事件、保證金風險、手動單成交、當沖倉位收盤前提醒。全部只通知、不動委託；測試用假的 notify，不碰網路。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

import core.daytrade as dt
import core.fill_notify as fn
import core.manual_monitor as mmod
import core.margin_watch as mw
import core.reminders as rem
from core.broker import broker
from core.manual_monitor import ManualOrderMonitor
from core.trade_log import TradeLog

TPE = timezone(timedelta(hours=8))


class Rec:
    """假的 notify：記錄每一次呼叫。"""

    def __init__(self):
        self.calls = []

    def __call__(self, title, message, *, key=None, cooldown=0.0, priority="high", wait=False):
        self.calls.append({"title": title, "message": message, "key": key, "cooldown": cooldown, "priority": priority})
        return True


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    for mod in (mmod, mw, fn, rem):
        monkeypatch.setattr(mod, "notify", r)
    return r


# ══ 停損停利事件 ═════════════════════════════════════════════════

class Env:
    def __init__(self, monkeypatch):
        self.sent = []
        self.t = 1000.0
        self.mm = ManualOrderMonitor()
        self.mm._clock = lambda: self.t
        env = self

        async def place_order(**kw):
            env.sent.append(kw)
            return {"trade_id": f"C{len(env.sent)}", "status": "PendingSubmit"}

        monkeypatch.setattr(broker, "place_order", place_order)

    def watch(self, qty=1, entry=49000.0, sl=50, tp=80):
        wid = self.mm.add("TMF", 1, qty, entry, sl, tp)
        w = self.mm._watches[wid]
        w.seen = True
        return w

    def check(self, w, price, status=None, fills=None):
        pos = [{"code": "TMFJ6", "direction": "Buy", "quantity": w.quantity, "price": w.entry_price, "last_price": price}]
        asyncio.run(self.mm._check(w, pos, status or {}, fills or {}))


@pytest.fixture
def env(monkeypatch, rec):
    return Env(monkeypatch)


def test_a_stop_loss_trigger_is_announced_with_prices_and_urgency(env, rec):
    w = env.watch()
    env.check(w, 49040.0)                                                          # 還沒到停損
    assert rec.calls == []
    env.check(w, 48950.0)                                                          # −50 點
    (c,) = rec.calls
    assert c["title"] == "🛑 停損觸發" and c["priority"] == "urgent" and c["key"] == f"trig:{w.id}"
    assert c["message"] == "TMF 多 1 口：進場 49,000，現價 48,950（-50 點），已送出平倉單，等待成交確認。"
    assert len(env.sent) == 1 and w.trig_kind == "sl"


def test_a_take_profit_trigger_is_announced_as_such(env, rec):
    w = env.watch()
    env.check(w, 49080.0)
    (c,) = rec.calls
    assert c["title"] == "🎯 停利觸發" and c["priority"] == "high" and "（+80 點）" in c["message"] and w.trig_kind == "tp"


def test_the_trigger_is_announced_once_not_on_every_retry_tick(env, rec):
    w = env.watch()
    env.check(w, 48950.0)
    env.check(w, 48940.0)                                                          # 前一張還在等結果：不會再送單、也不會再通知
    assert len(env.sent) == 1 and [c["title"] for c in rec.calls] == ["🛑 停損觸發"]


def test_completion_is_announced_when_the_close_order_is_fully_filled(env, rec):
    w = env.watch(qty=2)
    env.check(w, 48950.0)
    rec.calls.clear()
    env.check(w, 48950.0, status={"C1": "Filled"}, fills={"C1": 2})                # 平倉單全數成交 → 移除監看
    (c,) = rec.calls
    assert c["title"] == "✅ 平倉完成" and c["message"] == "TMF 多 2 口（停損），共送出 1 次平倉單。" and c["key"] == f"done:{w.id}"
    assert w.id not in env.mm._watches


def test_no_completion_message_when_the_position_was_closed_by_hand(env, rec):
    w = env.watch()
    asyncio.run(env.mm._check(w, [], {}, {}))                                      # 你自己在券商 App 平掉了：監看移除，但不是我們平的
    assert rec.calls == [] and w.id not in env.mm._watches


def test_retries_are_announced_from_the_third_send_on(env, rec):
    w = env.watch()
    for i in range(1, 5):
        env.t += 100
        w.close_order_id, w.retry_after = "", 0.0
        env.check(w, 48950.0)
    titles = [c["title"] for c in rec.calls]
    assert titles == ["🛑 停損觸發", "⚠️ 平倉單沒成交，重送中", "⚠️ 平倉單沒成交，重送中"]                # 第 1 次是觸發；第 2 次沒有；第 3、4 次各一則
    assert [c["key"] for c in rec.calls[1:]] == [f"retry:{w.id}:3", f"retry:{w.id}:4"]
    assert "第 3 次送出平倉單" in rec.calls[1]["message"] and f"超過 {mmod.MAX_CLOSE_ATTEMPTS} 次會放棄" in rec.calls[1]["message"]


def test_giving_up_is_an_urgent_message(env, rec):
    w = env.watch()
    w.close_attempts = mmod.MAX_CLOSE_ATTEMPTS
    env.check(w, 48950.0)
    (c,) = rec.calls
    assert c["title"] == "🚨 停損平不掉！請立刻手動處理" and c["priority"] == "urgent" and w.close_gave_up
    assert f"已送 {mmod.MAX_CLOSE_ATTEMPTS} 次" in c["message"] and "請立刻到券商 App 處理" in c["message"] and env.sent == []
    env.check(w, 48940.0)
    assert len(rec.calls) == 1                                                     # 放棄之後不再重複通知


def test_a_failing_notifier_never_breaks_the_stop(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("通知壞了")

    monkeypatch.setattr(mmod, "notify", boom)
    w = env.watch()
    env.check(w, 48950.0)
    assert len(env.sent) == 1                                                      # 平倉單照送


# ══ 保證金風險 ═══════════════════════════════════════════════════

def margin(equity=51492.0, maintenance=0.0, call=0.0, amount=None):
    return {"equity": equity, "equity_amount": equity if amount is None else amount, "margin_call": call, "initial_margin": maintenance * 1.3, "maintenance_margin": maintenance}


def test_assess_levels_and_the_ratio():
    assert mw.assess(None)["level"] == "ok" and mw.assess({})["ratio"] is None
    assert mw.assess(margin())["level"] == "ok" and mw.assess(margin())["ratio"] is None             # 沒有持倉（維持保證金 0）
    a = mw.assess(margin(51492, 26900))
    assert a["level"] == "ok" and a["ratio"] == pytest.approx(51492 / 26900 * 100)
    assert mw.assess(margin(40000, 26900))["level"] == "warn"                                         # 148.7% < 150%
    assert mw.assess(margin(40350, 26900))["level"] == "ok" and mw.assess(margin(40349, 26900))["level"] == "warn"    # 剛好 150%
    assert mw.assess(margin(32280, 26900))["level"] == "warn" and mw.assess(margin(32279, 26900))["level"] == "urgent"   # 剛好 120%
    assert mw.assess(margin(20000, 26900))["level"] == "urgent"
    assert mw.assess(margin(51492, 26900, call=1500))["level"] == "call"                              # 券商說要追繳：不管比率
    assert mw.assess(margin(30000, 26900, amount=45000))["level"] == "ok"                              # 用權益總值（equity_amount）算


def test_thresholds_follow_the_env_and_urgent_never_exceeds_warn(monkeypatch):
    monkeypatch.setenv("NOTIFY_MARGIN_WARN", "200")
    monkeypatch.setenv("NOTIFY_MARGIN_URGENT", "130")
    assert mw.thresholds() == (200.0, 130.0) and mw.assess(margin(51492, 26900))["level"] == "warn"
    monkeypatch.setenv("NOTIFY_MARGIN_URGENT", "300")
    assert mw.thresholds() == (200.0, 200.0)
    monkeypatch.setenv("NOTIFY_MARGIN_WARN", "abc")
    monkeypatch.setenv("NOTIFY_MARGIN_URGENT", "-5")
    assert mw.thresholds() == (150.0, 120.0)


def test_margin_messages(rec):
    w = mw.MarginWatcher()
    assert w.check(margin(40000, 26900)) == "warn"
    (c,) = rec.calls
    assert c["title"] == "⚠️ 保證金風險偏高" and c["priority"] == "high" and c["key"] == "margin:warn" and c["cooldown"] == 1800
    assert "權益 40,000 元、維持保證金 26,900 元" in c["message"] and "＝ 149%" in c["message"] and "低於 100% 會被追繳" in c["message"]
    rec.calls.clear()
    assert w.check(margin(30000, 26900)) == "urgent"
    assert rec.calls[0]["title"] == "🚨 保證金風險：權益快被維持保證金追上" and rec.calls[0]["priority"] == "urgent" and rec.calls[0]["cooldown"] == 600
    rec.calls.clear()
    assert w.check(margin(51492, 26900, call=2000)) == "call"
    assert rec.calls[0]["title"] == "🚨 券商回報需要追繳保證金" and "追繳金額 2,000 元" in rec.calls[0]["message"]


def test_recovery_is_announced_once_and_only_after_a_problem(rec):
    w = mw.MarginWatcher()
    assert w.check(margin(51492, 26900)) == "ok" and rec.calls == []                                   # 一直正常：不吵
    w.check(margin(30000, 26900))
    rec.calls.clear()
    assert w.check(margin(51492, 26900)) == "ok"
    (c,) = rec.calls
    assert c["title"] == "✅ 保證金風險已解除" and c["key"] == "margin:recover" and c["priority"] == "default"
    rec.calls.clear()
    w.check(margin(51492, 26900))
    assert rec.calls == []                                                                              # 已經解除過了


def test_margin_check_never_raises(monkeypatch, rec, caplog):
    def boom(*a, **k):
        raise RuntimeError("壞了")

    monkeypatch.setattr(mw, "assess", boom)
    w = mw.MarginWatcher()
    with caplog.at_level(logging.ERROR):
        assert w.check(margin(1, 1)) == "ok"
    assert "保證金風險檢查失敗" in caplog.text


def test_the_margin_cache_refresh_loop_feeds_the_watcher(monkeypatch):
    import api.routes_position as rp
    seen = []
    monkeypatch.setattr(rp.margin_watcher, "check", lambda m: seen.append(m))

    async def margin_():
        return {"equity": 1.0, "maintenance_margin": 0.0}

    async def usage():
        return {}

    async def stop(sec):
        raise asyncio.CancelledError()

    monkeypatch.setattr(broker, "margin", margin_)
    monkeypatch.setattr(broker, "usage", usage)
    monkeypatch.setattr(broker, "_is_connected", True)
    from types import SimpleNamespace
    monkeypatch.setattr(rp, "asyncio", SimpleNamespace(sleep=stop, wait_for=asyncio.wait_for))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rp.cache_refresh_loop())
    assert seen == [{"equity": 1.0, "maintenance_margin": 0.0}]


# ══ 手動單成交 ═══════════════════════════════════════════════════

def deal(price=49050.0, qty=1, trade_id="T1", seq="S1"):
    return {"type": "order_event", "state": "FDeal", "trade_id": trade_id, "price": price, "quantity": qty, "deal_ts": 1.0, "deal_seq": seq}


CTX = {"strategy": "manual", "reason": "manual", "contract": "TMF", "action": "Buy", "qty": 1}


def test_a_manual_fill_is_announced(rec):
    fn.on_deal(deal(), CTX)
    (c,) = rec.calls
    assert c["title"] == "✅ 成交" and c["message"] == "TMF 買進 1 口 @ 49,050（手動）" and c["priority"] == "default"
    assert c["key"] == "fill:T1:S1:1" and c["cooldown"] == 3600


def test_sells_and_partial_fills(rec):
    fn.on_deal(deal(49060.0, 2, seq="S2"), {**CTX, "action": "Sell", "contract": "MXF"})
    assert rec.calls[0]["message"] == "MXF 賣出 2 口 @ 49,060（手動）"
    fn.on_deal(deal(49061.0, 1, seq="S3"), {**CTX, "action": "Sell", "contract": "MXF"})
    assert len(rec.calls) == 2 and rec.calls[0]["key"] != rec.calls[1]["key"]                          # 部分成交各通知一次，key 不同才不會被當成重複


def test_the_same_deal_delivered_twice_has_the_same_key(rec):
    fn.on_deal(deal(), CTX)
    fn.on_deal(deal(), CTX)
    assert rec.calls[0]["key"] == rec.calls[1]["key"]                                                  # notify 的冷卻會擋掉第二則


def test_default_mode_ignores_strategies_and_stop_closes(rec):
    fn.on_deal(deal(), {**CTX, "strategy": "scalp", "reason": "entry"})
    fn.on_deal(deal(), {**CTX, "strategy": "manual_monitor", "reason": "sl"})                          # 停損停利有自己的訊息
    fn.on_deal(deal(), {})                                                                              # 不是這個系統下的單
    assert rec.calls == []


def test_all_mode_announces_strategies_and_unknown_orders_but_still_not_stop_closes(rec, monkeypatch):
    monkeypatch.setenv("NOTIFY_FILLS", "all")
    fn.on_deal(deal(), {**CTX, "strategy": "scalp"})
    fn.on_deal(deal(seq="S9"), {})
    fn.on_deal(deal(seq="S8"), {**CTX, "strategy": "manual_monitor"})
    assert [c["message"] for c in rec.calls] == ["TMF 買進 1 口 @ 49,050（策略 scalp）", "? ? 1 口 @ 49,050（來源不明）"]


def test_off_mode_and_garbage_values(rec, monkeypatch):
    monkeypatch.setenv("NOTIFY_FILLS", "off")
    fn.on_deal(deal(), CTX)
    assert rec.calls == []
    monkeypatch.setenv("NOTIFY_FILLS", "亂寫")
    assert fn.mode() == "manual"
    fn.on_deal(deal(), CTX)
    assert len(rec.calls) == 1


def test_zero_quantity_deals_are_skipped(rec):
    fn.on_deal(deal(qty=0), CTX)
    assert rec.calls == []


def test_on_deal_never_raises(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("壞了")

    monkeypatch.setattr(fn, "notify", boom)
    with caplog.at_level(logging.ERROR):
        fn.on_deal(deal(), CTX)
    assert "成交推播失敗" in caplog.text


def test_trade_log_remembers_orders_and_calls_the_listeners_on_deals(tmp_path, rec):
    tl = TradeLog(tmp_path / "t.db")
    fn.install(tl)
    fn.install(tl)                                                                                      # 可重複呼叫
    assert tl.deal_listeners == [fn.on_deal]
    with tl.context(strategy="manual", reason="manual"):
        tl.record_order(contract="TMF", action="Buy", qty=1, trade_id="T9", status="PendingSubmit")
    tl.record_event({"state": "FOrder", "trade_id": "T9"})                                              # 委託回報不是成交：不通知
    assert rec.calls == []
    tl.record_event({"state": "FDeal", "trade_id": "T9", "price": 49000.0, "quantity": 1, "deal_seq": "X"})
    assert [c["message"] for c in rec.calls] == ["TMF 買進 1 口 @ 49,000（手動）"]


def test_trade_log_keeps_only_the_most_recent_orders(tmp_path):
    from core import trade_log as tlm
    tl = TradeLog(tmp_path / "t.db")
    for i in range(tlm.RECENT_MAX + 25):
        tl.record_order(contract="TMF", action="Buy", qty=1, trade_id=f"T{i}")
    assert len(tl._recent) == tlm.RECENT_MAX and "T0" not in tl._recent and f"T{tlm.RECENT_MAX + 24}" in tl._recent


def test_a_broken_listener_does_not_stop_the_event_from_being_recorded(tmp_path, caplog):
    tl = TradeLog(tmp_path / "t.db")

    def bad(ev, ctx):
        raise RuntimeError("壞了")

    tl.deal_listeners.append(bad)
    with caplog.at_level(logging.ERROR):
        tl.record_event({"state": "FDeal", "trade_id": "T1", "price": 1.0, "quantity": 1})
    assert "成交監聽失敗" in caplog.text and tl._q.qsize() == 1                                          # 事件照常進佇列


# ══ 當沖倉位收盤前提醒 ═══════════════════════════════════════════

def at(hh, mm, day=14, ss=0):          # 2026-10-14 是週三
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=TPE)


def pos(code="MXFJ6", direction="Buy", qty=1):
    return {"code": code, "direction": direction, "quantity": qty}


@pytest.fixture(autouse=True)
def clean_marks():
    dt._MARKS.clear()
    yield
    dt._MARKS.clear()


def test_due_only_at_the_two_reminder_minutes_on_weekdays():
    assert rem.due(at(13, 15)) == "13:15" and rem.due(at(13, 15, ss=59)) == "13:15" and rem.due(at(13, 25)) == "13:25"
    assert rem.due(at(13, 14, ss=59)) is None and rem.due(at(13, 16)) is None and rem.due(at(13, 24)) is None and rem.due(at(10, 0)) is None
    assert rem.due(at(13, 15, day=17)) is None and rem.due(at(13, 15, day=18)) is None                # 週六、週日
    assert rem.due(datetime(2026, 10, 14, 5, 15, tzinfo=timezone.utc)) == "13:15"                      # 時區換算


def test_marks_are_per_day_and_per_direction():
    dt.note_open("mxf", 1, at(10, 0))
    assert dt.marks(at(11, 0)) == [("MXF", 1)] and dt.marks(at(11, 0, day=15)) == []                   # 隔天就不算
    dt.note_open("TXF", -1, at(10, 0))
    assert sorted(dt.marks(at(11, 0))) == [("MXF", 1), ("TXF", -1)]


def test_open_daytrade_positions_match_marks_and_watches():
    marks = [("MXF", 1)]
    assert rem.open_daytrade_positions([pos()], [], marks) == [{"code": "MXFJ6", "direction": "Buy", "quantity": 1}]
    assert rem.open_daytrade_positions([pos(direction="Sell")], [], marks) == []                        # 方向不同
    assert rem.open_daytrade_positions([pos(code="TXFJ6")], [], marks) == []                            # 合約不同
    assert rem.open_daytrade_positions([pos(qty=0)], [], marks) == []
    watch = {"octype": "DayTrade", "seen": True, "direction": "Buy", "match_code": "MXF", "is_option": False}
    assert len(rem.open_daytrade_positions([pos()], [watch], [])) == 1                                  # 重啟後靠落地的監看認得出來
    assert rem.open_daytrade_positions([pos()], [{**watch, "seen": False}], []) == []                   # 還沒看到部位的監看不算
    assert rem.open_daytrade_positions([pos()], [{**watch, "octype": "Auto"}], []) == []                # 不是當沖倉別
    assert rem.open_daytrade_positions([pos()], [{**watch, "match_code": ""}], []) == []                # 沒有合約代碼的監看對不上任何部位


def test_reminder_message():
    t, m = rem.message("13:15", [{"code": "MXFJ6", "direction": "Buy", "quantity": 1}, {"code": "TXFJ6", "direction": "Sell", "quantity": 2}])
    assert t == "⏰ 當沖倉位還沒平（13:15）" and m.startswith("MXFJ6 多 1 口、TXFJ6 空 2 口。") and "約 13:30" in m and "以期貨商規定為準" in m


def test_check_sends_only_when_due_and_a_daytrade_position_is_open(rec):
    dt.note_open("MXF", 1, at(10, 0))
    assert rem.check(at(12, 0), [pos()], []) is None and rec.calls == []                                # 不是提醒時間
    assert rem.check(at(13, 15), [], []) is None and rec.calls == []                                    # 已經平掉了
    assert rem.check(at(13, 15), [pos(code="TMFJ6")], []) is None                                       # 持倉不是當沖的
    items = rem.check(at(13, 15, ss=20), [pos()], [])
    assert items == [{"code": "MXFJ6", "direction": "Buy", "quantity": 1}]
    (c,) = rec.calls
    assert c["title"] == "⏰ 當沖倉位還沒平（13:15）" and c["priority"] == "urgent" and c["key"] == "daytrade:2026-10-14:13:15" and c["cooldown"] == 3600
    rem.check(at(13, 25), [pos()], [])
    assert [x["key"] for x in rec.calls] == ["daytrade:2026-10-14:13:15", "daytrade:2026-10-14:13:25"]    # 兩個時間點各一則


def test_the_reminder_loop_calls_check_with_the_current_state_and_survives_errors(monkeypatch, caplog):
    calls = []
    n = {"i": 0}

    def fake_check(now, positions, watches):
        calls.append((positions, watches))
        if len(calls) == 1:
            raise RuntimeError("壞了")

    async def fake_sleep(sec):
        n["i"] += 1
        if n["i"] > 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(rem, "check", fake_check)
    monkeypatch.setattr(rem.asyncio, "sleep", fake_sleep)
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        asyncio.run(rem.reminder_loop(lambda: [pos()], lambda: [{"id": "w"}]))
    assert calls == [([pos()], [{"id": "w"}])] * 2 and "收盤前提醒迴圈例外" in caplog.text


def test_the_order_route_registers_daytrade_orders(monkeypatch):
    import api.routes_order as ro
    from api.routes_order import ManualOrderRequest

    async def place_order(**kw):
        return {"trade_id": "T1", "status": "PendingSubmit"}

    monkeypatch.setattr(ro.broker, "place_order", place_order)
    monkeypatch.setattr(ro, "check_octype", lambda *a, **k: None)
    monkeypatch.setattr(ro.quote_hub, "last_price_by_prefix", lambda c: 49000.0)
    asyncio.run(ro.place_order(ManualOrderRequest(contract="MXF", action="Buy", quantity=1, octype="DayTrade")))
    assert dt.marks() == [("MXF", 1)]
    dt._MARKS.clear()
    asyncio.run(ro.place_order(ManualOrderRequest(contract="MXF", action="Buy", quantity=1, octype="Auto")))
    assert dt.marks() == []                                                                             # 沒勾當沖就不登記


# ── 補：變異測試抓到的缺口 ───────────────────────────────────────

def test_any_positive_margin_call_is_a_call_and_a_zero_threshold_is_ignored(monkeypatch):
    assert mw.assess(margin(51492, 26900, call=1.0))["level"] == "call"                               # 再小的追繳金額也是追繳
    for name in ("NOTIFY_MARGIN_WARN", "NOTIFY_MARGIN_URGENT"):
        monkeypatch.setenv(name, "0")
    assert mw.thresholds() == (150.0, 120.0)                                                           # 0 不是有效的門檻


def test_a_short_stop_reports_its_loss_with_the_right_sign(env, rec, monkeypatch):
    wid = env.mm.add("TMF", -1, 1, 49000.0, 50, 0)                                                     # 空單：價格漲 50 點＝虧 50 點
    w = env.mm._watches[wid]
    w.seen = True
    pos = [{"code": "TMFJ6", "direction": "Sell", "quantity": 1, "price": 49000.0, "last_price": 49050.0}]
    asyncio.run(env.mm._check(w, pos, {}, {}))
    (c,) = rec.calls
    assert c["title"] == "🛑 停損觸發" and c["message"] == "TMF 空 1 口：進場 49,000，現價 49,050（-50 點），已送出平倉單，等待成交確認。"


def test_completion_names_what_triggered_and_is_announced_only_once(env, rec):
    w = env.watch(qty=1)
    env.check(w, 49080.0)                                                                              # 停利
    rec.calls.clear()
    env.check(w, 49080.0, status={"C1": "Filled"}, fills={"C1": 1})
    (c,) = rec.calls
    assert c["message"] == "TMF 多 1 口（停利），共送出 1 次平倉單。" and c["cooldown"] == 3600


def test_orders_without_a_trade_id_are_not_remembered(tmp_path):
    tl = TradeLog(tmp_path / "t.db")
    tl.record_order(contract="TMF", action="Buy", qty=1, trade_id="", error="下單失敗")
    assert len(tl._recent) == 0


def test_only_deal_events_reach_the_listeners(tmp_path):
    tl = TradeLog(tmp_path / "t.db")
    seen = []
    tl.deal_listeners.append(lambda ev, ctx: seen.append(ev["state"]))
    tl.record_event({"state": "FOrder", "trade_id": "T1"})
    tl.record_event({"state": "Submitted", "trade_id": "T1"})
    tl.record_event({"state": "FDeal", "trade_id": "T1", "price": 1.0, "quantity": 1})
    assert seen == ["FDeal"]


def test_a_watch_on_the_other_side_is_not_a_daytrade_position():
    watch = {"octype": "DayTrade", "seen": True, "direction": "Sell", "match_code": "MXF", "is_option": False}
    assert rem.open_daytrade_positions([pos(direction="Buy")], [watch], []) == []                      # 空單的當沖監看不算多單部位
    assert len(rem.open_daytrade_positions([pos(direction="Sell")], [watch], [])) == 1
