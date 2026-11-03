"""價量分布（core/volume_profile.py，update.md 發現 77）：時段、合約、彙總、POC／價值區、前一個日盤、快取、API、靜態檢查。
資料庫一律用暫存的 ticks.db（和 core/tick_store 同樣的表與索引），不碰真的 data/。"""
from __future__ import annotations

import ast
import sqlite3
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from core import volume_profile as vp
from core.daytrade import TAIPEI

ROOT = Path(__file__).resolve().parent.parent
D = date(2026, 11, 3)            # 週二


def ts(d, hh, mm, ss=0.0):
    return datetime(d.year, d.month, d.day, hh, mm, int(ss), int((ss % 1) * 1e6), tzinfo=TAIPEI).timestamp()


def tk(code, d, hh, mm, price, vol, tt, ss=0.0):
    return (code, ts(d, hh, mm, ss), float(price), vol, tt)


def make_db(path, ticks):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE ticks (code TEXT NOT NULL, ts REAL NOT NULL, price REAL NOT NULL, volume INTEGER NOT NULL, tick_type INTEGER NOT NULL, total_volume INTEGER NOT NULL DEFAULT 0)")
    c.execute("CREATE INDEX idx_ticks_code_ts ON ticks(code, ts)")
    c.executemany("INSERT INTO ticks (code, ts, price, volume, tick_type) VALUES (?,?,?,?,?)", ticks)
    c.commit()
    c.close()
    return Path(path)


@pytest.fixture(autouse=True)
def fresh_cache():
    vp.clear_cache()
    yield
    vp.clear_cache()


# ── 時段與合約 ────────────────────────────────────────────────────

def test_window_and_label():
    a, b = vp.window(D, "day")
    assert (a, b) == (ts(D, 8, 45), ts(D, 13, 45, 1))
    assert ts(D, 13, 45, 0.5) < b                                                       # 收盤那一筆在 13:45:00 之後幾毫秒，要算進去
    n0, n1 = vp.window(D, "night")
    assert (n0, n1) == (ts(D, 15, 0), ts(D + timedelta(days=1), 5, 0, 1))
    assert vp.label(D, "day") == "11/3 日盤" and vp.label(date(2026, 12, 9), "night") == "12/9 夜盤"


@pytest.mark.parametrize("hh,mm,expect", [(0, 30, (date(2026, 11, 2), "night")), (8, 44, (date(2026, 11, 2), "night")), (8, 45, (D, "day")), (14, 59, (D, "day")),
                                          (15, 0, (D, "night")), (23, 59, (D, "night"))])
def test_current_session(hh, mm, expect):
    assert vp.current_session(datetime(2026, 11, 3, hh, mm, tzinfo=TAIPEI)) == expect


def test_sessions_back_and_boundary():
    assert vp.sessions_back((D, "day"), 4) == [(D, "day"), (date(2026, 11, 2), "night"), (date(2026, 11, 2), "day"), (date(2026, 11, 1), "night")]
    assert vp.sessions_back((D, "night"), 3) == [(D, "night"), (D, "day"), (date(2026, 11, 2), "night")]
    assert vp.next_boundary(ts(D, 10, 0)) == ts(D, 15, 0) and vp.next_boundary(ts(D, 15, 0)) == ts(D + timedelta(days=1), 8, 45)
    assert vp.next_boundary(ts(D, 23, 0)) == ts(D + timedelta(days=1), 8, 45) and vp.next_boundary(ts(D, 8, 44, 59)) == ts(D, 8, 45)


def test_candidate_codes():
    assert vp.candidate_codes("TMF", date(2026, 10, 27)) == ["TMFJ6", "TMFK6", "TMFL6", "TMFA7"]
    assert vp.candidate_codes("TXF", date(2026, 12, 9)) == ["TXFL6", "TXFA7", "TXFB7", "TXFC7"]
    assert vp.candidate_codes("MXF", date(2029, 11, 3))[-1] == "MXFB0"                   # 跨十年


