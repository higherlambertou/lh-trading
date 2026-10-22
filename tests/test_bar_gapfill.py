"""走勢圖缺口回補（發現 66）：什麼時候補哪一段、券商 K 棒的時間對齊（台灣時間當 UTC、標結束）、只補 flow_1m 沒有的分鐘、
一段只做一次與重試上限、資料庫、走勢圖 API 的合併（f＝1、不重複）、模擬盤不跑、研究模組不讀回補表。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from core import bar_gapfill as gf
from core.daytrade import TAIPEI
from core.market_store import MarketStore

ROOT = Path(gf.__file__).resolve().parent.parent


def tpe(*a):
    return datetime(*a, tzinfo=TAIPEI)


def naive_ns(*a) -> int:
    """券商 K 棒的時間：台灣當地時間當成 UTC 的 epoch 奈秒。"""
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1e9)


def test_windows_due():
    ws = gf.windows_due(tpe(2026, 10, 22, 13, 50))                                     # 週四收盤後：最近 3 個平日（四、三、二）已結束的段落
    assert [w["key"] for w in ws] == ["day:2026-10-22", "night:2026-10-21", "day:2026-10-21", "night:2026-10-20", "day:2026-10-20"]
    d = ws[0]
    assert d["start"] == tpe(2026, 10, 22, 8, 45).timestamp() and d["end"] == tpe(2026, 10, 22, 13, 45).timestamp()
    assert d["kb_start"] == d["kb_end"] == "2026-10-22" and d["expected"] == 300
    n = ws[1]
    assert n["start"] == tpe(2026, 10, 21, 15, 0).timestamp() and n["end"] == tpe(2026, 10, 22, 5, 0).timestamp()
    assert (n["kb_start"], n["kb_end"], n["expected"]) == ("2026-10-21", "2026-10-22", 840)
    mon = gf.windows_due(tpe(2026, 10, 26, 8, 0))                                      # 週一早上：今天的日盤還沒開、週五晚上的夜盤要看
    assert [w["key"] for w in mon][:3] == ["night:2026-10-23", "day:2026-10-23", "night:2026-10-22"]
    assert gf.windows_due(tpe(2026, 10, 22, 13, 49)) == [] and gf.windows_due(tpe(2026, 10, 22, 14, 50)) == [] and gf.windows_due(tpe(2026, 10, 22, 8, 40)) == []
    assert gf.windows_due(tpe(2026, 10, 24, 13, 55)) == []                              # 週六不跑


def test_kbar_minutes_alignment():
    raw = {"ts": [naive_ns(2026, 10, 21, 8, 46), naive_ns(2026, 10, 21, 15, 1), naive_ns(2026, 10, 21, 9, 0)],
           "open": [100, 200, 0], "high": [101, 201, 1], "low": [99, 199, 1], "close": [100.5, 200.5, 1], "volume": [5, 6, 7]}
    rows = gf.kbar_minutes(raw)
    assert rows == [(int(tpe(2026, 10, 21, 8, 45).timestamp()), 100.0, 101.0, 99.0, 100.5, 5),        # 標 08:46 結束 → 08:45 起點
                    (int(tpe(2026, 10, 21, 15, 0).timestamp()), 200.0, 201.0, 199.0, 200.5, 6)]       # 價格不完整的丟掉


def test_missing_only_fills_absent_minutes_inside_the_window():
    s = int(tpe(2026, 10, 21, 8, 45).timestamp())
    bars = [(s + 60 * k, 1.0, 1.0, 1.0, 1.0, 1) for k in range(-1, 6)]
    have = {s, s + 60}
    out = gf.missing(have, bars, s, s + 300)
    assert [b[0] for b in out] == [s + 120, s + 180, s + 240]


def full_minutes(*windows):
    out = set()
    for w in windows:
        out |= set(range(int(w["start"]), int(w["end"]), 60))
    return out


class FakeStore:
    """flow：後端記到的分鐘；回補的會記住，下一次算覆蓋率時算進去。"""
    def __init__(self, have):
        self.have, self.rows = set(have), []

    def flow_minutes(self, code, a, b):
        return {t for t in self.have if a <= t < b}

    def fill_minutes(self, code, a, b):
        return {r[0] for r in self.rows if a <= r[0] < b}

    def upsert_fill_1m(self, code, rows, at):
        self.rows += rows
        return len(rows)


def test_run_once_fills_gaps_once():
    start = int(tpe(2026, 10, 21, 8, 45).timestamp())
    others = [w for w in gf.windows_due(tpe(2026, 10, 21, 13, 55)) if w["key"] != "day:2026-10-21"]
    have = {start + 60 * k for k in range(130)} | full_minutes(*others)                # 10/21 日盤 10:55 之後都沒有；其他段落是滿的
    store, calls = FakeStore(have), []

    from datetime import timedelta
    raw_ts = []
    t = datetime(2026, 10, 21, 8, 46)                                                   # 券商的日盤 300 根：標 08:46～13:45
    for _ in range(300):
        raw_ts.append(int(t.replace(tzinfo=timezone.utc).timestamp() * 1e9))
        t += timedelta(minutes=1)

    async def kbars_full(code, a, b):
        calls.append((code, a, b))
        n = len(raw_ts)
        return {"ts": raw_ts, "open": [1.0] * n, "high": [2.0] * n, "low": [0.5] * n, "close": [1.5] * n, "volume": [3] * n}
    f = gf.GapFiller()
    now = tpe(2026, 10, 21, 13, 55)
    assert asyncio.run(f.run_once(now, store, kbars_full, True)) == 170
    assert calls == [("TMF", "2026-10-21", "2026-10-21")] and len(store.rows) == 170 and min(r[0] for r in store.rows) == start + 130 * 60
    assert asyncio.run(f.run_once(now, store, kbars_full, True)) == 0 and len(calls) == 1           # 一段只做一次


def test_run_once_skips_full_sessions_and_handles_failures():
    start = int(tpe(2026, 10, 21, 8, 45).timestamp())
    others = [w for w in gf.windows_due(tpe(2026, 10, 21, 13, 55)) if w["key"] != "day:2026-10-21"]
    full = FakeStore({start + 60 * k for k in range(299)} | full_minutes(*others))     # 少 1 分鐘：可能真的沒成交，不查
    calls = []

    async def boom(code, a, b):
        calls.append(1)
        raise TimeoutError
    f = gf.GapFiller()
    assert asyncio.run(f.run_once(tpe(2026, 10, 21, 13, 55), full, boom, True)) == 0 and calls == []
    f2, empty = gf.GapFiller(), FakeStore(full_minutes(*others))                       # 只有 10/21 日盤是空的
    for _ in range(5):
        asyncio.run(f2.run_once(tpe(2026, 10, 21, 14, 0), empty, boom, True))
    assert len(calls) == gf.MAX_TRIES                                                    # 失敗最多試 3 次
    assert asyncio.run(gf.GapFiller().run_once(tpe(2026, 10, 21, 14, 0), empty, boom, False)) is None    # 沒連上不查


def test_store_methods(tmp_path):
    s = MarketStore(tmp_path / "m.db")
    t0 = int(tpe(2026, 10, 21, 10, 0).timestamp())
    assert s.fills_at("TMF") == 0.0 and s.flow_minutes("TMF", t0, t0 + 600) == set()
    assert s.upsert_fill_1m("TMF", [(t0, 1, 2, 0.5, 1.5, 3), (t0 + 60, 1, 2, 0.5, 1.5, 4)], 123.0) == 2
    assert [r["ts"] for r in s.fill_1m("TMF", t0 + 30)] == [t0 + 60] and s.fills_at("TMF") == 123.0
    assert s.upsert_fill_1m("TMF", [], 1.0) == 0


def test_bars_api_merges_fills_without_duplicates(tmp_path, monkeypatch):
    from api import routes_market as rm
    s = MarketStore(tmp_path / "m.db")
    now = 1_800_000_000
    base = (now // 60) * 60 - 600
    with s._conn() as c:
        c.execute("INSERT INTO flow_1m(code, ts, buy_n, sell_n, unk_n, buy_vol, sell_vol, unk_vol, open, high, low, close) VALUES ('TMF', ?, 1, 1, 0, 2, 3, 0, 100, 101, 99, 100.5)", (base,))
    s.upsert_fill_1m("TMF", [(base, 1, 1, 1, 1, 1), (base + 60, 200, 201, 199, 200.5, 7)], 456.0)          # base 已經有真實資料：不覆蓋
    monkeypatch.setattr(rm.market_state, "store", s)
    monkeypatch.setattr(rm.time, "time", lambda: now)
    monkeypatch.setattr(rm.flow_recorder, "current_row", lambda: None)
    r = asyncio.run(rm.get_bars(minutes=60, since=None))
    assert [b["t"] for b in r["bars"]] == [base, base + 60] and r["fills_at"] == 456.0
    assert r["bars"][0]["o"] == 100 and "f" not in r["bars"][0]
    assert r["bars"][1] == {"t": base + 60, "o": 200, "h": 201, "l": 199, "c": 200.5, "v": 7, "b": 0, "s": 0, "n": 0, "f": 1}


def test_not_in_simulation(monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    assert not gf.enabled()
    asyncio.run(asyncio.wait_for(gf.gapfill_loop(None, None, lambda: True), 1))
    monkeypatch.setenv("SIMULATION", "false")
    assert gf.enabled()
    monkeypatch.setenv("GAPFILL", "false")
    assert not gf.enabled()


def test_research_modules_never_read_the_fill_table():
    users = sorted(p.name for p in list((ROOT / "core").glob("*.py")) + list((ROOT / "api").glob("*.py")) + list((ROOT / "strategies").glob("*.py"))
                   if "bars_fill_1m" in p.read_text(encoding="utf-8") or "fill_1m(" in p.read_text(encoding="utf-8"))
    assert users == ["bar_gapfill.py", "market_store.py", "routes_market.py"], users


def test_old_holes_are_filled_later_and_not_requeried():
    """10/21 的洞：隔天（10/22）收盤後的檢查會補上；補過之後（重啟也一樣）覆蓋率是滿的，不再查券商。"""
    from datetime import timedelta
    ws = gf.windows_due(tpe(2026, 10, 22, 13, 55))
    hole = next(w for w in ws if w["key"] == "day:2026-10-21")
    have = full_minutes(*[w for w in ws if w["key"] != "day:2026-10-21"]) | set(range(int(hole["start"]), int(hole["start"]) + 130 * 60, 60))
    store, calls = FakeStore(have), []
    raw_ts, t = [], datetime(2026, 10, 21, 8, 46)
    for _ in range(300):
        raw_ts.append(int(t.replace(tzinfo=timezone.utc).timestamp() * 1e9))
        t += timedelta(minutes=1)

    async def kb(code, a, b):
        calls.append((a, b))
        return {"ts": raw_ts, "open": [1.0] * 300, "high": [2.0] * 300, "low": [0.5] * 300, "close": [1.5] * 300, "volume": [3] * 300}
    assert asyncio.run(gf.GapFiller().run_once(tpe(2026, 10, 22, 13, 55), store, kb, True)) == 170 and calls == [("2026-10-21", "2026-10-21")]
    assert asyncio.run(gf.GapFiller().run_once(tpe(2026, 10, 22, 14, 10), store, kb, True)) == 0 and len(calls) == 1      # 新的行程（重啟）：已經補滿，不查
