"""合約到期提醒（update.md 發現 37）：代碼解析、最後交易日（第三個週三、休市順延）、三個提醒時間點、訊息。只通知，測試用假的 notify 與假的日曆，不碰網路。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

import core.reminders as rem
from core.daytrade import TAIPEI


def tpe(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TAIPEI)


ALWAYS_OPEN = lambda d: True  # noqa: E731


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message, *, key=None, cooldown=0.0, priority="high", wait=False):
        self.calls.append({"title": title, "message": message, "key": key, "cooldown": cooldown, "priority": priority})
        return True


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(rem, "notify", r)
    return r


def pos(code="TMFJ6", direction="Buy", qty=2):
    return {"code": code, "direction": direction, "quantity": qty}


# ── 代碼解析與到期日 ─────────────────────────────────────────

@pytest.mark.parametrize("code,expect", [
    ("TMFJ6", (2026, 10, True)), ("MXFA7", (2027, 1, True)), ("TXFL6", (2026, 12, True)),
    ("TXO49800J6", (2026, 10, False)),                  # 買權：A～L
    ("TXO20000V6", (2026, 10, False)),                  # 賣權：M～X（V＝10 月）
    ("TXO20000M6", (2026, 1, False)), ("TXO20000X6", (2026, 12, False)),
    ("TMFR1", None), ("TXFR1", None), ("TMF", None), ("TX122500J6", None), ("", None), ("XYZJ6", None),
])
def test_code_month(code, expect):
    assert rem.code_month(code, date(2026, 10, 15)) == expect


def test_year_digit_picks_the_nearest_year_not_before_last_year():
    assert rem.code_month("TMFA5", date(2026, 10, 15)) == (2025, 1, True)       # 去年
    assert rem.code_month("TMFA4", date(2026, 10, 15)) == (2034, 1, True)       # 前年的個位數 → 往後找（不會有這種持倉，但不能算成過去）
    assert rem.code_month("TMFA6", date(2026, 1, 2)) == (2026, 1, True)
    assert rem.code_month("TMFA7", date(2026, 12, 30)) == (2027, 1, True)


@pytest.mark.parametrize("y,m,d", [(2026, 10, 21), (2026, 1, 21), (2026, 4, 15), (2026, 7, 15), (2027, 3, 17), (2026, 12, 16)])
def test_third_wednesday(y, m, d):
    got = rem.third_wednesday(y, m)
    assert got == date(y, m, d) and got.weekday() == 2


def test_expiry_moves_to_the_next_open_day():
    closed = {date(2026, 10, 21), date(2026, 10, 22)}
    assert rem.expiry_date(2026, 10, lambda d: d not in closed and d.weekday() < 5) == date(2026, 10, 23)
    assert rem.expiry_date(2026, 10, ALWAYS_OPEN) == date(2026, 10, 21)
    assert rem.expiry_date(2026, 10, lambda d: False) == date(2026, 10, 21)      # 日曆壞了：不亂移


# ── 哪些持倉算快到期 ─────────────────────────────────────────

def test_expiring_window():
    p = [pos("TMFJ6"), pos("TXO49800J6", "Sell", 1), pos("TMFK6"), pos("MXFI6")]
    assert rem.expiring(p, date(2026, 10, 15), ALWAYS_OPEN) == {}                  # 還有 6 天
    got = rem.expiring(p, date(2026, 10, 18), ALWAYS_OPEN)                         # 週日，還有 3 天
    assert list(got) == [date(2026, 10, 21)]
    assert [i["code"] for i in got[date(2026, 10, 21)]] == ["TMFJ6", "TXO49800J6"]
    assert got[date(2026, 10, 21)][0]["is_future"] is True and got[date(2026, 10, 21)][1]["is_future"] is False
    assert rem.expiring(p, date(2026, 10, 21), ALWAYS_OPEN)                        # 當天
    assert rem.expiring(p, date(2026, 10, 22), ALWAYS_OPEN) == {}                  # 過了


def test_expiring_ignores_empty_and_unknown_positions():
    p = [pos("TMFJ6", qty=0), pos("TMFR1"), pos("TX122500J6"), {"code": None, "quantity": 1}]
    assert rem.expiring(p, date(2026, 10, 21), ALWAYS_OPEN) == {}
    assert rem.expiring(None, date(2026, 10, 21), ALWAYS_OPEN) == {}


def test_holiday_shifted_expiry_is_used():
    open_ = lambda d: d != date(2026, 10, 21)  # noqa: E731
    assert list(rem.expiring([pos()], date(2026, 10, 21), open_)) == [date(2026, 10, 22)]    # 第三個週三休市：還有 1 天，不是當天
    assert list(rem.expiring([pos()], date(2026, 10, 22), open_)) == [date(2026, 10, 22)]    # 順延後的最後交易日
    assert rem.expiry_stage(tpe(2026, 10, 21, 10, 0), 1) == "early"                         # 所以 21 號不會誤發「今天最後交易日」


# ── 時間點 ───────────────────────────────────────────────────

@pytest.mark.parametrize("now,days,stage", [
    (tpe(2026, 10, 18, 8, 59), 3, None), (tpe(2026, 10, 18, 9, 0), 3, "early"), (tpe(2026, 10, 20, 23, 0), 1, "early"),
    (tpe(2026, 10, 15, 10, 0), 6, None),
    (tpe(2026, 10, 21, 0, 30), 0, None), (tpe(2026, 10, 21, 8, 49), 0, None), (tpe(2026, 10, 21, 8, 50), 0, "today"),
    (tpe(2026, 10, 21, 12, 29), 0, "today"), (tpe(2026, 10, 21, 12, 30), 0, "final"), (tpe(2026, 10, 21, 13, 29), 0, "final"),
    (tpe(2026, 10, 21, 13, 30), 0, None),
])
def test_stage(now, days, stage):
    assert rem.expiry_stage(now, days) == stage


# ── 推播 ─────────────────────────────────────────────────────

def test_final_reminder_is_urgent_and_aggregated(rec):
    sent = rem.check_expiry(tpe(2026, 10, 21, 12, 40), [pos("TMFJ6"), pos("TXO49800J6", "Sell", 1)], ALWAYS_OPEN)
    assert len(sent) == 1 and len(rec.calls) == 1
    c = rec.calls[0]
    assert c["key"] == "expiry:2026-10-21:final" and c["priority"] == "urgent" and c["cooldown"] == 86400.0
    assert "還剩約 50 分鐘" in c["title"]
    assert "TMFJ6 多 2 口" in c["message"] and "TXO49800J6 空 1 口" in c["message"]


def test_early_reminder_is_sent_once_for_several_days(rec):
    rem.check_expiry(tpe(2026, 10, 18, 10, 0), [pos()], ALWAYS_OPEN)
    c = rec.calls[0]
    assert c["key"] == "expiry:2026-10-21:early" and c["cooldown"] == 5 * 86400.0 and c["priority"] == "high"
    assert "10/21（三）" in c["title"]
    assert "改持下個月合約" in c["message"] and "重啟後端" in c["message"]            # 期貨：結算後要重啟換月
    # 隔天同一個 key（冷卻 5 天）→ 由 notify 的冷卻擋住，這裡 key 不變
    rem.check_expiry(tpe(2026, 10, 19, 10, 0), [pos()], ALWAYS_OPEN)
    assert rec.calls[1]["key"] == c["key"]


def test_options_only_message_has_no_restart_advice(rec):
    rem.check_expiry(tpe(2026, 10, 21, 9, 0), [pos("TXO49800J6", "Buy", 1)], ALWAYS_OPEN)
    c = rec.calls[0]
    assert c["key"] == "expiry:2026-10-21:today" and "重啟後端" not in c["message"] and "最後交易日" in c["title"]


def test_nothing_to_say(rec):
    assert rem.check_expiry(tpe(2026, 10, 15, 10, 0), [pos()], ALWAYS_OPEN) == []                 # 還早
    assert rem.check_expiry(tpe(2026, 10, 21, 3, 0), [pos()], ALWAYS_OPEN) == []                  # 凌晨沒有時間點
    assert rem.check_expiry(tpe(2026, 10, 21, 12, 40), [], ALWAYS_OPEN) == []
    assert rem.check_expiry(tpe(2026, 10, 21, 12, 40), [pos("TMFK6")], ALWAYS_OPEN) == []         # 下個月的合約
    assert rec.calls == []


# ── 迴圈整合 ─────────────────────────────────────────────────

def _drive(monkeypatch, positions):
    """跑一輪 reminder_loop（第二次 sleep 時取消）；回傳 check_expiry 的呼叫。"""
    calls = []
    sleeps = {"n": 0}

    async def fake_sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(rem.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(rem, "check_expiry", lambda now, p, is_open: calls.append((now, p, is_open)) or [])
    monkeypatch.setattr(rem, "check", lambda *a, **k: None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rem.reminder_loop(lambda: positions, lambda: []))
    return calls


def test_loop_checks_expiry_only_with_positions(monkeypatch):
    assert _drive(monkeypatch, []) == []
    calls = _drive(monkeypatch, [pos()])
    assert len(calls) == 1 and calls[0][1] == [pos()] and calls[0][2] is rem._is_open


def test_loop_survives_an_expiry_error(monkeypatch, caplog):
    sleeps = {"n": 0}

    async def fake_sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] > 2:
            raise asyncio.CancelledError

    def boom(*a):
        raise RuntimeError("calendar broke")

    monkeypatch.setattr(rem.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(rem, "check_expiry", boom)
    monkeypatch.setattr(rem, "check", lambda *a, **k: None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rem.reminder_loop(lambda: [pos()], lambda: []))
    assert sleeps["n"] == 3                                      # 第一輪的例外被吞掉，迴圈照跑


def test_calendar_helper_uses_the_shared_calendar(monkeypatch):
    class Cal:
        def check(self, d):
            return {"open": d.day != 21}

    monkeypatch.setattr(rem, "_calendar", Cal())
    assert rem._is_open(date(2026, 10, 20)) is True and rem._is_open(date(2026, 10, 21)) is False