# ── 價值區與價位（純函式、手算）──────────────────────────────────

@pytest.mark.parametrize("vols,ref,expect", [
    ([1, 2, 10, 6, 3, 1, 0, 0, 2], None, (2, 2, 4)),             # 總 25、要 17.5；POC 10，上面兩格 9 > 下面兩格 3 → 往上一次就夠
    ([5, 6, 10, 2, 1], None, (2, 0, 2)),                         # 總 24、要 16.8；下面兩格 11 > 上面 3 → 往下
    ([10, 1, 1, 1], None, (0, 0, 0)),                            # POC 一格就 ≥ 70%
    ([10] + [1] * 10, None, (0, 0, 4)),                          # POC 在最下面：只能往上，一次兩格
    ([1] * 10 + [10], None, (10, 6, 10)),                        # POC 在最上面：只能往下
    ([5, 1, 5], None, (0, 0, 2)),                                # 同量：取較低的
    ([5, 1, 5], 2, (2, 0, 2)),                                   # 同量：取離 ref 近的
    ([3, 3, 3, 3], 0, (0, 0, 2)),                                # 全部同量也不當掉
    ([1, 10, 1, 1], None, (1, 1, 1)),
    ([3, 3, 10, 3, 3], None, (2, 2, 4)),                         # 上面兩格與下面兩格同量：往上（總 22、要 15.4，一次就夠）
    ([4, 4, 10, 1, 1], None, (2, 0, 2)),                         # 總 20、要 14；下面兩格 8 > 上面 2 → 往下一次加兩格（10＋8＝18）；若只加一格 10＋4＝14 剛好夠 → 兩種做法結果不同
])
def test_value_area_by_hand(vols, ref, expect):
    assert vp.value_area(vols, vp.VA_FRAC, ref) == expect


def test_value_area_degenerate_and_exact_seventy_percent():
    assert vp.value_area([]) is None and vp.value_area([0, 0, 0]) is None
    lo, hi = vp.value_area([3, 7], 0.7)[1:]                       # 7/10 剛好 70%：不用再往外加
    assert (lo, hi) == (1, 1)
    assert vp.value_area([3, 6, 1], 0.7)[1:] == (0, 1)            # 6/10 不夠 → 加一格（下面只有一格）


def test_levels_convert_bins_to_prices_and_handle_gaps():
    rows = [{"price": 100, "total": 1}, {"price": 110, "total": 10}, {"price": 120, "total": 6}, {"price": 130, "total": 3}]
    lv = vp.levels(rows, 10)
    assert lv == {"poc": 115.0, "vah": 140, "val": 110}           # POC＝那格的中心；VAH＝最上一格的上緣；VAL＝最下一格的下緣
    gap = vp.levels([{"price": 100, "total": 5}, {"price": 130, "total": 5}], 10, ref_price=131)           # 中間兩格沒有量：照零算；同量取離 131 近的
    assert gap["poc"] == 135.0 and gap["val"] == 100 and gap["vah"] == 140
    assert vp.levels([], 10) is None and vp.levels([{"price": 100, "total": 0}], 10) is None


def test_to_rows_splits_sides_and_sorts():
    rows = vp.to_rows([(4446, 2, 6.0), (4444, 0, 3.0), (4444, 2, 4.0), (4445, 1, 2.0), (4445, 1, 1.0), (4445, 7, 9.0)], 10)
    assert [r["price"] for r in rows] == [44440, 44450, 44460]
    assert rows[0] == {"price": 44440, "buy": 0.0, "sell": 4.0, "other": 3.0, "total": 7.0}
    assert rows[1] == {"price": 44450, "buy": 3.0, "sell": 0.0, "other": 9.0, "total": 12.0}        # 未知的 tick_type 算「無法判定」


# ── 從資料庫算 ────────────────────────────────────────────────────

