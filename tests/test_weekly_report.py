"""週報（發現 63）：要報哪一週、整理數字（日期邊界、費用另列、賺賠）、訊息不放權益、到點才發一週一則、失敗會重試、模擬盤不發、不接進交易路徑。"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from core import weekly_report as wr
from core.daytrade import TAIPEI

ROOT = Path(wr.__file__).resolve().parent.parent


def tpe(*a):
    return datetime(*a, tzinfo=TAIPEI)


def test_week_of():
    assert wr.week_of(tpe(2026, 10, 24, 10, 0)) == (date(2026, 10, 19), date(2026, 10, 24))      # 週六：這週
    assert wr.week_of(tpe(2026, 10, 25, 23, 0)) == (date(2026, 10, 19), date(2026, 10, 24))      # 週日：這週
    assert wr.week_of(tpe(2026, 10, 22, 9, 0)) == (date(2026, 10, 12), date(2026, 10, 17))       # 週四：上週
    assert wr.week_of(tpe(2026, 10, 19, 0, 1)) == (date(2026, 10, 12), date(2026, 10, 17))       # 週一：上週


ROWS = [
    {"id": 1, "code": "TMFK6", "quantity": 1, "pnl": 1200.0, "fee": 20, "tax": 5, "date": "20261019"},
    {"id": 2, "code": "TMFK6", "quantity": 2, "pnl": -800.0, "fee": 40, "tax": 9, "date": "2026-10-20"},
    {"id": 3, "code": "MXFK6", "quantity": 1, "pnl": -2500.0, "fee": 30, "tax": 12, "date": "20261024"},     # 週六（週五夜盤）
    {"id": 4, "code": "TMFK6", "quantity": 1, "pnl": 0.0, "fee": 20, "tax": 4, "date": "20261020"},
    {"id": 5, "code": "TMFK6", "quantity": 1, "pnl": 9999.0, "fee": 1, "tax": 1, "date": "20261018"},        # 上週日：不算
    {"id": 6, "code": "TMFK6", "quantity": 1, "pnl": 9999.0, "fee": 1, "tax": 1, "date": "20261026"},        # 下週：不算
    {"id": 7, "code": "TMFK6", "quantity": 1, "pnl": 9999.0, "fee": 1, "tax": 1, "date": ""},                # 沒日期：不算
]
MON, SAT = date(2026, 10, 19), date(2026, 10, 24)


def test_summarize_numbers():
    s = wr.summarize(ROWS, MON, SAT, {"stop_loss": 2})
    assert s["n"] == 4 and s["qty"] == 5
    assert s["pnl"] == -2100 and s["cost"] == 140 and s["net"] == -2240
    assert (s["wins"], s["losses"], s["flat"]) == (1, 2, 1)
    assert s["avg_win"] == 1200 and s["avg_loss"] == -1650
    assert s["worst"]["pnl"] == -2500 and s["worst"]["code"] == "MXFK6"
    assert s["by_code"]["TMFK6"] == {"n": 3, "qty": 4, "pnl": 400.0, "cost": 98.0}
    assert list(s["by_day"]) == ["2026-10-19", "2026-10-20", "2026-10-24"] and s["by_day"]["2026-10-20"]["pnl"] == -800


def test_summarize_tolerates_junk():
    s = wr.summarize([{"date": "20261020", "pnl": "abc", "fee": None, "quantity": None}, {"date": "x"}], MON, SAT)
    assert s["n"] == 1 and s["pnl"] == 0 and s["avg_win"] is None and s["avg_loss"] is None and s["worst"] is None


def test_compose():
    title, body = wr.compose(wr.summarize(ROWS, MON, SAT, {"stop_loss": 2, "take_profit": 1}))
    assert title == "🗓 週報 10/19～10/24"
    assert "平倉 4 筆、5 口：損益 −NT$2,100，手續費＋稅 NT$140，扣完 −NT$2,240" in body
    assert "賺 1 筆（平均 +NT$1,200）、賠 2 筆（平均 −NT$1,650）、持平 1 筆" in body
    assert "最大一筆虧損：MXFK6 1 口 −NT$2,500（10/24）" in body
    assert "各合約：MXFK6 1 筆 −NT$2,500；TMFK6 3 筆 +NT$400" in body
    assert "10/24（六）−NT$2,500" in body
    assert "停損監控觸發：停損 2 次、停利 1 次" in body
    assert "權益" not in body and "帳號" not in body
    _, empty = wr.compose(wr.summarize([], MON, SAT))
    assert "沒有平倉紀錄" in empty


def test_single_contract_has_no_per_contract_line():
    _, body = wr.compose(wr.summarize(ROWS[:2], MON, SAT))
    assert "各合約" not in body and "停損監控" not in body


def test_stop_counts_reads_only_this_week_manual_monitor(tmp_path):
    p = tmp_path / "t.db"
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE exit_fills(mode TEXT, strategy_name TEXT, exit_ts REAL, exit_reason TEXT)")
        ts = lambda *a: tpe(*a).timestamp()                                       # noqa: E731
        c.executemany("INSERT INTO exit_fills VALUES (?,?,?,?)", [
            ("live", "manual_monitor", ts(2026, 10, 19, 0, 0), "stop_loss"),
            ("live", "manual_monitor", ts(2026, 10, 24, 23, 59), "take_profit"),
            ("live", "manual_monitor", ts(2026, 10, 25, 0, 0), "stop_loss"),       # 週日：不算
            ("live", "manual_monitor", ts(2026, 10, 18, 23, 59), "stop_loss"),     # 上週：不算
            ("sim", "manual_monitor", ts(2026, 10, 20, 9, 0), "stop_loss"),        # 模擬盤：不算
            ("live", "scalp", ts(2026, 10, 20, 9, 0), "stop_loss"),                # 策略：不算
        ])
    assert wr.stop_counts(p, "live", MON, SAT) == {"stop_loss": 1, "take_profit": 1}
    assert wr.stop_counts(tmp_path / "none.db", "live", MON, SAT) == {}


@pytest.fixture
def rec(monkeypatch):
    sent = []
    monkeypatch.setattr(wr, "notify", lambda title, body, **k: sent.append((title, body, k)) or True)
    monkeypatch.setattr(wr, "sim_blocked", lambda: False)
    monkeypatch.delenv("WEEKLY_REPORT_TIME", raising=False)
    return sent


def fetcher(rows=ROWS, boom=False, calls=None):
    async def f(b, e):
        if calls is not None:
            calls.append((b, e))
        if boom:
            raise TimeoutError("worker")
        return rows
    return f


def run(r, now, f, stops=None):
    return asyncio.run(r.run_once(now, f, stops))


def test_sends_once_on_saturday_after_ten(rec):
    r, calls = wr.WeeklyReport(), []
    assert run(r, tpe(2026, 10, 24, 9, 59), fetcher(calls=calls)) is None and calls == []
    assert run(r, tpe(2026, 10, 23, 23, 0), fetcher(calls=calls)) is None                 # 週五
    assert run(r, tpe(2026, 10, 24, 10, 0), fetcher(calls=calls), lambda m, s: {"stop_loss": 1}) == "sent"
    assert calls == [("2026-10-19", "2026-10-24")]
    assert run(r, tpe(2026, 10, 25, 12, 0), fetcher(calls=calls)) is None and len(calls) == 1
    assert len(rec) == 1 and rec[0][2]["key"] == "weekly:2026-10-19" and "停損 1 次" in rec[0][1]
    assert run(r, tpe(2026, 10, 31, 10, 5), fetcher(calls=calls)) == "sent" and calls[-1] == ("2026-10-26", "2026-10-31")


def test_late_start_on_sunday_still_sends(rec):
    assert run(wr.WeeklyReport(), tpe(2026, 10, 25, 22, 0), fetcher()) == "sent"


def test_failure_retries_after_ten_minutes(rec):
    r = wr.WeeklyReport()
    t0 = tpe(2026, 10, 24, 10, 0)
    assert run(r, t0, fetcher(boom=True)) == "failed" and rec == []
    assert run(r, tpe(2026, 10, 24, 10, 9), fetcher()) is None
    assert run(r, tpe(2026, 10, 24, 10, 10), fetcher()) == "sent" and len(rec) == 1


def test_not_in_simulation_mode(monkeypatch, rec):
    monkeypatch.setattr(wr, "sim_blocked", lambda: True)
    calls = []
    assert run(wr.WeeklyReport(), tpe(2026, 10, 24, 10, 0), fetcher(calls=calls)) == "sim" and calls == [] and rec == []


def test_custom_time(monkeypatch, rec):
    monkeypatch.setenv("WEEKLY_REPORT_TIME", "1400")
    r = wr.WeeklyReport()
    assert run(r, tpe(2026, 10, 24, 13, 59), fetcher()) is None
    assert run(r, tpe(2026, 10, 24, 14, 0), fetcher()) == "sent"
    for raw, m in [("0900", 540), ("abc", 600), ("2560", 600), ("", 600)]:
        monkeypatch.setenv("WEEKLY_REPORT_TIME", raw)
        assert wr.report_minute() == m, raw


def test_loop_off_when_disabled(monkeypatch):
    monkeypatch.setenv("WEEKLY_REPORT", "false")
    asyncio.run(asyncio.wait_for(wr.weekly_loop(fetcher()), 1))


def test_not_imported_by_trading_modules():
    for d in ("strategies",):
        for p in (ROOT / d).rglob("*.py"):
            assert "weekly_report" not in p.read_text(encoding="utf-8"), p
    for name in ("broker.py", "order_router.py", "risk.py", "manual_monitor.py", "shioaji_worker.py"):
        p = ROOT / "core" / name
        if p.exists():
            assert "weekly_report" not in p.read_text(encoding="utf-8"), p


def test_broker_passes_the_date_range_to_the_worker(monkeypatch):
    from core.broker import broker
    seen = []

    async def fake(method, timeout=8.0, **kw):
        seen.append((method, kw))
        return []
    monkeypatch.setattr(broker, "_acall", fake)
    asyncio.run(broker.list_profit_loss("2026-10-19", "2026-10-24"))
    asyncio.run(broker.list_profit_loss())
    assert seen[0] == ("list_profit_loss", {"timeout_ms": 4000, "begin": "2026-10-19", "end": "2026-10-24"})
    assert seen[1] == ("list_profit_loss", {"timeout_ms": 4000})                  # 不給日期＝當日（原本的行為）


def test_preview_endpoint_uses_cache_and_reports_failure(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from api import routes_position as rp
    from core.broker import broker
    from core.trade_log import trade_log
    monkeypatch.setattr(trade_log, "path", tmp_path / "none.db")                  # 不能讀真的 trade_log.db
    calls = []

    async def fake(b=None, e=None):
        calls.append((b, e))
        return ROWS
    monkeypatch.setattr(broker, "list_profit_loss", fake)
    monkeypatch.setattr(rp, "_weekly_cache", {"at": 0.0, "data": None})
    d = asyncio.run(rp.get_weekly())
    assert d["title"].startswith("🗓 週報") and "summary" in d and len(calls) == 1
    asyncio.run(rp.get_weekly())
    assert len(calls) == 1                                                         # 60 秒內用快取，不再查券商

    async def boom(b=None, e=None):
        raise TimeoutError
    monkeypatch.setattr(broker, "list_profit_loss", boom)
    monkeypatch.setattr(rp, "_weekly_cache", {"at": 0.0, "data": None})
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rp.get_weekly())
    assert ei.value.status_code == 503
