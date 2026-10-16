"""進場市場狀態紀錄 2026-10-16 版：內外盤快照（面板同一組數字、累計起點、缺值寫原因）與手動單的主觀趨勢判斷標籤（進場後補填、只新增不改寫、事後標記要標出來）。
只寫入不被讀取：沒有策略或風控模組讀這些欄位；寫入失敗不擋下單。"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

import core.entry_log as el
import core.trade_log as tl
from core.entry_log import EntrySync, capture, fetch_entries, to_csv
from core.live_state import LiveState
from test_entry_log import FULL_TAG, T0, add_deal, add_order, new_sync, rows

SNAP = {"session_flow": {"inside": 60260, "outside": 62902, "session": "night", "share": 0.511, "ts": T0 - 2},
        "flow": {"20": {"vol_share": 0.52, "n": 20}, "100": {"vol_share": 0.62, "n": 100}, "300": {"vol_share": 0.57, "n": 300}},
        "xc": {"windows": {"5": {"TMF": {"share": 0.47, "n": 120}, "TXF": {"share": 0.38, "n": 80}}, "15": {"TMF": {"share": 0.47, "n": 300}},
                           "30": {"TMF": {"share": None, "n": 4}}}}}


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setattr(el, "flow_provider", lambda: SNAP)
    el.alerts.count, el.alerts.last = 0, None


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(tl._SCHEMA)
    tl.TradeLog._migrate(c)
    c.commit()
    yield c
    c.close()


def entry(conn, tid="E1", strategy="manual", **kw):
    add_order(conn, tid, strategy=strategy, **kw)
    add_deal(conn, tid, 48981.0, 1)
    return tid


# ── 內外盤快照 ───────────────────────────────────────────────

def test_flow_view_records_what_the_panel_shows():
    f, why = el.flow_view(SNAP)
    assert why is None
    assert (f["inside_volume"], f["outside_volume"], f["net_diff"], f["ratio"]) == (60260, 62902, 2642, 0.511)
    assert f["accum_session"] == "night" and f["accum_start"] == "夜盤 15:00 重置" and f["state_timestamp"] == T0 - 2 and "交易所" in f["source"]
    assert f["recent"]["trades100"] == {"vol_share": 0.62, "n": 100} and f["recent"]["min5"] == {"vol_share": 0.47, "n": 120}
    assert f["recent"]["min30"] == {"vol_share": None, "n": 4} and set(f["recent"]) == {"trades20", "trades100", "trades300", "min5", "min15", "min30"}
    d, _ = el.flow_view({"session_flow": {**SNAP["session_flow"], "session": "day"}})
    assert d["accum_start"] == "日盤 08:45 重置" and d["accum_session"] == "day"


def test_flow_numbers_equal_the_panels_for_the_same_quote():
    """同一時刻兩邊數字相同：用真的 LiveState 吃一筆帶累計內外盤的報價，面板的 session_flow 與快照的欄位要一致。"""
    live = LiveState()
    ts = time.mktime((2026, 10, 14, 10, 0, 0, 0, 0, -1))
    live.feed("TMFJ6", 47000.0, 1, 1, 1, ts, quote={"bid_side_total_vol": 1234, "ask_side_total_vol": 2345, "avg_price": 47010.0})
    snap = live.snapshot()
    f, why = el.flow_view(snap)
    sf = snap["session_flow"]
    assert why is None and f["inside_volume"] == sf["inside"] and f["outside_volume"] == sf["outside"] and f["ratio"] == sf["share"]
    assert f["net_diff"] == sf["outside"] - sf["inside"] and f["accum_session"] == "day" and f["state_timestamp"] == ts


@pytest.mark.parametrize("snap,needle", [(None, "沒有提供"), ({}, "沒有提供"), ({"session_flow": None}, "尚未收到"),
                                         ({"session_flow": {"inside": 1, "outside": None, "session": "day"}}, "不完整")])
def test_missing_flow_is_null_with_a_reason_never_zero(snap, needle):
    f, why = el.flow_view(snap)
    assert f is None and needle in why


def test_the_snapshot_row_carries_the_flow_columns(conn):
    s = new_sync(conn)
    entry(conn)
    s.step()
    (r,) = rows(conn, "entry_snapshots")
    assert (r["flow_inside"], r["flow_outside"], r["flow_net_diff"], r["flow_ratio"]) == (60260, 62902, 2642, 0.511)
    assert r["flow_accum_session"] == "night" and r["flow_accum_start"] == "夜盤 15:00 重置" and r["flow_state_ts"] == T0 - 2
    assert json.loads(r["flow_recent"])["trades100"]["vol_share"] == 0.62 and r["flow_missing_reason"] is None
    assert r["missing_reason"] is None                                               # 市場狀態那一欄不受影響


def test_first_trade_after_a_reset_and_the_day_night_boundary(monkeypatch, conn):
    """累計值重置之後的第一筆（量很小）原樣記；日盤與夜盤各自帶自己的起點。"""
    s = new_sync(conn)
    monkeypatch.setattr(el, "flow_provider", lambda: {"session_flow": {"inside": 3, "outside": 5, "session": "night", "share": 0.625, "ts": T0}})
    entry(conn, "N1", ts=T0)
    monkeypatch.setattr(el, "flow_provider", lambda: {"session_flow": {"inside": 90000, "outside": 80000, "session": "day", "share": 0.47, "ts": T0 + 60}})
    entry(conn, "D1", ts=T0 + 60)
    s.step()
    by = {r["trade_id"]: r for r in rows(conn, "entry_snapshots")}
    assert (by["N1"]["flow_inside"], by["N1"]["flow_outside"], by["N1"]["flow_accum_start"]) == (3, 5, "夜盤 15:00 重置")
    assert (by["D1"]["flow_net_diff"], by["D1"]["flow_accum_start"]) == (-10000, "日盤 08:45 重置")


def test_a_broken_or_missing_provider_never_blocks_and_leaves_null_with_a_reason(monkeypatch, conn):
    def boom():
        raise RuntimeError("live_state 壞了")
    monkeypatch.setattr(el, "flow_provider", boom)
    cap = capture(FULL_TAG, {})                                                       # 不丟例外（不能擋下單）
    assert cap["order_flow"] is None and "讀取即時狀態失敗" in cap["flow_missing_reason"]
    s = new_sync(conn)
    entry(conn, "E1")
    s.step()
    (r,) = rows(conn, "entry_snapshots")
    assert r["flow_inside"] is None and r["flow_ratio"] is None and "讀取即時狀態失敗" in r["flow_missing_reason"] and r["hurst_value"] == 0.36
    monkeypatch.setattr(el, "flow_provider", None)
    assert "沒有提供" in capture(FULL_TAG, {})["flow_missing_reason"]


def test_orders_placed_before_the_feature_get_a_reason_not_zeros(conn):
    s = new_sync(conn)
    old = {k: v for k, v in capture(FULL_TAG, {}).items() if k not in ("order_flow", "flow_missing_reason")}
    conn.execute("INSERT INTO orders(ts, mode, trade_id, strategy, reason, contract, action, qty, signal_price, state_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (T0, "sim", "OLD", "manual", "entry", "TMF", "Buy", 1, 100.0, json.dumps(old)))
    conn.commit()
    add_deal(conn, "OLD", 100.0, 1)
    s.step()
    (r,) = rows(conn, "entry_snapshots")
    assert r["flow_inside"] is None and "功能啟用前" in r["flow_missing_reason"]


def test_an_existing_database_gets_the_new_columns():
    c = sqlite3.connect(":memory:")
    c.executescript(tl._SCHEMA)
    tl.TradeLog._migrate(c)
    c.executescript("CREATE TABLE entry_snapshots(mode TEXT NOT NULL, trade_id TEXT NOT NULL, contract TEXT, strategy_name TEXT, strategy_type TEXT, direction TEXT, "
                    "order_ts REAL, entry_ts REAL, entry_time TEXT, entry_price REAL, signal_price REAL, stop_price REAL, lots INTEGER, hurst_value REAL, hurst_z REAL, "
                    "hurst_window INTEGER, hurst_bar_period TEXT, hurst_noise REAL, iv_value REAL, iv_percentile REAL, state_timestamp REAL, state_date TEXT, "
                    "market_state TEXT, missing_reason TEXT, strategy_params TEXT, created_ts REAL, PRIMARY KEY(mode, trade_id));")
    c.execute("INSERT INTO entry_snapshots(mode, trade_id) VALUES('sim','LEGACY')")
    c.commit()
    new_sync(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(entry_snapshots)")}
    assert {name for name, _ in el.FLOW_COLS} <= cols
    assert c.execute("SELECT flow_inside FROM entry_snapshots WHERE trade_id='LEGACY'").fetchone()[0] is None          # 舊列是 NULL，不是 0
    c.close()


# ── 趨勢判斷標籤 ─────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """檔案資料庫（add_label／fetch_entries 吃路徑）：一筆手動單、一筆策略單。"""
    p = tmp_path / "trade_log.db"
    c = sqlite3.connect(p)
    c.executescript(tl._SCHEMA)
    tl.TradeLog._migrate(c)
    c.commit()
    s = new_sync(c)
    entry(c, "M1", "manual", ts=T0)
    entry(c, "S1", "scalp", ts=T0 + 10)
    s.step()
    c.close()
    return p


def one(path, tid):
    return next(r for r in fetch_entries(path, "sim") if r["trade_id"] == tid)


def test_a_label_is_recorded_with_its_time_and_shown_on_the_entry(db):
    out = el.add_label(db, "sim", "M1", "multi", " 突破前高 ", now=T0 + 30)
    assert out["trend_label"] == "multi" and out["note"] == "突破前高" and out["label_after_exit"] is False and out["label_entered_at"].endswith("+08:00")
    r = one(db, "M1")
    assert (r["trend_label"], r["trend_note"], r["label_versions"], r["label_missing_reason"], r["label_after_exit"]) == ("multi", "突破前高", 1, None, False)
    assert r["label_entered_at"] == out["label_entered_at"]


def test_no_label_means_null_with_not_entered_and_strategy_orders_never_have_one(db):
    m, s = one(db, "M1"), one(db, "S1")
    assert (m["trend_label"], m["label_missing_reason"], m["label_after_exit"], m["label_versions"]) == (None, "not_entered", None, 0)
    assert (s["trend_label"], s["label_missing_reason"]) == (None, "strategy_order")


def test_label_validation(db):
    with pytest.raises(ValueError, match="趨勢判斷"):
        el.add_label(db, "sim", "M1", "up")
    with pytest.raises(ValueError, match="備註"):
        el.add_label(db, "sim", "M1", "range", "字" * 201)
    with pytest.raises(ValueError, match="只有手動單"):
        el.add_label(db, "sim", "S1", "multi")
    with pytest.raises(LookupError):
        el.add_label(db, "sim", "NOPE", "multi")
    assert one(db, "M1")["label_versions"] == 0 and one(db, "S1")["label_versions"] == 0               # 失敗的不留痕
    for t in el.LABELS:
        el.add_label(db, "sim", "M1", t)                                                                # 四個選項都收（含「無明確判斷」）
    assert one(db, "M1")["trend_label"] == "none" and one(db, "M1")["label_versions"] == 4


def test_labels_are_append_only_corrections_keep_the_history(db):
    el.add_label(db, "sim", "M1", "multi", "a", now=T0 + 20)
    el.add_label(db, "sim", "M1", "short", "b", now=T0 + 40)
    c = sqlite3.connect(db)
    labs = c.execute("SELECT trend_label, note FROM entry_labels ORDER BY id").fetchall()
    snap_before = c.execute("SELECT * FROM entry_snapshots ORDER BY trade_id").fetchall()
    c.close()
    assert labs == [("multi", "a"), ("short", "b")]                                                     # 舊的還在
    r = one(db, "M1")
    assert (r["trend_label"], r["trend_note"], r["label_versions"]) == ("short", "b", 2)                # 最新一筆是目前的
    c = sqlite3.connect(db)
    assert c.execute("SELECT * FROM entry_snapshots ORDER BY trade_id").fetchall() == snap_before        # 標籤不動進場快照
    c.close()


def test_a_label_entered_after_the_exit_is_flagged(db):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO exit_fills(fill_key, mode, trade_id, exit_ts, exit_price, lots, entry_price, pnl_points, pnl_ntd, exit_reason, created_ts) "
              "VALUES('x1','sim','M1',?,?,?,?,?,?,?,?)", (T0 + 100, 49001.0, 1, 48981.0, 20.0, 200.0, "take_profit", T0 + 100))
    c.commit()
    c.close()
    early = el.add_label(db, "sim", "M1", "multi", now=T0 + 50)                                         # 出場之前標的
    late = el.add_label(db, "sim", "M1", "multi", now=T0 + 101)                                         # 已經知道結果才標的
    assert early["label_after_exit"] is False and late["label_after_exit"] is True
    assert one(db, "M1")["label_after_exit"] is True                                                    # 目前的標籤是後來標的 → 可信度打折
    el.add_label(db, "sim", "M1", "multi", now=T0 + 60)                                                 # 再補一筆（時間在出場之前）→ 目前的標籤變回出場前標的
    assert one(db, "M1")["label_after_exit"] is False


def test_csv_has_the_new_columns_and_nulls_are_empty(db):
    el.add_label(db, "sim", "M1", "range", "盤整", now=T0 + 20)
    import csv, io
    text = to_csv(fetch_entries(db, "sim"))
    rd = list(csv.DictReader(io.StringIO(text)))
    for col in ("flow_inside", "flow_net_diff", "flow_accum_start", "flow_recent", "trend_label", "label_entered_at", "label_after_exit", "label_missing_reason"):
        assert col in rd[0]
    m = next(r for r in rd if r["trade_id"] == "M1")
    s = next(r for r in rd if r["trade_id"] == "S1")
    assert m["trend_label"] == "range" and m["flow_net_diff"] == "2642" and m["flow_accum_start"] == "夜盤 15:00 重置" and m["label_entered_at"].endswith("+08:00")
    assert s["trend_label"] == "" and s["label_missing_reason"] == "strategy_order" and s["flow_net_diff"] == "2642"


def test_route_maps_errors_to_http_codes(db, monkeypatch):
    import api.routes_tradelog as rt
    monkeypatch.setattr(rt.trade_log, "path", db)
    monkeypatch.setattr(rt, "current_mode", lambda: "sim")
    ok = rt.post_label("M1", rt.LabelRequest(trend="multi", note="x"))
    assert ok["trend_label"] == "multi"
    for tid, trend, code in (("NOPE", "multi", 404), ("S1", "multi", 422), ("M1", "bogus", 422)):
        with pytest.raises(rt.HTTPException) as e:
            rt.post_label(tid, rt.LabelRequest(trend=trend))
        assert e.value.status_code == code, (tid, trend)


# ── 沒有任何策略或風控讀這些欄位；標籤不碰下單 ────────────────────

NEW_WORDS = ("order_flow", "entry_labels", "trend_label", "flow_provider", "flow_view", "add_label", "label_after_exit", "flow_inside", "flow_ratio")


def test_nothing_that_trades_reads_the_new_fields():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    files = sorted((root / "strategies").glob("*.py")) + [root / "api" / "routes_strategy.py", root / "api" / "routes_order.py", root / "core" / "manual_monitor.py",
                                                         root / "core" / "ruin.py", root / "api" / "routes_risk.py", root / "core" / "broker_guard.py", root / "core" / "broker.py"]
    offenders = [f"{f.name}: {w}" for f in files for w in NEW_WORDS if w in f.read_text(encoding="utf-8")]
    assert len(files) >= 14 and not offenders, offenders


def test_the_only_writer_of_labels_is_the_label_route_and_it_never_places_orders():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    src = (root / "api" / "routes_tradelog.py").read_text(encoding="utf-8")
    assert "place_order" not in src and "broker" not in src


def test_a_label_between_two_partial_exits_counts_as_after_the_first_exit(db):
    c = sqlite3.connect(db)
    for i, t in enumerate((T0 + 100, T0 + 200)):
        c.execute("INSERT INTO exit_fills(fill_key, mode, trade_id, exit_ts, exit_price, lots, entry_price, pnl_points, pnl_ntd, exit_reason, created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (f"p{i}", "sim", "M1", t, 49000.0, 1, 48981.0, 19.0, 190.0, "take_profit", t))
    c.commit()
    c.close()
    el.add_label(db, "sim", "M1", "multi", now=T0 + 150)                                                # 第一次出場之後、最後一次出場之前
    assert one(db, "M1")["label_after_exit"] is True                                                    # 已經看到一部分結果了


def test_order_source_separates_manual_from_strategy_and_manual_has_no_strategy_name_or_type(db, tmp_path):
    m, s = one(db, "M1"), one(db, "S1")
    assert (m["order_source"], m["strategy_name"], m["strategy_type"]) == ("manual", None, None)          # 手動單：名稱與類型是 null，不填預設策略
    assert (s["order_source"], s["strategy_name"], s["strategy_type"]) == ("strategy", "scalp", "other")
    out = to_csv(fetch_entries(db, "sim"))
    head = out.splitlines()[0].split(",")
    assert "order_source" in head and head.index("order_source") < head.index("strategy_name")
    c = sqlite3.connect(db)                                                                              # 資料庫裡仍是原始值（只在讀出時換成 null）
    assert c.execute("SELECT strategy_name FROM entry_snapshots WHERE trade_id='M1'").fetchone()[0] == "manual"
    c.close()
    for name in ("manual_monitor", ""):                                                                  # 停損監看平倉單、沒有脈絡的單也算手動
        p = tmp_path / f"{name or 'blank'}.db"
        c = sqlite3.connect(p)
        c.executescript(tl._SCHEMA)
        tl.TradeLog._migrate(c)
        c.commit()
        sy = new_sync(c)
        entry(c, "Z1", name, ts=T0)
        sy.step()
        c.close()
        z = one(p, "Z1")
        assert (z["order_source"], z["strategy_name"], z["strategy_type"], z["label_missing_reason"]) == ("manual", None, None, "not_entered")


def test_a_snapshot_written_before_the_upgrade_still_reads_back_with_a_reason(db):
    c = sqlite3.connect(db)
    c.execute("UPDATE entry_snapshots SET flow_inside=NULL, flow_outside=NULL, flow_net_diff=NULL, flow_ratio=NULL, flow_missing_reason=NULL WHERE trade_id='M1'")   # 升級前寫好的列
    c.commit()
    c.close()
    r = one(db, "M1")
    assert r["flow_inside"] is None and r["flow_missing_reason"] == el.NO_FLOW_REASON                      # null 一定有原因
    assert one(db, "S1")["flow_missing_reason"] is None and one(db, "S1")["flow_ratio"] is not None        # 有值的不會被補原因