def day_ticks(d=D):
    return [
        tk("TMFK6", d, 8, 44, 44000, 99, 1, 59.0),                    # 盤前試算（08:44:59）：不算
        tk("TMFK6", d, 8, 45, 44444, 3, 0),                           # 開盤那一筆（無法判定）
        tk("TMFK6", d, 9, 0, 44450, 2, 1), tk("TMFK6", d, 9, 0, 44455, 1, 1, 1),
        tk("TMFK6", d, 9, 10, 44449, 4, 2), tk("TMFK6", d, 10, 0, 44460, 6, 2),
        tk("TMFK6", d, 11, 0, 44480, 0, 1),                           # 沒有成交量的報價更新：不算
        tk("TMFK6", d, 13, 45, 44470, 5, 1, 0.5),                     # 收盤那一筆
        tk("TMFK6", d, 13, 45, 44999, 50, 1, 2.0),                    # 收盤之後：不算
        tk("TMFK6", d, 13, 45, 44998, 7, 1, 1.0),                     # 剛好在 13:45:01（時段是左閉右開）：不算
        tk("TXFK6", d, 9, 0, 40000, 77, 1),                           # 別的商品：不算
    ]


def test_compute_one_session_by_hand(tmp_path):
    db = make_db(tmp_path / "t.db", day_ticks())
    r = vp.compute(db, "TMF", "day", 10, D, ts(D, 16, 0))
    assert r["code"] == "TMFK6" and r["label"] == "11/3 日盤" and r["complete"] is True and r["session"] == "day" and r["date"] == "2026-11-03"
    assert [(x["price"], x["buy"], x["sell"], x["other"], x["total"]) for x in r["rows"]] == [(44440, 0, 4, 3, 7), (44450, 3, 0, 0, 3), (44460, 0, 6, 0, 6), (44470, 5, 0, 0, 5)]
    assert (r["total"], r["buy"], r["sell"], r["other"]) == (21, 8, 10, 3)
    assert (r["poc"], r["val"], r["vah"]) == (44445.0, 44440, 44470)             # POC 那格 7；70% ＝ 14.7，往上加兩格（3＋6）到 16
    assert (r["open"], r["high"], r["low"], r["last"]) == (44444, 44470, 44444, 44470)
    assert r["prev"] is None and r["note"] is None and r["start"] == ts(D, 8, 45)


def test_complete_flag_and_session_not_started(tmp_path):
    db = make_db(tmp_path / "t.db", day_ticks())
    assert vp.compute(db, "TMF", "day", 10, D, ts(D, 10, 0))["complete"] is False        # 進行中
    assert vp.compute(db, "TMF", "day", 10, D, ts(D, 13, 45, 1))["complete"] is True


def test_front_contract_is_the_one_with_most_volume(tmp_path):
    t = day_ticks() + [tk("TMFL6", D, 9, 0, 44000, 500, 1), tk("TMFA7", D, 9, 0, 45000, 1, 1)]
    r = vp.compute(make_db(tmp_path / "t.db", t), "TMF", "day", 10, D, ts(D, 16, 0))
    assert r["code"] == "TMFL6" and r["total"] == 500                                   # 量最大的那一檔（不靠推算）
    t2 = day_ticks() + [tk("TMFJ6", D, 9, 0, 44000, 900, 1)]
    assert vp.compute(make_db(tmp_path / "t2.db", t2), "TMF", "day", 10, D, ts(D, 16, 0))["code"] == "TMFK6"     # 上個月的合約早就結算了，不是候選


def test_pbin_changes_resolution(tmp_path):
    db = make_db(tmp_path / "t.db", day_ticks())
    r5 = vp.compute(db, "TMF", "day", 5, D, ts(D, 16, 0))
    assert [x["price"] for x in r5["rows"]] == [44440, 44445, 44450, 44455, 44460, 44470] and r5["total"] == 21
    r100 = vp.compute(db, "TMF", "day", 100, D, ts(D, 16, 0))
    assert len(r100["rows"]) == 1 and r100["rows"][0]["price"] == 44400 and r100["poc"] == 44450.0 and (r100["val"], r100["vah"]) == (44400, 44500)


