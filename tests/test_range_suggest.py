"""機率範圍帶的顯示端（core/range_suggest.py、GET /api/market/range，update.md 發現 48）：組裝、單位、缺值、日夜盤、到收盤的剩餘分鐘、不穩與樣本少的旗標、真正的實測表。"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import time

import pytest

import api.routes_market as rm
import core.range_calibration as rc
import core.range_suggest as rs


def cell(term=(0.6, 1.2, 2.0), path=(1.0, 1.6, 2.5), few=False, ut=(False,) * 3, up=(False,) * 3, ho_t=None, ho_p=None):
    c = {"term": list(term), "path": list(path), "gauss": [0.67, 1.28, 1.96], "unstable_term": list(ut), "unstable_path": list(up), "few": few, "n": 5000, "days": 1000}
    if ho_t is not None:
        c["holdout_ok_term"] = list(ho_t)
    if ho_p is not None:
        c["holdout_ok_path"] = list(ho_p)
    return c


def table(**kw):
    day = {"sessions": 1600, "from": "2020-03-02", "to": "2026-10-15", "table": {**{str(T): cell(**kw) for T in rc.HORIZONS}, **{rc.bucket_key(b): cell(term=(0.7, 1.4, 2.5), path=(1.2, 1.9, 3.0)) for b in rc.BUCKETS}}}
    night = {"sessions": 43, "from": "2026-08-14", "to": "2026-10-15", "few": True, "table": {str(T): cell(term=(0.58, 1.21, 2.1), path=(1.05, 1.66, 2.6)) for T in rc.HORIZONS}}
    return {"version": 1, "generated": "2026-10-16", "levels": [0.5, 0.8, 0.95], "day": day, "night": night, "holdout": {"split": "2025-01-01", "ok": 44, "total": 48}}


@pytest.fixture
def taipei(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("需要 POSIX 的 tzset")
    monkeypatch.setenv("TZ", "Asia/Taipei")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(y, mo, d, hh, mm, ss=30):
    return time.mktime((y, mo, d, hh, mm, ss, 0, 0, -1))


def flow(now_ts, n=31, base=20000.0):
    b = int(now_ts // 60) * 60
    return [{"ts": b - 60 * i, "close": base + (i % 2)} for i in range(1, n + 1)]


# ── build ───────────────────────────────────────────────────────

def test_band_distances_are_q_times_sigma_root_t_and_centred_on_the_price():
    out = rs.build(30000.0, 4.0, 30, session="day", remaining=None, table=table())
    b = out["bands"]["15"]
    assert b["minutes"] == 15 and b["noise"] == pytest.approx(4.0 * math.sqrt(15), abs=0.05)
    l80 = b["levels"][1]
    assert l80["p"] == 80 and l80["term"]["d"] == pytest.approx(1.2 * 4.0 * math.sqrt(15), abs=0.05) and l80["path"]["d"] == pytest.approx(1.6 * 4.0 * math.sqrt(15), abs=0.05)
    assert l80["term"]["lo"] == pytest.approx(30000.0 - l80["term"]["d"], abs=0.06) and l80["term"]["hi"] == pytest.approx(30000.0 + l80["term"]["d"], abs=0.06)
    assert [lv["p"] for lv in b["levels"]] == [50, 80, 95]
    assert all(lv["path"]["d"] > lv["term"]["d"] for lv in b["levels"])                              # 期間範圍比落點範圍寬
    assert [b["levels"][i]["term"]["d"] for i in range(3)] == sorted(b["levels"][i]["term"]["d"] for i in range(3))
    assert set(out["bands"]) == {"5", "15", "30", "60"}                                              # 沒給剩餘分鐘就沒有「到收盤」
    assert out["bands"]["60"]["noise"] == pytest.approx(out["bands"]["15"]["noise"] * 2, abs=0.2)                      # σ√60 ＝ 2·σ√15


def test_longer_horizons_are_wider_by_root_t_when_the_table_is_flat():
    out = rs.build(30000.0, 3.0, 30, session="day", remaining=None, table=table())
    d5, d60 = out["bands"]["5"]["levels"][1]["term"]["d"], out["bands"]["60"]["levels"][1]["term"]["d"]
    assert d60 / d5 == pytest.approx(math.sqrt(60 / 5), rel=0.02)


def test_close_band_uses_remaining_minutes_and_the_matching_bucket():
    for rem, key in ((20, "c15_30"), (45, "c31_60"), (100, "c61_120"), (200, "c121_270")):
        out = rs.build(30000.0, 4.0, 30, session="day", remaining=rem, table=table())
        c = out["bands"]["close"]
        assert c["bucket"] == key and c["minutes"] == rem and c["noise"] == pytest.approx(4.0 * math.sqrt(rem), abs=0.05)
        assert c["levels"][1]["term"]["d"] == pytest.approx(1.4 * 4.0 * math.sqrt(rem), abs=0.06)      # 用這一組自己的 q（0.7／1.4／2.5）
    for rem in (14, 0, -5, 271, None):
        assert "close" not in rs.build(30000.0, 4.0, 30, session="day", remaining=rem, table=table())["bands"]
    assert "close" not in rs.build(30000.0, 4.0, 30, session="night", remaining=200, table=table())["bands"]   # 夜盤沒有到收盤


def test_night_uses_the_night_table_and_is_marked_few():
    out = rs.build(30000.0, 4.0, 30, session="night", remaining=None, table=table())
    assert all(b["few"] for b in out["bands"].values())
    assert out["bands"]["15"]["levels"][1]["term"]["d"] == pytest.approx(1.21 * 4.0 * math.sqrt(15), abs=0.06)
    assert not any(b["few"] for b in rs.build(30000.0, 4.0, 30, session="day", remaining=None, table=table())["bands"].values())


def test_unstable_comes_from_the_half_split_or_a_failed_holdout():
    out = rs.build(30000.0, 4.0, 30, session="day", remaining=None, table=table(ut=(False, False, True), ho_t=(True, False, True), ho_p=(True, True, False)))
    lv = out["bands"]["15"]["levels"]
    assert [x["unstable_term"] for x in lv] == [False, True, True]                                  # 50% 好、80% 驗證沒過、95% 前後半差太多
    assert [x["unstable_path"] for x in lv] == [False, False, True]
    plain = rs.build(30000.0, 4.0, 30, session="day", remaining=None, table=table())["bands"]["15"]["levels"]
    assert not any(x["unstable_term"] or x["unstable_path"] for x in plain)                         # 沒有驗證資料（夜盤）不算不穩


def test_a_missing_quantile_is_null_not_zero():
    t = table(term=(None, 1.2, 2.0))
    lv = rs.build(30000.0, 4.0, 30, session="day", remaining=None, table=t)["bands"]["15"]["levels"]
    assert lv[0]["term"] is None and lv[1]["term"]["d"] > 0


def test_no_numbers_when_there_is_nothing_to_stand_on():
    n = rs.build(30000.0, 4.0, 30, session="day", remaining=None, table=None)
    assert n["bands"] == {} and n["calibrated"] is False and "實測表" in n["note"] and n["cal"] is None
    for sigma in (None, 0.0):
        out = rs.build(30000.0, sigma, 12, session="day", remaining=None, table=table())
        assert out["bands"] == {} and "成交太少" in out["note"] and "12" in out["note"]
    for price in (None, 0.0, -1.0):
        out = rs.build(price, 4.0, 30, session="day", remaining=None, table=table())
        assert out["bands"] == {} and "成交價" in out["note"]


def test_cal_summary_carries_the_holdout_result():
    out = rs.build(30000.0, 4.0, 30, session="day", remaining=None, table=table())
    assert out["cal"]["holdout_ok"] == 44 and out["cal"]["holdout_total"] == 48 and out["cal"]["split"] == "2025-01-01" and out["cal"]["levels"] == [0.5, 0.8, 0.95]


# ── range_now ───────────────────────────────────────────────────

def test_remaining_minutes_counts_to_1345_and_only_in_the_day(taipei):
    assert rs.remaining_minutes(at(2026, 10, 14, 10, 0), "day") == 225
    assert rs.remaining_minutes(at(2026, 10, 14, 13, 30), "day") == 15
    assert rs.remaining_minutes(at(2026, 10, 14, 13, 44), "day") == 1
    assert rs.remaining_minutes(at(2026, 10, 14, 10, 0), "night") is None


def test_range_now_picks_session_and_close_bucket_from_the_clock(taipei):
    t = table()
    d = at(2026, 10, 14, 13, 0)                                                                      # 距收盤 45 分鐘
    out = rs.range_now(flow(d), d, table=t)
    assert out["session"] == "day" and out["remaining"] == 45 and out["bands"]["close"]["bucket"] == "c31_60" and out["sigma"] == pytest.approx(math.sqrt(30 / 29), abs=0.01)
    assert out["price"] == 20001.0 and out["as_of"] == d and out["stale"] is False
    n = at(2026, 10, 14, 20, 0)
    on = rs.range_now(flow(n), n, table=t)
    assert on["session"] == "night" and on["remaining"] is None and "close" not in on["bands"]
    sat = at(2026, 10, 17, 10, 0)
    assert rs.range_now(flow(sat), sat, table=t)["session"] == "night"


def test_price_prefers_the_live_minute_and_old_prices_are_flagged_stale(taipei):
    now = at(2026, 10, 14, 10, 15, 40)
    b = int(now // 60) * 60
    live = {"ts": b, "close": 20005.0}
    out = rs.range_now(flow(now), now, live_row=live, table=table())
    assert out["price"] == 20005.0 and out["price_age_sec"] == 40 and out["stale"] is False
    old = at(2026, 10, 14, 10, 15)
    rows = flow(old - 600)
    st = rs.range_now(rows, old, table=table())
    assert st["stale"] is True and st["price_age_sec"] > rs.STALE_SEC and st["price"] == rows[0]["close"]
    bad = rs.range_now(flow(now), now, live_row={"ts": b, "close": None}, table=table())
    assert bad["price"] == 20001.0                                                                   # 進行中那根壞了就退回最後一根收完的


def test_range_now_without_recent_trades_says_so(taipei):
    now = at(2026, 10, 14, 10, 15)
    out = rs.range_now([], now, table=table())
    assert out["bands"] == {} and out["price"] is None and out["price_age_sec"] is None and out["stale"] is False


# ── 真正的實測表（core/range_calibration.json）──────────────────────

def test_the_shipped_table_is_sane_and_matches_what_the_ui_expects():
    t = rs.load_table()
    assert t is not None and t["version"] == 1 and t["levels"] == [0.5, 0.8, 0.95] and t["horizons"] == [5, 15, 30, 60] and t["sigma"]["window"] == 30
    assert t["day"]["sessions"] > 1000 and t["night"]["sessions"] >= 30 and t["night"]["few"] is True and t["holdout"]["total"] == 48
    assert set(t["night"]["table"]) == {"5", "15", "30", "60"}
    for sec in ("day", "night"):
        for k, c in t[sec]["table"].items():
            for kind in ("term", "path"):
                q = c[kind]
                assert len(q) == 3 and all(x is not None and x > 0 for x in q) and q == sorted(q)         # 層級越高範圍越寬
            assert all(c["path"][i] > c["term"][i] for i in range(3))                                   # 期間比落點寬
    d = t["day"]["table"]
    for T in ("5", "15", "30"):                                                                          # 白天主要持有時間：實測 80% 的 q 在常態公式附近（± 15%）
        assert d[T]["term"][1] == pytest.approx(1.28, rel=0.15) and d[T]["n"] > 50_000
    for k in ("c15_30", "c31_60", "c61_120", "c121_270"):
        assert k in d
    json.dumps(t, allow_nan=False)


def test_the_shipped_table_builds_a_full_card_with_the_flags_from_the_holdout(taipei):
    t = rs.load_table()
    now = at(2026, 10, 14, 13, 0)                                                                        # 到收盤 45 分鐘（c31_60）
    out = rs.range_now(flow(now, base=30000.0), now, table=t)
    assert set(out["bands"]) == {"5", "15", "30", "60", "close"} and out["calibrated"] is True
    close = out["bands"]["close"]
    assert close["bucket"] == "c31_60" and any(lv["unstable_path"] for lv in close["levels"][:2])      # 登記的驗證裡這一組的期間 50%／80% 沒通過
    assert not any(lv["unstable_term"] or lv["unstable_path"] for lv in out["bands"]["15"]["levels"])  # 15 分鐘的 6 格都過了


# ── API ─────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def flow_1m(self, code, since_ts=None, until_ts=None):
        self.calls.append((code, since_ts))
        return self.rows


def test_route_assembles_from_the_store_and_the_live_minute(monkeypatch, taipei):
    now = at(2026, 10, 14, 10, 15, 40)
    monkeypatch.setattr(time, "time", lambda: now)
    store = FakeStore(flow(now))
    monkeypatch.setattr(rm.market_state, "store", store)
    monkeypatch.setattr(rm.flow_recorder, "current_row", lambda: {"ts": int(now // 60) * 60, "close": 20003.0})
    out = asyncio.run(rm.get_range())
    assert store.calls == [("TMF", now - 3600)] and out["price"] == 20003.0 and out["session"] == "day" and out["calibrated"] is True
    assert set(out["bands"]) == {"5", "15", "30", "60", "close"} and out["as_of"] == now
    json.dumps(out, allow_nan=False)


def test_route_survives_a_broken_live_row_and_a_missing_table(monkeypatch, taipei):
    now = at(2026, 10, 14, 10, 15, 40)
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(rm.market_state, "store", FakeStore(flow(now)))

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(rm.flow_recorder, "current_row", boom)
    monkeypatch.setattr(rs, "load_table", lambda path=None: None)
    out = asyncio.run(rm.get_range())
    assert out["price"] == 20001.0 and out["bands"] == {} and out["calibrated"] is False and "實測表" in out["note"]


def test_load_table_rereads_a_changed_file_and_rejects_garbage(tmp_path):
    p = tmp_path / "t.json"
    assert rs.load_table(p) is None
    p.write_text("not json", encoding="utf-8")
    assert rs.load_table(p) is None
    p.write_text(json.dumps({"version": 2}), encoding="utf-8")
    assert rs.load_table(p) is None
    p.write_text(json.dumps(table()), encoding="utf-8")
    assert rs.load_table(p)["generated"] == "2026-10-16"
    t2 = table()
    t2["generated"] = "2027-01-01"
    time.sleep(0.01)
    p.write_text(json.dumps(t2), encoding="utf-8")
    import os
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert rs.load_table(p)["generated"] == "2027-01-01"


def test_nothing_in_the_trading_path_reads_the_range_modules():
    """範圍帶只顯示：策略、風控、下單、券商模組不得引用。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("strategies", "core/broker.py", "core/risk.py", "core/manual_monitor.py", "core/shioaji_worker.py", "api/routes_order.py", "api/routes_strategy.py"):
        p = root / name
        files = list(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            if f.exists():
                src = f.read_text(encoding="utf-8")
                assert "range_suggest" not in src and "range_calibration" not in src, f
