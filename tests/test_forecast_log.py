"""預測紀錄與命中率（發現 68）：存了不改（觸發器）、實際值（日盤 bars_1m、夜盤期交所盤後列）、命中率、什麼時候記與補、模擬盤不記、交易路徑不讀。"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from core import forecast_log as fl
from core.daytrade import TAIPEI

ROOT = Path(fl.__file__).resolve().parent.parent
E3 = {"lo": 500.0, "mid": 700.0, "hi": 1000.0}
HEADER = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"


def tpe(*a):
    return datetime(*a, tzinfo=TAIPEI)


def test_log_is_append_only(tmp_path):
    p = tmp_path / "m.db"
    assert fl.log_forecast(p, "day", "2026-10-22", E3, {"vix": 30})
    assert not fl.log_forecast(p, "day", "2026-10-22", {"lo": 1, "mid": 2, "hi": 3}, {})              # 第一筆為準
    assert not fl.log_forecast(p, "day", "2026-10-23", {"lo": 1}, {})                                  # 不完整不存
    with sqlite3.connect(p) as c:
        assert c.execute("SELECT mid FROM forecast_log WHERE target='2026-10-22'").fetchone()[0] == 700.0
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE forecast_log SET mid=1")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM forecast_log")
    assert fl.pending(p) == [("day", "2026-10-22")]
    fl.save_outcome(p, "day", "2026-10-22", 650.0, "bars_1m")
    assert fl.pending(p) == []


def test_realized_day():
    bars = [{"date": "2026-10-22", "high": 100 + i % 7, "low": 90 - i % 5} for i in range(300)] + [{"date": "2026-10-23", "high": 999, "low": 1}]
    assert fl.realized_day(bars, "2026-10-22") == 106 - 86
    assert fl.realized_day(bars[:200], "2026-10-22") is None                            # 不完整不算


def row(d, code, h, l, vol, sess):
    return f"{d},TX,{code}  ,100,{h},{l},101,1,1%,{vol},101,10,100,101,200,50,,{sess},"


def test_realized_night(tmp_path):
    lines = [HEADER, row("2026/10/22", "202611", 900, 800, 50, "盤後"),                  # 10/22 的盤後＝10/21 晚上：不是要的
             row("2026/10/23", "202612", 2000, 100, 10, "盤後"), row("2026/10/23", "202611", 1500, 1000, 900, "盤後"),        # 成交量小的排前面：也要取大的
             row("2026/10/23", "202611", 3000, 1, 5000, "一般"), row("2026/10/26", "202611", 700, 600, 900, "盤後")]
    (tmp_path / "tx_2026_10.csv").write_bytes(("\n".join(lines) + "\n").encode("cp950"))
    assert fl.realized_night(tmp_path, "2026-10-22") == 500.0                             # 10/22 晚上的夜盤＝10/23 那列、成交量最大的合約
    assert fl.realized_night(tmp_path, "2026-10-23") == 100.0                             # 週五晚上＝下週一那列
    assert fl.realized_night(tmp_path, "2026-10-26") is None                              # 還沒有
    assert fl.realized_night(tmp_path / "none", "2026-10-22") is None


def test_hit_rate_and_summary(tmp_path):
    rows = [(500, 700, 1000, r) for r in (650, 1200, 400, 900, 700)]
    h = fl.hit_rate(rows)
    assert h == {"n": 5, "inside": 3, "rate": 0.6, "ratio_median": 1.0}
    assert fl.hit_rate(rows, n=2) == {"n": 2, "inside": 2, "rate": 1.0, "ratio_median": 1.143}                 # 只看最近 2 筆（900、700）
    assert fl.hit_rate([])["rate"] is None
    assert fl.hit_rate([(500, 700, 1000, 500), (500, 700, 1000, 1000)])["inside"] == 2           # 剛好等於上下限算在內
    p = tmp_path / "m.db"
    for i, r in enumerate((650, 1200, 400)):
        fl.log_forecast(p, "night", f"2026-10-{20 + i}", E3, {})
        fl.save_outcome(p, "night", f"2026-10-{20 + i}", r, "taifex_tx")
    s = fl.summary(p)
    assert s["night"]["n"] == 3 and s["night"]["inside"] == 1 and s["day"]["n"] == 0 and s["min_show"] == fl.MIN_SHOW


def test_tick_logs_in_the_right_windows_and_fills_outcomes(tmp_path):
    p = tmp_path / "m.db"
    day = {"expect": E3, "r20": 600.0, "vix": 30.0, "vix_date": "2026-10-21"}
    ev = {"prev_day": "2026-10-22", "night": {"expect": {"lo": 600.0, "mid": 900.0, "hi": 1360.0}, "r20n": 684.0, "vix": 30.3, "vix_date": "2026-10-22", "day_range": 1120.0}}
    async def get_day(today):
        return day
    bars = {"2026-10-22": [{"date": "2026-10-22", "high": 1000.0, "low": 400.0}] * 300}
    calls = []
    r = asyncio.run(fl.tick(p, tpe(2026, 10, 22, 9, 0), get_day, lambda now: calls.append(now) or ev, lambda t: bars.get(t, [])))
    assert r["day"] is True and "night" not in r and calls == []                          # 日盤時段只記日盤
    r = asyncio.run(fl.tick(p, tpe(2026, 10, 22, 13, 50), get_day, lambda now: ev, lambda t: bars.get(t, [])))
    assert "day" not in r and r["filled"] == 0                                            # 14:00 之前不補今天的實際值
    r = asyncio.run(fl.tick(p, tpe(2026, 10, 22, 16, 30), get_day, lambda now: ev, lambda t: bars.get(t, [])))
    assert r["night"] is True and r["filled"] == 1                                        # 今天日盤的實際值補上了
    with sqlite3.connect(p) as c:
        assert c.execute("SELECT realized FROM forecast_outcome WHERE kind='day'").fetchone()[0] == 600.0
    asyncio.run(fl.tick(p, tpe(2026, 10, 24, 10, 0), get_day, lambda now: ev, lambda t: []))          # 週六：不記日盤
    assert fl.pending(p) == [("night", "2026-10-22")]                                      # 夜盤要等期交所資料


def test_not_in_simulation(monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    assert not fl.enabled()
    asyncio.run(asyncio.wait_for(fl.forecast_loop(Path("/nonexistent"), None, None, None), 1))
    monkeypatch.setenv("SIMULATION", "false")
    assert fl.enabled()


def test_trading_paths_do_not_read_it():
    files = list((ROOT / "strategies").glob("*.py")) + [ROOT / "core" / n for n in ("broker.py", "manual_monitor.py", "quote_hub.py", "risk.py", "shioaji_worker.py")] \
        + [ROOT / "api" / n for n in ("routes_order.py", "routes_strategy.py", "routes_risk.py")]
    for f in files:
        if f.exists():
            assert "forecast_log" not in f.read_text(encoding="utf-8"), f