def prev_ticks(code="TMFK6", d=date(2026, 11, 2), vol1=10, vol2=5):
    return [tk(code, d, 9, 0, 45000, vol1, 1), tk(code, d, 10, 0, 45010, vol2, 2)]


def test_previous_day_session_levels_and_skipping_days_without_ticks(tmp_path):
    t = day_ticks() + prev_ticks(d=date(2026, 10, 30))                          # 前一個交易日是上週五（11/2 沒有成交＝假設休市）
    r = vp.compute(make_db(tmp_path / "t.db", t), "TMF", "day", 10, D, ts(D, 16, 0))
    p = r["prev"]
    assert p["date"] == "2026-10-30" and p["label"] == "10/30 日盤" and p["code"] == "TMFK6" and p["note"] is None
    assert (p["poc"], p["val"], p["vah"], p["total"]) == (45005.0, 45000, 45020, 15) and (p["high"], p["low"], p["last"]) == (45010, 45000, 45010)


def test_previous_for_night_session_is_the_same_days_day_session(tmp_path):
    t = day_ticks() + prev_ticks() + [tk("TMFK6", D, 15, 30, 44300, 8, 2), tk("TMFK6", D, 16, 0, 44310, 4, 1)]
    r = vp.compute(make_db(tmp_path / "t.db", t), "TMF", "night", 10, D, ts(D, 17, 0))
    assert r["label"] == "11/3 夜盤" and r["total"] == 12 and r["complete"] is False
    assert r["prev"]["date"] == "2026-11-03" and r["prev"]["total"] == 21               # 夜盤看的是同一天剛收的日盤
    r2 = vp.compute(make_db(tmp_path / "t2.db", day_ticks() + prev_ticks()), "TMF", "day", 10, D, ts(D, 16, 0))
    assert r2["prev"]["date"] == "2026-11-02"                                          # 日盤看的是前一個交易日


def test_previous_contract_handling_on_a_roll(tmp_path):
    settle, today = date(2026, 10, 21), date(2026, 10, 22)                           # 10/21 結算日：當天近月是 J6，隔天起是 K6
    thin = day_ticks(today) + prev_ticks("TMFJ6", settle, 100, 0) + prev_ticks("TMFK6", settle, 10, 5)             # K6 當天只有 J6 的 15%
    p = vp.compute(make_db(tmp_path / "a.db", thin), "TMF", "day", 10, today, ts(today, 16, 0))["prev"]
    assert p["code"] == "TMFJ6" and "換月" in p["note"] and "TMFJ6" in p["note"] and p["date"] == "2026-10-21"
    enough = day_ticks(today) + prev_ticks("TMFJ6", settle, 40, 0) + prev_ticks("TMFK6", settle, 10, 5)           # 15/40 ≥ 20%：沿用目前的 K6
    p2 = vp.compute(make_db(tmp_path / "b.db", enough), "TMF", "day", 10, today, ts(today, 16, 0))["prev"]
    assert p2["code"] == "TMFK6" and p2["note"] is None and p2["total"] == 15
    same = day_ticks(today) + prev_ticks("TMFK6", settle, 10, 5)
    assert vp.compute(make_db(tmp_path / "c.db", same), "TMF", "day", 10, today, ts(today, 16, 0))["prev"]["note"] is None       # 沒換月：不註明


