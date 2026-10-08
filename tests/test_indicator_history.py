"""core/indicator_history.py：每日指標歷史、本機 ticks.db 與券商歷史逐筆的聚合、回補程序（流量保護）。

券商部分全部用假的 api 物件，不連線。"""
from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest

import core.indicator_history as ih
from core.iv_monitor import iv_percentile
from core.market_store import MarketStore


def at(h: int, m: int, s: int = 0, day: int = 8) -> float:
    return time.mktime((2026, 10, day, h, m, s, 0, 0, -1))


def naive_ns(day: int, h: int, m: int, s: int = 0) -> int:
    """券商歷史資料的時間戳：台灣當地時間當成 UTC 的 epoch（奈秒）。"""
    return int((datetime(2026, 10, day, h, m, s) - datetime(1970, 1, 1)).total_seconds() * 1e9)


def ticks_obj(items):
    """items: [(day, h, m, s, price, volume, tick_type)]（可亂序）→ 像 api.ticks() 的結果。"""
    return SimpleNamespace(ts=[naive_ns(d, h, m, s) for d, h, m, s, *_ in items], close=[i[4] for i in items],
                           volume=[i[5] for i in items], tick_type=[i[6] for i in items])


# ── 時間轉換與聚合 ───────────────────────────────────────────────

def test_naive_ns_becomes_the_real_epoch():
    got = ih.naive_ns_to_epoch(np.array([naive_ns(8, 9, 0), naive_ns(8, 9, 1, 30)]))
    assert got.tolist() == [at(9, 0), at(9, 1, 30)]
    assert ih.naive_ns_to_epoch(np.array([])).size == 0


def test_preopen_mask_matches_the_quote_hub_windows():
    t = np.array([at(8, 29, 59), at(8, 30), at(8, 44, 59), at(8, 45), at(14, 50), at(14, 59, 59), at(15, 0), at(3, 0)])
    assert ih.preopen_mask(t).tolist() == [False, True, True, False, True, True, False, False]


def test_broker_ticks_are_sorted_aggregated_and_preopen_is_dropped():
    ticks = ticks_obj([
        (8, 9, 1, 5, 101.0, 1, 2),       # 故意亂序
        (8, 9, 0, 10, 100.0, 2, 1),
        (8, 8, 31, 0, 49479.0, 1, 1),    # 開盤前試算：不能進統計
        (8, 9, 0, 40, 100.5, 1, 1),
        (8, 9, 1, 50, 102.0, 1, 0),
        (8, 9, 2, 0, 103.0, 0, 1),       # volume 0：不算
    ])
    rows = ih.broker_ticks_to_rows(ticks)
    assert [(r["ts"], r["buy_n"], r["sell_n"], r["unk_n"]) for r in rows] == [(int(at(9, 0)), 2, 0, 0), (int(at(9, 1)), 0, 1, 1)]
    assert rows[0]["buy_vol"] == 3 and rows[0]["open"] == 100.0 and rows[0]["close"] == 100.5
    assert ih.broker_ticks_to_rows(ticks_obj([])) == []


def make_ticks_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE ticks (code TEXT NOT NULL, ts REAL NOT NULL, price REAL NOT NULL, volume INTEGER NOT NULL, "
              "tick_type INTEGER NOT NULL, total_volume INTEGER NOT NULL DEFAULT 0)")
    c.executemany("INSERT INTO ticks VALUES (?,?,?,?,?,?)", [
        ("TMFJ6", at(9, 0, 5), 100.0, 1, 1, 1), ("TMFJ6", at(9, 0, 6), 100.0, 0, 1, 1),       # 第二筆是報價更新
        ("MXFJ6", at(9, 0, 7), 100.0, 1, 1, 1),                                                  # 別的合約
        ("TMFJ6", at(9, 1, 5), 101.0, 2, 2, 3), ("TMFJ6", at(8, 31, 0), 49479.0, 1, 1, 4)])      # 最後一筆是盤前試算
    c.commit()
    c.close()


def test_ticks_db_rows_only_include_tmf_trades_outside_preopen(tmp_path):
    db = tmp_path / "t.db"
    make_ticks_db(db)
    rows = ih.ticks_db_to_rows(db)
    assert [(r["ts"], r["buy_n"], r["sell_n"]) for r in rows] == [(int(at(9, 0)), 1, 0), (int(at(9, 1)), 0, 1)]
    assert rows[1]["sell_vol"] == 2


# ── 每日指標歷史 ─────────────────────────────────────────────────

