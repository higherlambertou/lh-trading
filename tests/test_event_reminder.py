"""CPI 提醒（發現 52）：只在『公布當天 12:30～公布前』、有會留到晚上的持倉時提醒一次；讀不到事件就不提醒；只通知、不碰下單。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import core.event_reminder as er
import core.intel_reader as ir
from core import daytrade
from intel_fixture import event, make_db

TPE = er.TAIPEI
ROOT = Path(er.__file__).resolve().parent.parent


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TPE).timestamp()


CPI_T = at(2026, 11, 10, 21, 30)                                                   # 冬令：美東 08:30 ＝ 台北 21:30
CPI = {"uid": "bls-cpi-nov", "source": "bls", "title_en": "Consumer Price Index", "scheduled_time": CPI_T, "importance": 3}


def pos(code="TMFK6", direction="Buy", q=1):
    return {"code": code, "direction": direction, "quantity": q}


# ── 時間窗 ───────────────────────────────────────────────────────

def test_due_event_window_is_release_day_1230_until_release():
    day = (2026, 11, 10)
    assert er.due_event(at(*day, 12, 30), [CPI]) is CPI
    assert er.due_event(at(*day, 12, 29, 59), [CPI]) is None                       # 12:30 之前不提醒
    assert er.due_event(at(*day, 21, 29, 59), [CPI]) is CPI
    assert er.due_event(at(*day, 21, 30), [CPI]) is None                           # 公布時間到了就不提醒（已經公布）
    assert er.due_event(at(2026, 11, 9, 20, 0), [CPI]) is None                     # 前一天不提醒
    assert er.due_event(at(2026, 11, 11, 13, 0), [CPI]) is None                    # 隔天不提醒
    assert er.due_event(at(*day, 13, 0), []) is None and er.due_event(at(*day, 13, 0), None) is None


def test_due_event_only_for_the_cpi_with_a_time_and_picks_the_right_one():
    t = at(2026, 11, 10, 14, 0)
    others = [{**CPI, "source": "fred"}, {**CPI, "title_en": "Employment Situation"}, {**CPI, "scheduled_time": None}, {**CPI, "title_en": "Producer Price Index"}]
    assert er.due_event(t, others) is None
    later = {**CPI, "uid": "bls-cpi-dec", "scheduled_time": at(2026, 12, 10, 21, 30)}
    assert er.due_event(t, [later, *others, CPI]) is CPI
    summer = {**CPI, "uid": "x", "scheduled_time": at(2026, 7, 14, 20, 30)}        # 夏令是 20:30
    assert er.due_event(at(2026, 7, 14, 20, 29), [summer]) is summer and er.due_event(at(2026, 7, 14, 20, 30), [summer]) is None


# ── 哪些持倉算 ───────────────────────────────────────────────────

def test_carried_skips_zero_blank_and_daytrade_positions():
    now = datetime.fromtimestamp(at(2026, 11, 10, 13, 0), TPE)
    daytrade._MARKS.clear()
    try:
        daytrade.note_open("TXF", 1)                                               # 登記了當沖開倉（日期＝今天，用真的 taipei_now 判斷，所以用實際今天）
        today = daytrade.taipei_now()
        items = er.carried([pos("TXFK6", "Buy", 1), pos("TMFK6", "Sell", 2), pos("TMFK6", "Buy", 0), pos("", "Buy", 1)], [], today)
        assert [(i["code"], i["direction"], i["quantity"]) for i in items] == [("TMFK6", "Sell", 2)]                  # 當沖的大台不算、0 口與空代碼不算
    finally:
        daytrade._MARKS.clear()
    assert [i["code"] for i in er.carried([pos("TXFK6")], [], now)] == ["TXFK6"]                                   # 沒登記當沖就算
    assert er.carried([], [], now) == [] and er.carried(None, None, now) == []


# ── 訊息 ─────────────────────────────────────────────────────────

def test_message_has_the_measured_facts_and_no_equity():
    title, msg = er.message(CPI, [{"code": "TMFK6", "direction": "Buy", "quantity": 2}, {"code": "MXFK6", "direction": "Sell", "quantity": 1}])
    assert title == "📅 今晚 21:30 美國 CPI，你還有持倉"
    assert "TMFK6 多 2 口、MXFK6 空 1 口" in msg and "61 次" in msg and "154 點" in msg and "100 點" in msg and "約 1.6 倍" in msg and "振幅沒有明顯變大" in msg and "不是方向預測" in msg
    assert "權益" not in msg and "權益" not in title and "保證金" not in msg
    summer = er.message({**CPI, "scheduled_time": at(2026, 7, 14, 20, 30)}, [pos()])[0]
    assert "今晚 20:30" in summer


def test_stat_matches_the_registered_study_result_when_it_is_on_disk():
    p = ROOT / "data" / "event_day_result.json"
    if not p.exists():
        pytest.skip("本機沒有 data/event_day_result.json")
    r = json.loads(p.read_text(encoding="utf-8"))
    pooled, d = r["tests"]["CPI/gap"]["pooled"], r["descriptive"]["CPI"]["gap"]
    rng = r["tests"]["CPI/range"]["pooled"]
    assert r["tests"]["CPI/gap"]["confirmed"] is True and r["tests"]["CPI/range"]["confirmed"] is False
    assert (er.STAT["n"], round(pooled["amp"], 2), round(pooled["amp_lo"], 2), round(pooled["amp_hi"], 2), round(rng["amp"], 2)) == (d["n"], er.STAT["amp"], er.STAT["lo"], er.STAT["hi"], er.STAT["range_amp"])
    assert (round(d["points_median_event"]), round(d["points_median_ctrl"])) == (er.STAT["median_event"], er.STAT["median_ctrl"])


# ── check ────────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(er, "notify", lambda title, msg, **kw: out.append((title, msg, kw)) or True)
    return out


def test_check_notifies_once_per_event_with_a_stable_key(sent):
    calls = []
    get = lambda t: calls.append(t) or [CPI]
    now = at(2026, 11, 10, 15, 0)
    assert er.check(now, [pos("TMFK6", "Buy", 1)], [], get) is CPI
    assert len(sent) == 1 and sent[0][2]["key"] == "cpi:bls-cpi-nov" and sent[0][2]["cooldown"] >= 86400 and sent[0][2]["priority"] == "high" and "TMFK6 多 1 口" in sent[0][1]
    er.check(now + 600, [pos("TMFK6", "Buy", 1)], [], get)
    assert sent[1][2]["key"] == sent[0][2]["key"]                                  # 同一個事件永遠同一個 key（冷卻由 notify 擋）


def test_check_does_not_touch_the_database_when_there_is_nothing_to_remind(sent):
    calls = []
    get = lambda t: calls.append(t) or [CPI]
    assert er.check(at(2026, 11, 10, 15, 0), [], [], get) is None                 # 沒有持倉
    assert er.check(at(2026, 11, 10, 12, 29), [pos()], [], get) is None           # 還沒到 12:30
    assert er.check(at(2026, 11, 10, 15, 0), [pos("TMFK6", "Buy", 0)], [], get) is None
    assert calls == [] and sent == []


def test_check_is_silent_when_events_are_unavailable_or_not_due(sent):
    now = at(2026, 11, 10, 15, 0)
    assert er.check(now, [pos()], [], lambda t: None) is None and er.check(now, [pos()], [], lambda t: []) is None
    assert er.check(at(2026, 11, 9, 15, 0), [pos()], [], lambda t: [CPI]) is None                   # 前一天
    assert er.check(at(2026, 11, 10, 21, 31), [pos()], [], lambda t: [CPI]) is None                 # 已經公布
    assert sent == []


def test_check_skips_daytrade_only_positions(sent):
    daytrade._MARKS.clear()
    try:
        daytrade.note_open("TXF", 1)
        now = daytrade.taipei_now()
        # 今天的日期 12:30 之後的某一刻，造一個『今天公布』的 CPI
        t0 = now.replace(hour=15, minute=0, second=0, microsecond=0).timestamp()
        cpi_today = {**CPI, "scheduled_time": now.replace(hour=21, minute=30, second=0, microsecond=0).timestamp()}
        assert er.check(t0, [pos("TXFK6", "Buy", 1)], [], lambda t: [cpi_today]) is None and sent == []
        assert er.check(t0, [pos("TXFK6", "Buy", 1), pos("TMFK6", "Buy", 1)], [], lambda t: [cpi_today]) is cpi_today
        assert "TMFK6" in sent[0][1] and "TXFK6" not in sent[0][1]
    finally:
        daytrade._MARKS.clear()


# ── intel_reader.upcoming_events ─────────────────────────────────

NOW = at(2026, 11, 10, 15, 0)


def seed(tmp_path, **kw):
    c = make_db(tmp_path / "intel.db", **kw)
    event(c, "bls-cpi", "Consumer Price Index", when=CPI_T, date="2026-11-10", importance=3, source="bls", detail={"zh": "美國消費者物價指數 CPI"})
    event(c, "bls-old", "Consumer Price Index", when=NOW - 3600, date="2026-11-10", importance=3, source="bls")                  # 已經過去
    event(c, "bls-far", "Consumer Price Index", when=NOW + 5 * 86400, date="2026-11-15", importance=3, source="bls")              # 2 天以外
    event(c, "bls-low", "Real Earnings", when=CPI_T, date="2026-11-10", importance=1, source="bls")
    c.close()
    return tmp_path / "intel.db"


def test_upcoming_events_returns_the_future_rows_the_panel_would_show(tmp_path):
    ev = ir.upcoming_events(NOW, seed(tmp_path))
    assert [e["uid"] for e in ev] == ["bls-cpi"] and ev[0]["source"] == "bls" and ev[0]["title_en"] == "Consumer Price Index" and ev[0]["scheduled_time"] == CPI_T
    assert [e["uid"] for e in ir.upcoming_events(NOW, tmp_path / "intel.db", ahead_days=7)] == ["bls-cpi", "bls-far"]
    assert er.due_event(NOW, ev)["uid"] == "bls-cpi"


def test_upcoming_events_never_raises_and_returns_none(tmp_path):
    assert ir.upcoming_events(NOW, tmp_path / "nope.db") is None
    new = tmp_path / "b"
    new.mkdir()
    c = make_db(new / "intel.db", version=ir.SUPPORTED_SCHEMA + 1)
    c.close()
    assert ir.upcoming_events(NOW, new / "intel.db") is None                       # 版本比認得的新
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 50)
    assert ir.upcoming_events(NOW, bad) is None
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert ir.upcoming_events(NOW, empty) is None                                  # 沒有 schema_meta


def test_upcoming_events_is_read_only(tmp_path):
    p = seed(tmp_path)
    before = p.read_bytes()
    ir.upcoming_events(NOW, p)
    assert p.read_bytes() == before


# ── 迴圈 ─────────────────────────────────────────────────────────

def test_loop_is_off_when_disabled_and_survives_errors(monkeypatch):
    monkeypatch.setenv("EVENT_REMINDER", "false")
    assert er.enabled() is False
    asyncio.run(asyncio.wait_for(er.event_reminder_loop(lambda: [pos()], lambda: []), 1))                    # 直接結束
    monkeypatch.delenv("EVENT_REMINDER")
    assert er.enabled() is True
    monkeypatch.setattr(er, "POLL_SEC", 0.01)
    n = {"c": 0}

    def boom(*a, **k):
        n["c"] += 1
        raise RuntimeError("壞了")
    monkeypatch.setattr(er, "check", boom)

    async def run():
        t = asyncio.create_task(er.event_reminder_loop(lambda: [pos()], lambda: []))
        await asyncio.sleep(0.15)
        assert not t.done()                                                        # 例外被吞掉，迴圈還活著
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(run())
    assert 2 <= n["c"] <= 40                                                       # 有睡覺（POLL_SEC），不是空轉


def test_loop_skips_the_database_when_flat(monkeypatch):
    monkeypatch.setattr(er, "POLL_SEC", 0.01)
    called = []
    monkeypatch.setattr(er, "check", lambda *a, **k: called.append(1))

    async def run():
        t = asyncio.create_task(er.event_reminder_loop(lambda: [], lambda: []))
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(run())
    assert called == []


# ── 只通知、不碰下單 ─────────────────────────────────────────────

def test_event_reminder_is_wired_in_main_and_not_used_by_trading_paths():
    assert "event_reminder_loop" in (ROOT / "main.py").read_text(encoding="utf-8")
    paths = list((ROOT / "strategies").glob("*.py")) + [ROOT / "core" / n for n in ("broker.py", "manual_monitor.py", "protection.py", "quote_hub.py", "shioaji_worker.py", "trade_log.py", "entry_log.py")] \
        + [ROOT / "api" / n for n in ("routes_order.py", "routes_strategy.py", "routes_position.py", "routes_quote.py")] + list((ROOT / "core").glob("*risk*.py"))
    for p in paths:
        if p.exists():
            assert "event_reminder" not in p.read_text(encoding="utf-8"), p