def test_auto_picks_the_latest_started_session_with_ticks(tmp_path):
    fri, mon = date(2026, 10, 30), date(2026, 11, 2)
    t = prev_ticks(d=fri) + prev_ticks(d=mon) + [tk("TMFK6", mon, 15, 30, 44300, 8, 2)]
    db = make_db(tmp_path / "t.db", t)
    at = lambda d, hh, mm: vp.compute(db, "TMF", "auto", 10, None, ts(d, hh, mm))
    assert at(mon, 10, 0)["label"] == "11/2 日盤" and at(mon, 10, 0)["complete"] is False     # 日盤進行中（10:00）
    assert at(mon, 14, 0)["label"] == "11/2 日盤" and at(mon, 14, 0)["complete"] is True       # 過了 13:45 才算收完
    assert at(mon, 16, 0)["label"] == "11/2 夜盤" and at(mon, 16, 0)["complete"] is False
    assert at(D, 6, 0)["label"] == "11/2 夜盤" and at(D, 6, 0)["complete"] is True             # 開盤前：昨晚剛收的夜盤
    assert at(D, 10, 0)["label"] == "11/2 夜盤"                                               # 今天日盤沒有成交（假設休市）→ 往前找到最近有成交的
    sat = at(date(2026, 11, 7), 10, 0)
    assert sat["label"] == "11/2 夜盤"                                                        # 週末：一路往前找
    assert vp.compute(db, "TMF", "day", 10, None, ts(D, 16, 0))["label"] == "11/2 日盤"          # 指定日盤：不被夜盤搶走
    assert vp.compute(db, "TMF", "night", 10, None, ts(D, 16, 0))["label"] == "11/2 夜盤"
    assert vp.compute(db, "TMF", "auto", 10, mon, ts(D, 16, 0))["label"] == "11/2 日盤"        # 給日期但沒指定時段＝日盤
    assert vp.compute(db, "TMF", "night", 10, mon, ts(D, 16, 0))["label"] == "11/2 夜盤"
    assert vp.compute(db, "TMF", "day", 10, date(2026, 10, 31), ts(D, 16, 0))["rows"] == []   # 指定的那天沒成交：不亂往前找


def test_missing_database_and_no_ticks_never_raise(tmp_path):
    r = vp.compute(tmp_path / "nope.db", "TMF", "auto", 10, None, ts(D, 10, 0))
    assert r["rows"] == [] and r["code"] is None and "讀不到" in r["note"] and r["prev"] is None
    r2 = vp.compute(make_db(tmp_path / "e.db", []), "TMF", "auto", 10, None, ts(D, 10, 0))
    assert r2["rows"] == [] and "沒有找到" in r2["note"] and r2["poc"] is None
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all")
    assert vp.compute(bad, "TMF", "auto", 10, None, ts(D, 10, 0))["rows"] == []
    nt = tmp_path / "notable.db"
    sqlite3.connect(nt).close()
    assert "讀不到" in vp.compute(nt, "TMF", "auto", 10, None, ts(D, 10, 0))["note"]        # 有檔案但沒有 ticks 表


# ── 快取 ──────────────────────────────────────────────────────────

def test_cache_ttl_and_keys(monkeypatch, tmp_path):
    calls = []

    def fake(path, prefix, session, pbin, day, now_ts):
        calls.append((prefix, session, pbin, day))
        return {"complete": day is not None or session == "done", "rows": [1], "n": len(calls)}
    monkeypatch.setattr(vp, "compute", fake)
    db = tmp_path / "x.db"
    t0 = ts(D, 10, 0)
    a = vp.get_profile("TMF", "auto", 10, None, t0, db)
    assert vp.get_profile("TMF", "auto", 10, None, t0 + vp.TTL_LIVE - 1, db) is a and len(calls) == 1          # 進行中：10 秒內不重算
    assert vp.get_profile("TMF", "auto", 10, None, t0 + vp.TTL_LIVE + 1, db)["n"] == 2
    vp.get_profile("TXF", "auto", 10, None, t0, db)
    vp.get_profile("TMF", "day", 10, None, t0, db)
    vp.get_profile("TMF", "auto", 5, None, t0, db)
    assert len(calls) == 5                                                                                      # 商品、時段、格寬不同＝不同的快取
    vp.get_profile("TMF", "auto", 10, D, t0, db)
    assert vp.get_profile("TMF", "auto", 10, D, t0 + 3599, db)["n"] == 6 and len(calls) == 6                    # 指定日期（已收完）：1 小時
    assert vp.get_profile("TMF", "auto", 10, D, t0 + 3601, db)["n"] == 7
    vp.clear_cache()
    vp.get_profile("TMF", "auto", 10, None, t0, db)
    assert len(calls) == 8