def seed_daily(tmp_path, bars=90, ivs=0, seed=3):
    store = MarketStore(tmp_path / "m.db")
    rng = np.random.default_rng(seed)
    price, daily, d0 = 20000.0, [], date(2026, 6, 1)
    for i in range(bars):
        price *= 1 + float(rng.normal(0, 0.008))
        daily.append({"date": date.fromordinal(d0.toordinal() + i).isoformat(), "open": price, "high": price * 1.006,
                      "low": price * 0.994, "close": price, "volume": 1000, "nbars": 300})
    store.upsert_bars(ih.BAR_CODE, daily)
    for i in range(ivs):
        store.upsert_iv(daily[bars - ivs + i]["date"], 18.0 + (i * 7 % 11), "csv")
    return store, daily


def test_daily_history_has_hurst_only_after_a_full_window_and_never_looks_ahead(tmp_path):
    store, daily = seed_daily(tmp_path)
    rows = ih.build_daily(store)
    assert [r["date"] for r in rows] == [b["date"] for b in daily]
    assert all(r["hurst"] is None for r in rows[:59]) and all(r["hurst"] is not None for r in rows[59:])
    assert all(r["direction"] == 0 for r in rows[:19])                           # 不到 20 根日 K 沒有方向
    # 不看未來：第 i 天的 Hurst 只用到第 i 天為止的日 K（拿掉後面的資料，這一天的值不能變）
    store2 = MarketStore(tmp_path / "m2.db")
    store2.upsert_bars(ih.BAR_CODE, daily[:70])
    short = ih.build_daily(store2)
    assert short[69]["hurst"] == pytest.approx(rows[69]["hurst"]) and short[69]["direction"] == rows[69]["direction"]


def test_iv_percentile_needs_enough_history_and_matches_the_system_definition(tmp_path):
    few, _ = seed_daily(tmp_path, ivs=2)
    rows = ih.build_daily(few)
    assert sum(r["iv"] is not None for r in rows) == 2 and all(r["iv_pct"] is None for r in rows)    # 歷史不足 → 不給百分位
    many, daily = seed_daily(tmp_path / "b", bars=90, ivs=80)
    rows = ih.build_daily(many)
    last = rows[-1]
    series = many.iv_series()
    assert last["iv_pct"] == pytest.approx(iv_percentile(series[-1][1], [v for _, v in series[:-1]][::-1]))
    assert last["iv_state"] in ("LOW", "NORMAL", "HIGH")
    assert rows[9]["iv"] is None and rows[10]["iv"] is not None and rows[10]["iv_pct"] is None      # IV 從第 10 天開始有，但前面沒有歷史 → 沒有百分位
    assert ih.build_daily(many) == rows and len(many.indicator_daily()) == 90                         # 可重複執行，結果一致


def row_at(ts):
    return {"ts": ts, "buy_n": 1, "sell_n": 0, "unk_n": 0, "buy_vol": 1, "sell_vol": 0, "unk_vol": 0,
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}


# ── 券商回補（假的 api）─────────────────────────────────────────

class FakeApi:
    def __init__(self, ticks_by_date=None, per_call_bytes=5_000_000, connections=2, limit=2_000_000_000, used=40_000_000):
        self.ticks_by_date = ticks_by_date or {}
        self.per_call_bytes, self.connections, self.limit, self.used = per_call_bytes, connections, limit, used
        self.calls, self.logged_out = [], False
        self.Contracts = SimpleNamespace(Futures=SimpleNamespace(TMF=SimpleNamespace(TMFR1="TMFR1")))

    def usage(self):
        return SimpleNamespace(connections=self.connections, bytes=self.used, limit_bytes=self.limit,
                               remaining_bytes=self.limit - self.used)

    def ticks(self, contract, date, timeout):
        assert contract == "TMFR1"
        self.calls.append(date)
        self.used += self.per_call_bytes
        v = self.ticks_by_date.get(date)
        if isinstance(v, list):                                    # 第一次丟例外、之後回傳
            r = v.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return v if v is not None else ticks_obj([])

    def logout(self):
        self.logged_out = True


def day_ticks(day):
    return ticks_obj([(day, 9, 0, 5, 100.0, 1, 1), (day, 9, 0, 30, 100.5, 2, 2), (day, 9, 1, 5, 101.0, 1, 1)])


TODAY = date(2026, 10, 8)        # 週四 → 往前的平日：10/07、10/06、10/05、10/02、10/01


