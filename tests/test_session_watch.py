"""券商連線假死偵測（update.md 發現 65）：可判斷時段、連線錯誤（A1）、worker 不回話（A2）、報價停止（B）、狀態轉換、
broker 的鉤子、/api/health 的回報、休市日曆的快取、週末放慢判斷，以及照 10/21 事故重演一次。"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

import pytest

from core import session_watch as sw
from core.daytrade import TAIPEI

SESSION_ERR = ("list_positions: Shioaji error Session error SolClient send request api/v1/portfolio/position_unit, code: NotReady, "
               "Error ErrorInfo { sub_code: SubCode(SessionNotEstablished), error_str: \"Unable to wait for session '(c0,s1)_sinopac' to be established\" }")


def tpe(*a):
    return datetime(*a, tzinfo=TAIPEI)


def ts(*a):
    return tpe(*a).timestamp()


UNKNOWN = lambda d: None                       # noqa: E731  休市日曆查不到：當平日


# ── 可判斷時段 ───────────────────────────────────────────────────

@pytest.mark.parametrize("when,expect", [
    (tpe(2026, 10, 19, 7, 59), None), (tpe(2026, 10, 19, 8, 0), "day"), (tpe(2026, 10, 19, 13, 49), "day"), (tpe(2026, 10, 19, 13, 50), None),
    (tpe(2026, 10, 19, 14, 49), None), (tpe(2026, 10, 19, 14, 50), "night"), (tpe(2026, 10, 19, 23, 30), "night"),
    (tpe(2026, 10, 20, 4, 59), "night"), (tpe(2026, 10, 20, 5, 0), None),                     # 每天 05:00～08:00 券商維護：不判斷
    (tpe(2026, 10, 24, 0, 30), "night"), (tpe(2026, 10, 24, 5, 0), None),                     # 週五的夜盤延續到週六 05:00
    (tpe(2026, 10, 25, 10, 0), None), (tpe(2026, 10, 26, 0, 30), None),                       # 週日、週一凌晨（前一天週日）
])
def test_live_window(when, expect):
    assert sw.live_window(when, UNKNOWN) == expect


def test_live_window_holidays():
    closed = {date(2026, 10, 21)}
    is_open = lambda d: d not in closed                                                 # noqa: E731
    assert sw.live_window(tpe(2026, 10, 21, 10, 0), is_open) is None
    assert sw.live_window(tpe(2026, 10, 21, 20, 0), is_open) is None
    assert sw.live_window(tpe(2026, 10, 22, 1, 0), is_open) is None                    # 前一天休市：凌晨沒有夜盤
    assert sw.live_window(tpe(2026, 10, 22, 9, 0), is_open) == "day"
    assert sw.live_window(tpe(2026, 10, 24, 10, 0), lambda d: True) == "day"            # 日曆說開就開（補班日）


@pytest.mark.parametrize("when,expect", [
    (tpe(2026, 10, 24, 5, 29), False), (tpe(2026, 10, 24, 5, 30), True), (tpe(2026, 10, 25, 12, 0), True),
    (tpe(2026, 10, 26, 7, 29), True), (tpe(2026, 10, 26, 7, 30), False), (tpe(2026, 10, 23, 12, 0), False),
])
def test_weekend_quiet(when, expect):
    assert sw.weekend_quiet(when) is expect


# ── A1：連線錯誤 ─────────────────────────────────────────────────

def watch(connected=ts(2026, 10, 21, 9, 0)):
    w = sw.SessionWatch()
    w.on_connected(connected)
    return w


def ev(w, now):
    return w.evaluate(now.timestamp(), now, sw.live_window(now, UNKNOWN), has_subs=False)


def test_a1_needs_three_session_errors_over_sixty_seconds_without_success():
    w = watch()
    t0 = ts(2026, 10, 21, 10, 56)
    w.on_response(t0, SESSION_ERR)
    w.on_response(t0 + 30, SESSION_ERR)
    assert ev(w, tpe(2026, 10, 21, 10, 58)) is None                                     # 只有 2 次
    w.on_response(t0 + 59, SESSION_ERR)
    assert ev(w, tpe(2026, 10, 21, 10, 56, 59)) is None                                 # 3 次但還不到 60 秒
    r = ev(w, tpe(2026, 10, 21, 10, 57))
    assert r and "連續 3 次連線錯誤" in r and "60 秒" in r and "list_positions: SessionNotEstablished" in r


def test_short_error():
    assert sw.short_error(SESSION_ERR) == "list_positions: SessionNotEstablished"
    assert sw.short_error("kbars: Shioaji error Session error SolClient send request api/v1/data/kbars, code: NotReady") == "kbars: NotReady"
    assert sw.short_error("奇怪的錯誤") == "奇怪的錯誤"


def test_a1_success_resets_and_other_errors_neither_count_nor_reset():
    w = watch()
    t0 = ts(2026, 10, 21, 11, 0)
    w.on_response(t0, SESSION_ERR)
    w.on_response(t0 + 10, SESSION_ERR)
    w.on_response(t0 + 20, None)                                                        # 中間成功一次：重來
    w.on_response(t0 + 30, SESSION_ERR)
    assert ev(w, tpe(2026, 10, 21, 11, 2)) is None
    w.on_response(t0 + 40, "找不到 trade_id=abc")                                       # 業務錯誤：不算、也不打斷
    w.on_response(t0 + 50, SESSION_ERR)
    w.on_response(t0 + 60, SESSION_ERR)
    assert ev(w, tpe(2026, 10, 21, 11, 1, 29)) is None                                  # 從 t0+30 起還不到 60 秒
    assert "連續 3 次" in ev(w, tpe(2026, 10, 21, 11, 1, 30))


def test_a1_outside_the_window_does_nothing():
    w = watch(ts(2026, 10, 25, 9, 0))
    t0 = ts(2026, 10, 25, 10, 0)                                                        # 週日
    for k in range(10):
        w.on_response(t0 + 10 * k, SESSION_ERR)
    assert ev(w, tpe(2026, 10, 25, 10, 5)) is None


def test_not_connected_yet_means_no_verdict():
    w = sw.SessionWatch()
    for k in range(5):
        w.on_response(ts(2026, 10, 21, 10, 0) + 20 * k, SESSION_ERR)
    assert ev(w, tpe(2026, 10, 21, 10, 5)) is None


# ── A2：worker 不回話 ────────────────────────────────────────────

def test_a2_worker_silent():
    w = watch()
    t0 = ts(2026, 10, 21, 11, 0)
    for k in range(3):
        w.on_request(t0 + 5 * k)
    assert ev(w, tpe(2026, 10, 21, 11, 1, 29)) is None
    r = ev(w, tpe(2026, 10, 21, 11, 1, 30))
    assert r and "90 秒沒有回覆" in r and "3 個在等" in r
    w.on_response(t0 + 95, "其他錯誤")                                                  # 有任何回覆就不是不回話
    assert ev(w, tpe(2026, 10, 21, 11, 2)) is None


def test_a2_two_requests_are_not_enough():
    w = watch()
    w.on_request(ts(2026, 10, 21, 11, 0))
    w.on_request(ts(2026, 10, 21, 11, 0, 5))
    assert ev(w, tpe(2026, 10, 21, 11, 5)) is None


# ── B：報價停止 ──────────────────────────────────────────────────

def evb(w, now, subs=True):
    return w.evaluate(now.timestamp(), now, sw.live_window(now, UNKNOWN), has_subs=subs)


def test_b_day_quiet():
    w = watch(ts(2026, 10, 21, 8, 0))
    w.on_quote(ts(2026, 10, 21, 10, 0))
    assert evb(w, tpe(2026, 10, 21, 10, 2, 59)) is None
    r = evb(w, tpe(2026, 10, 21, 10, 3))
    assert r and "日盤中 180 秒沒有收到任何報價" in r
    assert evb(w, tpe(2026, 10, 21, 10, 3), subs=False) is None                         # 沒有訂閱期貨：不判斷
    w.on_quote(ts(2026, 10, 21, 10, 3))
    assert evb(w, tpe(2026, 10, 21, 10, 3)) is None


def test_b_day_counts_from_the_open_and_from_connection():
    w = watch(ts(2026, 10, 21, 7, 0))
    w.on_quote(ts(2026, 10, 20, 23, 0))                                                 # 昨晚的報價
    assert evb(w, tpe(2026, 10, 21, 8, 47, 59)) is None                                 # 從 08:45 起算
    assert evb(w, tpe(2026, 10, 21, 8, 48))
    w2 = watch(ts(2026, 10, 21, 10, 0))                                                 # 10:00 才連上、還沒有報價
    assert evb(w2, tpe(2026, 10, 21, 10, 2, 59)) is None                                # 剛連上的寬限
    assert evb(w2, tpe(2026, 10, 21, 10, 3))
    assert evb(w2, tpe(2026, 10, 21, 13, 45)) is None                                   # 收盤後不看日盤規則


def test_b_night_needs_a_quote_tonight():
    w = watch(ts(2026, 10, 21, 14, 50))
    assert evb(w, tpe(2026, 10, 21, 16, 0)) is None                                     # 今晚一筆都沒有：可能沒有夜盤，不判斷
    w.on_quote(ts(2026, 10, 21, 13, 40))
    assert evb(w, tpe(2026, 10, 21, 16, 0)) is None                                     # 日盤的報價不算今晚
    w.on_quote(ts(2026, 10, 21, 23, 0))
    assert evb(w, tpe(2026, 10, 21, 23, 4, 59)) is None
    assert "夜盤中 300 秒" in evb(w, tpe(2026, 10, 21, 23, 5))
    assert "夜盤中" in evb(w, tpe(2026, 10, 22, 0, 30))                                 # 過了午夜還是同一個夜盤
    w.on_quote(ts(2026, 10, 22, 4, 58))
    assert evb(w, tpe(2026, 10, 22, 5, 30)) is None                                     # 05:00 以後不判斷


# ── 狀態轉換與 snapshot ──────────────────────────────────────────

def test_check_sets_and_clears_with_logs(caplog):
    w = watch(ts(2026, 10, 21, 8, 0))
    w.on_quote(ts(2026, 10, 21, 10, 0))
    caplog.set_level(logging.INFO, logger="core.session_watch")
    now = tpe(2026, 10, 21, 10, 5)
    assert w.check(now.timestamp(), now, "day", True)
    assert w.stale_reason and "券商連線假死" in caplog.text
    w.on_quote(now.timestamp())
    assert w.check(now.timestamp() + 1, now + timedelta(seconds=1), "day", True) is None
    assert w.stale_reason is None and "券商連線恢復" in caplog.text
    s = w.snapshot(now.timestamp() + 1)
    assert s["stale_reason"] is None and s["last_quote_age"] == 1.0 and s["err_n"] == 0


def test_reconnect_resets_counters():
    w = watch()
    for k in range(5):
        w.on_response(ts(2026, 10, 21, 11, 0) + 20 * k, SESSION_ERR)
        w.on_request(ts(2026, 10, 21, 11, 0) + 20 * k)
    w.on_connected(ts(2026, 10, 21, 11, 3))
    assert (w.err_n, w.unanswered_n, w.last_quote, w.err_since) == (0, 0, None, None)
    assert ev(w, tpe(2026, 10, 21, 11, 4)) is None


def test_replay_of_the_1021_incident():
    """10/21：10:55 起查詢一直回 SessionNotEstablished、報價停止；部位每 5 秒查一次（失敗後退避到 60 秒）。要在 10:57 前判定。"""
    w = watch(ts(2026, 10, 21, 8, 40))
    t = ts(2026, 10, 21, 8, 45)
    while t < ts(2026, 10, 21, 10, 55):
        w.on_quote(t)
        w.on_request(t)
        w.on_response(t + 0.2, None)
        t += 5
    found = None
    delay, fails = 5, 0
    while t < ts(2026, 10, 21, 11, 10) and found is None:
        w.on_request(t)
        w.on_response(t + 2, SESSION_ERR)
        fails += 1
        delay = min(5 * 2 ** fails, 60)
        for k in range(int(delay // 15) + 1):                                           # 背景每 15 秒判斷一次
            now_ts = t + 2 + 15 * k
            now = datetime.fromtimestamp(now_ts, TAIPEI)
            if w.check(now_ts, now, sw.live_window(now, UNKNOWN), True):
                found = now
                break
        t += delay
    assert found is not None and found <= tpe(2026, 10, 21, 10, 58), found


# ── broker 的鉤子 ────────────────────────────────────────────────

def test_broker_hooks(monkeypatch):
    from core.broker import broker
    w = sw.SessionWatch()
    monkeypatch.setattr("core.broker.session_watch", w)
    monkeypatch.setattr(broker, "_loop", None)
    monkeypatch.setattr(broker, "_cmd_q", None)
    monkeypatch.setattr(broker, "_known_subs", [])
    monkeypatch.setattr(broker, "_known_opt_subs", [])
    broker._dispatch({"type": "connected"})
    assert w.connected_at is not None
    broker._dispatch({"type": "quote", "data": {}})
    assert w.last_quote is not None
    broker._dispatch({"type": "response", "req_id": "x", "error": SESSION_ERR})
    assert w.err_n == 1
    broker._dispatch({"type": "response", "req_id": "y", "result": []})
    assert w.err_n == 0 and w.last_ok is not None


def test_acall_counts_a_sent_request(monkeypatch):
    from core.broker import broker
    w = sw.SessionWatch()
    sent = []

    class Q:
        def put_nowait(self, x):
            sent.append(x)
    monkeypatch.setattr("core.broker.session_watch", w)
    monkeypatch.setattr(broker, "_is_connected", True)
    monkeypatch.setattr(broker, "_cmd_q", Q())
    monkeypatch.setattr(broker, "_pending", {})
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(broker._acall("list_positions", timeout=0.05))
    assert len(sent) == 1 and w.unanswered_n == 1


# ── /api/health ──────────────────────────────────────────────────

def test_health_reports_stale(monkeypatch):
    import main
    from core.broker import broker
    monkeypatch.setattr(broker, "_is_connected", True)
    monkeypatch.setattr(main.session_watch, "stale_reason", None)
    h = main.health()
    assert h["broker_connected"] == "True" and "session" not in h
    monkeypatch.setattr(main.session_watch, "stale_reason", "日盤中 200 秒沒有收到任何報價")
    h = main.health()
    assert h["broker_connected"] == "False" and h["session"] == "stale" and "200 秒" in h["stale_reason"]
    assert main.health_session()["stale_reason"] == "日盤中 200 秒沒有收到任何報價"


# ── 休市日曆與迴圈 ───────────────────────────────────────────────

def test_trading_days_caches_and_retries():
    calls = []

    def check(d):
        calls.append(d)
        if d == date(2026, 10, 20):
            raise OSError("offline")
        return {"open": d != date(2026, 10, 21)}
    days = sw.TradingDays(check)
    t0 = 1_000_000.0
    asyncio.run(days.ensure([date(2026, 10, 21), date(2026, 10, 20)], t0))
    assert days.is_open(date(2026, 10, 21)) is False and days.is_open(date(2026, 10, 20)) is None
    asyncio.run(days.ensure([date(2026, 10, 21), date(2026, 10, 20)], t0 + 10))
    assert calls.count(date(2026, 10, 21)) == 1 and calls.count(date(2026, 10, 20)) == 1          # 查到的不重查；失敗的等一下再試
    asyncio.run(days.ensure([date(2026, 10, 20)], t0 + sw.CAL_RETRY_SEC + 1))
    assert calls.count(date(2026, 10, 20)) == 2


def test_loop_is_off_when_disabled(monkeypatch):
    monkeypatch.setenv("SESSION_WATCH", "false")
    asyncio.run(asyncio.wait_for(sw.session_watch_loop(lambda: True), 1))


# ── 週末放慢帳務查詢 ─────────────────────────────────────────────

def test_weekend_slowdown_of_account_polling():
    import inspect
    from api import routes_order as ro, routes_position as rp
    weekday, weekend = tpe(2026, 10, 21, 10, 0), tpe(2026, 10, 25, 10, 0)
    assert rp.loop_delay(0, weekday) == rp.POSITION_REFRESH_SEC == 5
    assert rp.loop_delay(0, weekend) == rp.WEEKEND_POSITION_SEC == 120
    assert rp.loop_delay(5, weekday) == rp.POSITION_BACKOFF_MAX_SEC
    assert rp.loop_delay(5, weekend) == 120
    assert "loop_delay(fails, datetime.now(TAIPEI))" in inspect.getsource(rp.positions_refresh_loop)
    assert "WEEKEND_CACHE_SEC if weekend_quiet(datetime.now(TAIPEI)) else 60" in inspect.getsource(rp.cache_refresh_loop) and rp.WEEKEND_CACHE_SEC == 600
    assert "WEEKEND_TRADES_SEC if weekend_quiet(datetime.now(TAIPEI)) else 8" in inspect.getsource(ro.trades_refresh_loop) and ro.WEEKEND_TRADES_SEC == 120


def test_a_broken_hook_never_breaks_dispatch_or_orders(monkeypatch):
    """鉤子丟例外也不能讓券商回覆遺失（下單會等到逾時）或讓 _acall 在送出之後才報錯（呼叫端重試＝重複下單）。"""
    from core.broker import broker

    class Boom:
        def __getattr__(self, name):
            def f(*a, **k):
                raise RuntimeError("hook broken")
            return f
    monkeypatch.setattr("core.broker.session_watch", Boom())
    monkeypatch.setattr(broker, "_known_subs", [])
    monkeypatch.setattr(broker, "_known_opt_subs", [])
    monkeypatch.setattr(broker, "_cmd_q", None)
    resolved = []

    class Fut:
        def done(self):
            return False

        def set_result(self, v):
            resolved.append(v)

    class Loop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, fn, *a):
            fn(*a)
    monkeypatch.setattr(broker, "_loop", Loop())
    monkeypatch.setattr(broker, "_pending", {"r1": Fut()})
    broker._dispatch({"type": "connected"})
    broker._dispatch({"type": "response", "req_id": "r1", "result": {"ok": 1}})
    assert resolved == [{"ok": 1}]
    sent = []

    class Q:
        def put_nowait(self, x):
            sent.append(x)
    monkeypatch.setattr(broker, "_loop", None)
    monkeypatch.setattr(broker, "_is_connected", True)
    monkeypatch.setattr(broker, "_cmd_q", Q())
    monkeypatch.setattr(broker, "_pending", {})
    with pytest.raises(asyncio.TimeoutError):                                           # 照常等回覆（逾時），不是鉤子的 RuntimeError
        asyncio.run(broker._acall("place_order", timeout=0.05))
    assert len(sent) == 1


def test_trades_refresh_updates_status_only_when_orders_are_open(monkeypatch):
    """1.5.x 的委託快取不會被回報更新：有沒結束的委託才 update_status（多一次帳務查詢），沒有就只讀本機快取（發現 68）。"""
    from api import routes_order as ro
    assert ro.has_unfinished([{"status": "Filled"}, {"status": "Submitted"}]) and not ro.has_unfinished([{"status": "Filled"}, {"status": "Cancelled"}])
    assert not ro.has_unfinished([]) and not ro.has_unfinished(None) and ro.has_unfinished([{"status": "PendingSubmit"}])
    calls = []

    class B:
        is_connected = True

        async def list_trades(self):
            calls.append("plain")
            return [{"id": "1", "status": "PendingSubmit"}]

        async def list_trades_with_status(self):
            calls.append("status")
            return [{"id": "1", "status": "Filled"}]
    monkeypatch.setattr(ro, "broker", B())
    monkeypatch.setattr(ro, "_trades_cache", [])
    n = {"k": 0}

    async def fake_sleep(sec):
        n["k"] += 1
        if n["k"] > 4:
            raise asyncio.CancelledError
    monkeypatch.setattr(ro.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ro.trades_refresh_loop())
    assert calls == ["plain", "status", "plain"]                                           # 沒掛單 → 讀快取；看到 PendingSubmit → 更新狀態；都結束了 → 回到只讀快取
    assert ro._trades_cache == [{"id": "1", "status": "PendingSubmit"}]