def test_completed_current_session_is_cached_only_until_the_next_session_starts(monkeypatch, tmp_path):
    n = []
    monkeypatch.setattr(vp, "compute", lambda *a: n.append(1) or {"complete": True, "rows": [1]})
    db = tmp_path / "x.db"
    t0 = ts(D, 14, 30)                                                                                         # 日盤已收完；15:00 夜盤開始
    vp.get_profile("TMF", "auto", 10, None, t0, db)
    vp.get_profile("TMF", "auto", 10, None, t0 + 29 * 60, db)                                                  # 14:59：還是舊的
    assert len(n) == 1
    vp.get_profile("TMF", "auto", 10, None, ts(D, 15, 0, 1), db)                                               # 15:00:01：重算（不能還停在日盤）
    assert len(n) == 2
    monkeypatch.setattr(vp, "compute", lambda *a: n.append(1) or {"complete": True, "rows": []})              # 沒有資料的結果不要快取一小時
    vp.get_profile("TXF", "auto", 10, None, t0, db)
    vp.get_profile("TXF", "auto", 10, None, t0 + vp.TTL_LIVE + 1, db)
    assert len(n) == 4


def test_cache_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(vp, "compute", lambda *a: {"complete": False, "rows": [1]})
    for pb in range(1, 80):
        vp.get_profile("TMF", "auto", pb, None, 1000.0 + pb, tmp_path / "x.db")
    assert len(vp._cache) <= 64


# ── API ───────────────────────────────────────────────────────────

def test_route_validation_and_arguments(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    import api.routes_market as rm
    got = []
    monkeypatch.setattr(rm.volume_profile, "get_profile", lambda *a: got.append(a) or {"ok": True})
    monkeypatch.setattr(rm, "time", types.SimpleNamespace(time=lambda: 1234.5))
    c = TestClient(main.app)
    assert c.get("/api/market/profile").json() == {"ok": True}
    assert got[-1] == ("TMF", "auto", 10, None, 1234.5)
    assert c.get("/api/market/profile?code=txf&session=night&pbin=5&date=2026-11-03").status_code == 200
    assert got[-1] == ("TXF", "night", 5, date(2026, 11, 3), 1234.5)                                           # 代號不分大小寫
    for q in ("code=ABC", "session=week", "date=2026-13-40", "date=今天"):
        assert c.get(f"/api/market/profile?{q}").status_code == 400, q
    for q in ("pbin=0", "pbin=101", "pbin=x"):
        assert c.get(f"/api/market/profile?{q}").status_code == 422, q


def test_route_end_to_end_on_a_temp_database(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import main
    import api.routes_market as rm
    from core import tick_store
    monkeypatch.setattr(tick_store, "DB_PATH", make_db(tmp_path / "t.db", day_ticks() + prev_ticks()))
    monkeypatch.setattr(rm, "time", types.SimpleNamespace(time=lambda: ts(D, 16, 0, 5)))
    j = TestClient(main.app).get("/api/market/profile?code=TMF&session=day").json()
    assert j["label"] == "11/3 日盤" and j["poc"] == 44445.0 and j["prev"]["label"] == "11/2 日盤" and j["prev"]["poc"] == 45005.0
    assert [r["price"] for r in j["rows"]] == [44440, 44450, 44460, 44470]


# ── 純顯示：策略、風控、下單模組不得引用 ──────────────────────────

def test_not_imported_by_trading_modules():
    allowed = {"volume_profile.py", "volume_profile_study.py", "routes_market.py"}
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name in allowed:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [x.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for x in n.names]
            assert "volume_profile" not in " ".join(names), p
    main_src = (ROOT / "api" / "routes_order.py").read_text(encoding="utf-8")
    assert "volume_profile" not in main_src