def fetch(tmp_path, api, **kw):
    sleeps = []
    store = kw.pop("store", None) or MarketStore(tmp_path / "m.db")
    res = ih.fetch_flow_history(kw.pop("days", 7), store=store, api=api, today=TODAY, sleep=sleeps.append, log=lambda *a: None, **kw)
    return res, store, sleeps


def test_fetch_stores_each_day_throttles_and_measures_traffic(tmp_path):
    api = FakeApi({f"2026-10-0{d}": day_ticks(d) for d in (7, 6, 5)})
    res, store, sleeps = fetch(tmp_path, api, days=7)                      # 往前 7 天的平日：10/07 10/06 10/05 10/02 10/01（後兩天沒資料）
    assert api.calls[:3] == ["2026-10-07", "2026-10-06", "2026-10-05"]       # 新的日子先抓
    assert res["fetched"] == ["2026-10-07", "2026-10-06", "2026-10-05"] and res["minutes"] == 6
    assert len(res["empty"]) == 2 and res["failed"] == [] and res["stopped"] is None
    assert res["bytes"] == 5 * 5_000_000                                      # 每次呼叫的流量用 usage 差額量
    assert len(sleeps) == 5 and all(s >= 1 for s in sleeps)                   # 每次呼叫之間睡一下，不超過券商頻率上限
    assert len(store.flow_1m("TMF")) == 6 and {r["source"] for r in store.flow_1m("TMF")} == {"broker"}
    assert api.logged_out is False                                            # 外面傳進來的 api 由呼叫端負責登出


