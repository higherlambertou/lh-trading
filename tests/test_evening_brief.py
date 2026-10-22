"""傍晚摘要（發現 68）：組訊息（夜盤、下一個日盤、今天實際 vs 預期、事件、命中率）、到點才發、等臺指 VIX、一天一則、休市與模擬盤不發、不放權益。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from core import evening_brief as eb
from core.daytrade import TAIPEI


def tpe(*a):
    return datetime(*a, tzinfo=TAIPEI)


NIGHT = {"expect": {"lo": 599.5, "mid": 898.9, "hi": 1363.5}, "r20n": 683.6}
NEXT = {"expect": {"lo": 497.0, "mid": 693.4, "hi": 1016.9}, "r20": 579.8}
CPI = {"when": "今晚 20:30", "label": "美國 CPI", "note": "歷史上隔天日盤開盤跳空約平常的 1.6 倍"}


def data(**kw):
    d = {"date": "2026-10-22", "evening": {"prev_day": "2026-10-22", "night": NIGHT, "next_day": NEXT}, "events": [],
         "today": {"lo": 500.0, "mid": 700.0, "hi": 1000.0, "actual": 1120.0},
         "hits": {"day": {"n": 20, "inside": 14}, "night": {"n": 3, "inside": 2}, "min_show": 5}}
    d.update(kw)
    return d


def test_compose():
    title, body = eb.compose(data(events=[CPI]))
    assert title == "🌆 傍晚摘要 10/22（四）"
    assert "🌙 今晚夜盤預期振幅約 899 點（68% 區間 600～1,364；近 20 個夜盤平均 684）" in body
    assert "下一個日盤預期振幅約 693 點（68% 區間 497～1,017）" in body
    assert "今天日盤實際走了 1,120 點；早上預期約 700 點（68% 區間 500～1,000）→ 比區間大" in body
    assert "📅 今晚 20:30 美國 CPI" in body and "預測驗證：日盤近 20 天有 14 天落在區間內（模型說應約 68%）" in body
    assert "夜盤近" not in body                                                           # 夜盤才 3 晚：不夠
    assert "權益" not in body and "不是方向訊號" in body


def test_compose_missing_pieces():
    _, body = eb.compose(data(evening={"night": {"note": "等 10-22 收盤的臺指選擇權波動率指數"}, "next_day": None}, today=None, events=None, hits={}))
    assert "🌙 今晚夜盤預期振幅：等 10-22 收盤的臺指選擇權波動率指數" in body and "下一個日盤" not in body
    assert "今天日盤實際" not in body and "事件：讀不到" in body and "預測驗證" not in body
    _, b2 = eb.compose(data(evening={"error": "RuntimeError"}, events=[]))
    assert "今晚夜盤預期振幅：RuntimeError" in b2 and "36 小時內沒有美國重要事件" in b2
    _, b3 = eb.compose(data(today={"lo": 500.0, "mid": 700.0, "hi": 1000.0, "actual": 650.0}))
    assert "→ 落在區間內" in b3


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(eb, "notify", lambda title, body, **k: out.append((title, body, k)) or True)
    monkeypatch.setattr(eb, "sim_blocked", lambda: False)
    monkeypatch.delenv("EVENING_BRIEF_TIME", raising=False)
    return out


def run(b, now, d, is_open=lambda x: True):
    return asyncio.run(b.run_once(now, lambda ts: d, is_open))


def test_timing_waits_for_vix_and_sends_once(sent):
    b = eb.EveningBrief()
    waiting = data(evening={"night": {"note": "等 10-22 收盤的臺指選擇權波動率指數"}, "next_day": None})
    assert run(b, tpe(2026, 10, 22, 16, 19), data()) is None
    assert run(b, tpe(2026, 10, 22, 16, 20), waiting) == "waiting" and sent == []
    assert run(b, tpe(2026, 10, 22, 16, 30), data()) == "sent" and len(sent) == 1 and sent[0][2]["key"] == "evening:2026-10-22"
    assert run(b, tpe(2026, 10, 22, 18, 0), data()) is None and len(sent) == 1           # 一天一則
    b2 = eb.EveningBrief()
    assert run(b2, tpe(2026, 10, 23, 17, 30), waiting) == "sent" and "等 10-22" in sent[-1][1]      # 17:30 還沒有：照發、寫原因
    assert run(eb.EveningBrief(), tpe(2026, 10, 23, 21, 0), data()) is None              # 21:00 以後不補


def test_closed_days_weekends_and_sim(sent, monkeypatch):
    assert run(eb.EveningBrief(), tpe(2026, 10, 22, 16, 30), data(), is_open=lambda x: False) == "closed"
    assert run(eb.EveningBrief(), tpe(2026, 10, 24, 16, 30), data()) is None             # 週六
    monkeypatch.setattr(eb, "sim_blocked", lambda: True)
    assert run(eb.EveningBrief(), tpe(2026, 10, 22, 16, 30), data()) == "sim" and sent == []


def test_time_env(monkeypatch):
    for raw, m in [("1700", 1020), ("abc", 980), ("", 980), ("2575", 980)]:
        monkeypatch.setenv("EVENING_BRIEF_TIME", raw)
        assert eb.brief_minute() == m, raw


def test_today_vs_forecast(tmp_path):
    from core import forecast_log as fl
    p = tmp_path / "m.db"
    assert eb.today_vs_forecast(p, "2026-10-22", []) is None
    fl.log_forecast(p, "day", "2026-10-22", {"lo": 500.0, "mid": 700.0, "hi": 1000.0}, {})
    bars = [{"date": "2026-10-22", "high": 47100.0, "low": 46000.0}] * 300
    assert eb.today_vs_forecast(p, "2026-10-22", bars) == {"lo": 500.0, "mid": 700.0, "hi": 1000.0, "actual": 1100.0}
    assert eb.today_vs_forecast(p, "2026-10-22", bars[:10])["actual"] is None


def test_loop_off(monkeypatch):
    monkeypatch.setenv("EVENING_BRIEF", "false")
    asyncio.run(asyncio.wait_for(eb.evening_loop(None, lambda d: True), 1))
