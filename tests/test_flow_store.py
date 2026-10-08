"""逐分鐘外/內盤成交統計（core/flow_store.py）與 market_store 的 flow_1m／indicator_daily。

背景：市場指標視覺化要先歷史回放、統計驗證，需要三個指標合在一起的歷史；外/內盤比例原本只活在記憶體。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import core.daily_summary as ds
import core.quote_hub as qh
from core.daily_summary import Config, MarketStateService
from core.flow_store import FlowRecorder, MinuteAcc, aggregate_trades
from core.live_state import LiveState
from core.market_store import MarketStore


def at(h: int, m: int, s: int = 0, day: int = 8) -> float:
    return time.mktime((2026, 10, day, h, m, s, 0, 0, -1))


def row(ts, buy=0, sell=0, unk=0, close=100.0):
    return {"ts": ts, "buy_n": buy, "sell_n": sell, "unk_n": unk, "buy_vol": buy, "sell_vol": sell, "unk_vol": unk,
            "open": close, "high": close, "low": close, "close": close}


# ── 聚合規則 ─────────────────────────────────────────────────────

def test_minute_acc_counts_sides_volumes_and_ohlc():
    a = MinuteAcc(600)
    a.add(100.0, 2, 1)           # 外盤
    a.add(103.0, 1, 2)           # 內盤
    a.add(99.0, 3, 1)
    a.add(101.0, 1, 0)           # 無法判斷方向
    r = a.row()
    assert (r["buy_n"], r["sell_n"], r["unk_n"]) == (2, 1, 1) and (r["buy_vol"], r["sell_vol"], r["unk_vol"]) == (5, 1, 1)
    assert (r["open"], r["high"], r["low"], r["close"]) == (100.0, 103.0, 99.0, 101.0) and a.trades == 4


def test_aggregate_trades_buckets_by_minute_and_skips_non_trades():
    ts = [at(9, 0, 5), at(9, 0, 50), at(9, 1, 1), at(9, 1, 2), at(9, 1, 3), at(9, 3, 0)]
    price = [100, 101, 102, 0, 103, 104]                 # 價格 0 的略過
    vol = [1, 2, 1, 1, 0, 1]                              # volume 0 的略過
    tt = [1, 2, 1, 1, 1, 2]
    out = aggregate_trades(ts, price, vol, tt)
    assert [(r["ts"], r["buy_n"], r["sell_n"]) for r in out] == [(int(at(9, 0)), 1, 1), (int(at(9, 1)), 1, 0), (int(at(9, 3)), 0, 1)]
    dropped = aggregate_trades(ts, [100] * 6, [1] * 6, [1] * 6, drop=[False, False, True, True, True, False])
    assert [r["ts"] for r in dropped] == [int(at(9, 0)), int(at(9, 3))]        # 被標記的（如盤前試算）整筆略過


def test_a_late_tick_is_merged_into_the_current_minute_instead_of_starting_a_new_one():
    out = aggregate_trades([at(9, 1, 5), at(9, 0, 59)], [100, 101], [1, 1], [1, 1])
    assert len(out) == 1 and out[0]["buy_n"] == 2


# ── 資料庫 ───────────────────────────────────────────────────────

def test_upsert_keeps_the_more_complete_minute(tmp_path):
    s = MarketStore(tmp_path / "m.db")
    t = int(at(9, 0))
    s.upsert_flow_1m("TMF", [row(t, buy=5, sell=5)], source="live")
    s.upsert_flow_1m("TMF", [row(t, buy=1, sell=1)], source="broker")              # 較殘缺的不能蓋掉完整的
    got = s.flow_1m("TMF")
    assert len(got) == 1 and got[0]["buy_n"] == 5 and got[0]["source"] == "live"
    s.upsert_flow_1m("TMF", [row(t, buy=8, sell=6)], source="broker")               # 較完整的才覆蓋
    got = s.flow_1m("TMF")
    assert got[0]["buy_n"] == 8 and got[0]["source"] == "broker"
    s.upsert_flow_1m("TMF", [row(t, buy=8, sell=6, close=555.0)], source="x")       # 一樣完整：維持原狀
    assert s.flow_1m("TMF")[0]["close"] == 100.0


def test_flow_range_span_and_day_session_coverage(tmp_path):
    s = MarketStore(tmp_path / "m.db")
    day = [row(int(at(9, 0)) + 60 * i, buy=1) for i in range(250)]                  # 10/08 日盤 250 分鐘
    thin = [row(int(at(9, 0, day=7)) + 60 * i, buy=1) for i in range(50)]           # 10/07 只有 50 分鐘
    night = [row(int(at(20, 0, day=7)) + 60 * i, buy=1) for i in range(300)]        # 夜盤不算日盤覆蓋
    s.upsert_flow_1m("TMF", day + thin + night)
    s.upsert_flow_1m("MXF", [row(int(at(9, 0)))])                                    # 別的合約不混進來
    assert s.flow_days("TMF") == {"2026-10-08"}
    assert s.flow_days("TMF", min_minutes=40) == {"2026-10-08", "2026-10-07"}
    assert len(s.flow_1m("TMF", at(9, 0), at(9, 10))) == 10
    sp = s.flow_1m_span("TMF")
    assert sp["n"] == 600 and sp["first_ts"] == int(at(9, 0, day=7))
    assert s.flow_1m_span("NONE") == {"n": 0, "first_ts": None, "last_ts": None}


def test_indicator_daily_roundtrip_and_iv_series(tmp_path):
    s = MarketStore(tmp_path / "m.db")
    s.upsert_indicator_daily([{"date": "2026-10-07", "hurst": 0.55, "hurst_z": 1.2, "hurst_state": "RANDOM", "direction": 1,
                               "iv": 20.0, "iv_pct": None, "iv_state": None}])
    s.upsert_indicator_daily([{"date": "2026-10-07", "hurst": 0.6, "direction": -1}])         # 同一天重寫 → 取代
    r = s.indicator_daily()
    assert len(r) == 1 and r[0]["hurst"] == 0.6 and r[0]["direction"] == -1 and r[0]["iv"] is None
    assert s.indicator_daily(since="2026-10-08") == []
    s.upsert_iv("2026-10-08", 21.0, "manual")
    s.upsert_iv("2026-10-07", 20.0, "csv")
    assert s.iv_series() == [("2026-10-07", 20.0), ("2026-10-08", 21.0)]                     # 舊 → 新


# ── FlowRecorder ─────────────────────────────────────────────────

def make_recorder(tmp_path, monkeypatch=None, enabled=True):
    if monkeypatch is not None:
        monkeypatch.setenv("RECORD_FLOW", "true" if enabled else "false")
    store = MarketStore(tmp_path / "m.db")
    rec = FlowRecorder(store)
    rec.start()
    return rec, store


def test_recorder_writes_one_row_per_minute_and_skips_non_trades(tmp_path, monkeypatch):
    rec, store = make_recorder(tmp_path, monkeypatch)
    f = rec.feed
    f("TMFJ6", 100.0, 2, 10, 1, at(9, 0, 5))          # 外盤成交
    f("TMFJ6", 100.0, 0, 10, 1, at(9, 0, 6))          # 報價更新（volume=0）：不是成交
    f("TMFJ6", 100.0, 1, 10, 1, at(9, 0, 7))          # 重複回報（total_volume 沒增加）
    f("MXFJ6", 100.0, 1, 5, 1, at(9, 0, 8))           # 別的合約
    f("TMFJ6", 101.0, 1, 11, 2, at(9, 0, 20))         # 內盤成交
    f("TMFJ6", 102.0, 1, 12, 0, at(9, 0, 30))         # 方向不明
    f("TMFJ6", 103.0, 3, 15, 1, at(9, 1, 2))          # 下一分鐘 → 上一分鐘送出
    rec.stop()                                         # 關機時進行中的那一分鐘也存下來
    rows = store.flow_1m("TMF")
    assert [(r["ts"], r["buy_n"], r["sell_n"], r["unk_n"]) for r in rows] == [(int(at(9, 0)), 1, 1, 1), (int(at(9, 1)), 1, 0, 0)]
    assert rows[0]["buy_vol"] == 2 and rows[0]["close"] == 102.0 and rows[0]["high"] == 102.0 and rows[0]["open"] == 100.0
    assert rows[1]["buy_vol"] == 3


def test_recorder_flushes_the_last_minute_after_the_session_ends(tmp_path, monkeypatch):
    rec, store = make_recorder(tmp_path, monkeypatch)
    rec.feed("TMFJ6", 100.0, 1, 1, 1, at(13, 44, 50))                                # 日盤最後一筆之後再沒有成交
    rec.flush_stale(now=at(13, 44, 58))                                                # 這分鐘才過 58 秒（< 75）：還可能有成交，不能提早關
    assert rec._cur is not None
    rec.flush_stale(now=at(13, 46, 30))                                                # 過了 → 關掉並送出
    assert rec._cur is None
    rec.stop()
    assert [r["ts"] for r in store.flow_1m("TMF")] == [int(at(13, 44))]


def test_recorder_is_inert_before_start_and_when_disabled(tmp_path, monkeypatch):
    store = MarketStore(tmp_path / "m.db")
    rec = FlowRecorder(store)
    rec.feed("TMFJ6", 100.0, 1, 1, 1, at(9, 0, 5))                                     # 還沒 start：什麼都不做
    assert rec._cur is None
    monkeypatch.setenv("RECORD_FLOW", "false")
    off = FlowRecorder(store)
    off.start()
    off.feed("TMFJ6", 100.0, 1, 1, 1, at(9, 0, 5))
    assert off._thread is None and off._cur is None
    off.stop()
    assert store.flow_1m("TMF") == []


def test_recorder_survives_a_writer_failure(tmp_path, monkeypatch):
    class Boom(MarketStore):
        def upsert_flow_1m(self, *a, **k):
            raise RuntimeError("disk full")

    monkeypatch.setenv("RECORD_FLOW", "true")
    rec = FlowRecorder(Boom(tmp_path / "m.db"))
    rec.start()
    rec.feed("TMFJ6", 100.0, 1, 1, 1, at(9, 0, 5))
    rec.feed("TMFJ6", 100.0, 1, 2, 1, at(9, 1, 5))
    rec.stop()                                                                         # 寫入失敗只丟那批，不會卡住或丟例外


def test_full_queue_drops_minutes_without_blocking(tmp_path, monkeypatch):
    import core.flow_store as fs
    monkeypatch.setattr(fs, "QUEUE_MAX", 1)
    monkeypatch.setenv("RECORD_FLOW", "true")
    rec = FlowRecorder(MarketStore(tmp_path / "m.db"))
    rec._thread = object()                      # 假裝已啟動但沒有 writer，佇列就會滿
    rec._q = fs.queue.Queue(maxsize=1)
    for i in range(4):
        rec.feed("TMFJ6", 100.0, 1, i + 1, 1, at(9, i, 5))
    assert rec._dropped >= 2                    # 滿了就丟、記數，不阻塞報價路徑
    rec._thread = None


# ── 接進報價路徑 ─────────────────────────────────────────────────

class FakeFlow:
    def __init__(self, boom=False):
        self.got, self.boom = [], boom

    def feed(self, *a):
        if self.boom:
            raise RuntimeError("boom")
        self.got.append(a)


def snap(t, code="TMFJ6", price=100.0, total=1):
    return {"code": code, "close": price, "volume": 1, "total_volume": total, "tick_type": 1, "ts": t}


def test_quote_hub_feeds_the_recorder_but_never_preopen_ticks(monkeypatch):
    fake = FakeFlow()
    monkeypatch.setattr(qh, "live_state", LiveState())                                 # 不污染全域的即時狀態
    monkeypatch.setattr(qh, "flow_recorder", fake)
    monkeypatch.setattr(qh, "FILTER_PREOPEN", True)
    hub = qh.QuoteHub()
    hub._inject_quote(snap(at(8, 31), price=49479.0))          # 開盤前試算：不能進統計
    hub._inject_quote(snap(at(9, 0, 5), price=49500.0, total=2))
    assert [(a[0], a[1]) for a in fake.got] == [("TMFJ6", 49500.0)]


def test_a_failing_recorder_never_blocks_quote_dispatch(monkeypatch):
    monkeypatch.setattr(qh, "live_state", LiveState())
    monkeypatch.setattr(qh, "flow_recorder", FakeFlow(boom=True))
    hub, got = qh.QuoteHub(), []

    async def cb(s):
        got.append(s["close"])

    async def run():
        hub.setup(asyncio.get_running_loop())
        hub.subscribe_strategy("t", cb)
        hub._inject_quote(snap(at(9, 0, 5), price=49500.0))
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert got == [49500.0]                                                              # 記錄器出錯，策略照樣收到報價


class FlushSpy:
    def __init__(self, calls):
        self.calls = calls

    def flush_stale(self):
        self.calls.append("flush")


def test_scheduler_closes_the_last_minute_even_on_weekends(tmp_path, monkeypatch):
    """週五夜盤跨到週六凌晨，週末的排程不能因為 weekday 檢查就提早 return 而漏掉收尾。"""
    calls = []
    monkeypatch.setattr(ds, "flow_recorder", FlushSpy(calls))

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 4, 59)                                        # 週六

    monkeypatch.setattr(ds, "datetime", _DT)
    monkeypatch.setenv("SIMULATION", "true")

    class NoBroker:
        is_connected = False

    svc = MarketStateService(MarketStore(tmp_path / "m.db"), Config(iv_auto=False, backup=False), NoBroker())
    asyncio.run(svc.tick({}))
    assert calls == ["flush"]