def test_days_that_already_have_a_day_session_are_skipped_unless_forced(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    t0 = int(at(9, 0, day=7))
    store.upsert_flow_1m("TMF", [row_at(t0 + 60 * i) for i in range(250)])
    api = FakeApi({"2026-10-06": day_ticks(6)})
    res, _, _ = fetch(tmp_path, api, days=3, store=store)
    assert "2026-10-07" not in api.calls and "2026-10-06" in api.calls
    api2 = FakeApi()
    fetch(tmp_path, api2, days=3, store=store, force=True)
    assert "2026-10-07" in api2.calls


def test_stops_before_eating_the_traffic_reserved_for_live_quotes(tmp_path):
    api = FakeApi({f"2026-10-0{d}": day_ticks(d) for d in (7, 6, 5)}, per_call_bytes=500_000_000, used=900_000_000, limit=2_000_000_000)
    res, _, _ = fetch(tmp_path, api, days=5, reserve_mb=800, max_mb=10_000)
    # 剩 1100 MB → 抓一天 -500 → 600 MB 低於保留的 800 MB → 第二天不再抓
    assert api.calls == ["2026-10-07"] and "低於保留額度" in res["stopped"]


def test_stops_at_the_per_run_traffic_cap(tmp_path):
    api = FakeApi({f"2026-10-0{d}": day_ticks(d) for d in (7, 6, 5)}, per_call_bytes=100_000_000)
    res, _, _ = fetch(tmp_path, api, days=5, max_mb=150, reserve_mb=0)
    assert api.calls == ["2026-10-07", "2026-10-06"] and "達上限" in res["stopped"]          # 第二天後累計 200 MB ≥ 150


def test_cap_still_works_when_the_broker_traffic_counter_lags(tmp_path, monkeypatch):
    """實測券商的流量計數有延遲（抓完當下 +0、十幾秒後才反映），所以本次上限要靠「筆數 × 每筆位元組」的估算。"""
    api = FakeApi({f"2026-10-0{d}": day_ticks(d) for d in (7, 6, 5)}, per_call_bytes=0)         # usage 完全不動
    monkeypatch.setattr(ih, "BYTES_PER_TICK", 60_000_000)                                      # 3 筆 × 6000 萬 ≈ 180 MB/天
    res, _, _ = fetch(tmp_path, api, days=5, max_mb=250, reserve_mb=0)
    assert api.calls == ["2026-10-07", "2026-10-06"] and "達上限" in res["stopped"]             # 第二天後估算 360 MB ≥ 250
    assert res["bytes"] == 2 * 3 * 60_000_000


def test_refuses_to_log_in_more_when_connections_are_already_high(tmp_path):
    api = FakeApi({"2026-10-07": day_ticks(7)}, connections=5)
    res, store, _ = fetch(tmp_path, api, days=3)
    assert api.calls == [] and "連線數" in res["stopped"] and store.flow_1m("TMF") == []


def test_a_failed_day_is_retried_once_then_reported(tmp_path):
    api = FakeApi({"2026-10-07": [TimeoutError("slow"), day_ticks(7)], "2026-10-06": [TimeoutError("a"), TimeoutError("b")]})
    res, _, _ = fetch(tmp_path, api, days=2)
    assert res["fetched"] == ["2026-10-07"] and res["failed"] == ["2026-10-06"]
    assert api.calls.count("2026-10-07") == 2 and api.calls.count("2026-10-06") == 2


def test_probe_fetches_one_day_and_returns_its_rows_for_comparison(tmp_path):
    api = FakeApi({"2026-10-07": day_ticks(7), "2026-10-06": day_ticks(6)})
    res, _, _ = fetch(tmp_path, api, days=5, probe=True)
    assert api.calls == ["2026-10-07"] and len(res["rows"]) == 2
    api2 = FakeApi({"2026-10-06": day_ticks(6)})
    res2, _, _ = fetch(tmp_path, api2, days=5, probe=True, dates=[date(2026, 10, 6)])
    assert api2.calls == ["2026-10-06"] and res2["fetched"] == ["2026-10-06"]


def test_it_logs_out_when_it_opened_the_session_itself(tmp_path, monkeypatch):
    api = FakeApi({"2026-10-07": day_ticks(7)})
    monkeypatch.setattr(ih, "open_readonly_api", lambda: api)
    ih.fetch_flow_history(2, store=MarketStore(tmp_path / "m.db"), today=TODAY, sleep=lambda s: None, log=lambda *a: None)
    assert api.logged_out is True
    bad = FakeApi()
    bad.usage = lambda: (_ for _ in ()).throw(RuntimeError("登入成功但查流量時壞掉"))
    monkeypatch.setattr(ih, "open_readonly_api", lambda: bad)
    with pytest.raises(RuntimeError):
        ih.fetch_flow_history(2, store=MarketStore(tmp_path / "m2.db"), today=TODAY, sleep=lambda s: None, log=lambda *a: None)
    assert bad.logged_out is True                                              # 例外往外丟也一定登出（殘留連線會佔券商的 5 條額度）


def test_nothing_to_do_never_logs_in(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "open_readonly_api", lambda: (_ for _ in ()).throw(AssertionError("不該登入")))
    store = MarketStore(tmp_path / "m.db")
    t0 = int(at(9, 0, day=7))
    store.upsert_flow_1m("TMF", [row_at(t0 + 60 * i) for i in range(250)])
    res = ih.fetch_flow_history(1, store=store, today=date(2026, 10, 8), sleep=lambda s: None, log=lambda *a: None)
    assert res["stopped"] == "沒有需要補的日子"


def test_trading_days_skips_weekends():
    assert [d.isoformat() for d in ih.trading_days(7, TODAY)] == ["2026-10-07", "2026-10-06", "2026-10-05", "2026-10-02", "2026-10-01"]
    assert ih.trading_days(1, TODAY, include_today=True)[0] == TODAY


# ── 比對與狀態 ───────────────────────────────────────────────────

def test_compare_rows_reports_agreement_and_gaps():
    a = [row_at(60), row_at(120), row_at(180)]
    same = ih.compare_rows(a, [dict(r) for r in a])
    assert same["minutes"] == 3 and same["identical_count_rate"] == 1.0 and same["count_ratio_other_over_local"] == 1.0
    other = [dict(a[0], buy_n=2), dict(a[1]), row_at(240)]
    diff = ih.compare_rows(a, other)
    assert diff["minutes"] == 2 and diff["only_in_local"] == 1 and diff["only_in_other"] == 1
    assert diff["identical_count_rate"] == 0.5 and diff["count_ratio_other_over_local"] == pytest.approx(3 / 2)
    assert ih.compare_rows(a, [row_at(999)])["minutes"] == 0


def test_status_summarises_what_has_been_collected(tmp_path):
    store, _ = seed_daily(tmp_path, bars=70, ivs=2)
    ih.build_daily(store)
    t0 = int(at(9, 0))
    store.upsert_flow_1m("TMF", [row_at(t0 + 60 * i) for i in range(5)])
    st = ih.status(store)
    assert st["flow_1m"]["minutes"] == 5 and st["flow_1m"]["day_session_days"] == 0
    assert st["indicator_daily"]["days"] == 70 and st["indicator_daily"]["with_hurst"] == 11
    assert st["indicator_daily"]["with_iv_pct"] == 0 and st["iv_points"] == 2
