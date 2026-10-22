"""下單前檢查（發現 60）：事件篩選與說法、三塊各自壞掉時只寫原因、API、只顯示（交易路徑不引用）。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from core import event_reminder as er
from core import precheck as pc

TPE = er.TAIPEI
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=TPE).timestamp()


def ev(kind, t, uid="u"):
    src, title = {"CPI": ("bls", "Consumer Price Index"), "NFP": ("bls", "Employment Situation"), "FOMC": ("fed", "FOMC 利率決策")}[kind]
    return {"uid": uid, "source": src, "title_en": title, "scheduled_time": t, "local_date": "2026-11-10"}


def test_upcoming_major_filters_window_kinds_and_sorts():
    cpi = ev("CPI", datetime(2026, 11, 10, 21, 30, tzinfo=TPE).timestamp())
    fomc = ev("FOMC", datetime(2026, 11, 11, 3, 0, tzinfo=TPE).timestamp())
    far = ev("NFP", NOW + 40 * 3600)
    past = ev("CPI", NOW - 60)
    other = {**cpi, "title_en": "Real Earnings"}
    out = pc.upcoming_major(NOW, [fomc, far, past, other, cpi])
    assert [x["kind"] for x in out] == ["CPI", "FOMC"]                                       # 36 小時內、已過的與其他事件不列、依時間排
    assert out[0]["when"] == "今晚 21:30" and "1.6 倍" in out[0]["note"] and out[1]["when"] == "明天凌晨 03:00" and "沒有確認" in out[1]["note"]
    assert pc.upcoming_major(NOW, None) is None and pc.upcoming_major(NOW, []) == []
    assert [x["kind"] for x in pc.upcoming_major(NOW, [far], hours=48)] == ["NFP"]


def test_build_reports_each_block_or_its_reason():
    day = {"expect": {"lo": 489.0, "mid": 682.0, "hi": 1000.0}, "r20": 542.0}
    state = {"ready": True, "iv": {"value": 27.1, "percentile": 53.6, "label": "正常", "history_n": 252, "note": ""}}
    b = pc.build({"day_iv": day}, state, [], NOW)
    assert b["day_range"] == day and b["iv"]["percentile"] == 53.6 and b["events"] == [] and b["events_note"] is None and b["as_of"] == NOW
    empty = pc.build(None, {"ready": False}, None, NOW)
    assert "還沒有" in empty["day_range"]["note"] and empty["iv"] is None and empty["events"] is None and "lh-intel" in empty["events_note"]


def test_fetch_events_never_raises():
    assert pc.fetch_events(NOW, lambda t: (_ for _ in ()).throw(RuntimeError("x"))) is None
    assert pc.fetch_events(NOW, lambda t: None) is None
    assert [x["kind"] for x in pc.fetch_events(NOW, lambda t: [ev("CPI", NOW + 3600)])] == ["CPI"]


def test_api_route(monkeypatch):
    import api.routes_market as rm

    async def live():
        return {"ctx": {"day_iv": {"note": "x"}}}
    monkeypatch.setattr(rm.market_state, "live_snapshot", live)
    monkeypatch.setattr(rm.market_state, "snapshot", lambda: {"ready": False})
    monkeypatch.setattr(rm.precheck, "fetch_events", lambda now: [])
    out = asyncio.run(rm.get_precheck())
    assert out["day_range"] == {"note": "x"} and out["iv"] is None and out["events"] == []

    async def boom():
        raise RuntimeError("壞了")
    monkeypatch.setattr(rm.market_state, "live_snapshot", boom)
    assert "還沒有" in asyncio.run(rm.get_precheck())["day_range"]["note"]               # 一塊壞了不影響其他
    import main
    assert any(getattr(r, "path", "") == "/api/market/precheck" for r in main.app.routes)


def test_precheck_is_display_only():
    root = Path(pc.__file__).resolve().parent.parent
    for p in list((root / "strategies").glob("*.py")) + [root / "core" / n for n in ("broker.py", "manual_monitor.py", "protection.py")] + [root / "api" / n for n in ("routes_order.py", "routes_strategy.py")]:
        assert "precheck" not in p.read_text(encoding="utf-8"), p
