"""ticks.db 瘦身：只存真實成交、舊資料庫就地升級、compact 預覽/執行。"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest

import core.quote_hub as qh
import core.tick_store as ts


def rows(path):
    with closing(sqlite3.connect(path)) as c:
        return c.execute("SELECT code, price, volume, tick_type, total_volume FROM ticks ORDER BY ts").fetchall()


def make_ticks(path, with_total_volume=True):
    """建一個空的 ticks 表（可選舊 schema：沒有 total_volume）。"""
    extra = ", total_volume INTEGER NOT NULL DEFAULT 0" if with_total_volume else ""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE ticks (code TEXT NOT NULL, ts REAL NOT NULL, price REAL NOT NULL, "
              f"volume INTEGER NOT NULL, tick_type INTEGER NOT NULL{extra})")
    return c


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "DB_PATH", tmp_path / "t.db")
    monkeypatch.delenv("RECORD_QUOTE_UPDATES", raising=False)
    return tmp_path / "t.db"


def feed(rec):
    t = time.time()
    rec.record("TMFJ6", t, 100.0, 1, 1, 10)             # 成交
    rec.record("TMFJ6", t + 0.1, 100.0, 0, 1, 10)       # 報價更新（volume=0，帶著上一筆的方向）
    rec.record("TMFJ6", t + 0.2, 100.0, 1, 1, 10)       # 重複回報（total_volume 沒增加）
    rec.record("TMFJ6", t + 0.3, 101.0, 2, 2, 12)       # 成交
    rec.record("MXFJ6", t + 0.4, 101.0, 1, 2, 5)        # 別的合約的成交


def test_recorder_keeps_only_real_trades_and_stores_total_volume(db):
    rec = ts.TickRecorder()
    rec.start()
    feed(rec)
    rec.stop()
    assert rows(db) == [("TMFJ6", 100.0, 1, 1, 10), ("TMFJ6", 101.0, 2, 2, 12), ("MXFJ6", 101.0, 1, 2, 5)]


def test_quote_updates_can_still_be_recorded_when_asked(db, monkeypatch):
    monkeypatch.setenv("RECORD_QUOTE_UPDATES", "true")
    rec = ts.TickRecorder()
    rec.start()
    feed(rec)
    rec.stop()
    assert len(rows(db)) == 5


def test_old_database_is_upgraded_in_place(db):
    c = make_ticks(db, with_total_volume=False)                              # 舊 schema：沒有 total_volume
    c.execute("INSERT INTO ticks VALUES ('TMFJ6', 1.0, 99.0, 3, 1)")
    c.commit()
    c.close()
    rec = ts.TickRecorder()
    rec.start()
    rec.record("TMFJ6", time.time(), 100.0, 1, 1, 10)
    rec.stop()
    assert rows(db) == [("TMFJ6", 99.0, 3, 1, 0), ("TMFJ6", 100.0, 1, 1, 10)]   # 舊資料保留、total_volume 補 0


def test_compact_previews_then_applies(db):
    now = time.time()
    c = make_ticks(db)
    c.executemany("INSERT INTO ticks VALUES ('TMFJ6', ?, 100.0, 0, 1, 0)", [(now - i,) for i in range(3000)])     # 報價更新
    c.executemany("INSERT INTO ticks VALUES ('TMFJ6', ?, 100.0, 1, 1, 0)", [(now - i,) for i in range(300)])       # 新成交
    c.executemany("INSERT INTO ticks VALUES ('TMFJ6', ?, 100.0, 1, 1, 0)", [(now - 40 * 86400 - i,) for i in range(100)])  # 老成交
    c.commit()
    c.close()
    size0 = db.stat().st_size
    prev = ts.compact(db, keep_days=30)
    assert prev["applied"] is False and prev["quote_update_rows"] == 3000 and prev["old_trade_rows"] == 100
    assert db.stat().st_size == size0 and len(rows(db)) == 3400                       # 預覽不改任何東西

    only_quotes = ts.compact(db, apply=True)                                          # 不給 keep_days：只清報價更新
    assert only_quotes["size_after"] < size0 and len(rows(db)) == 400
    ts.compact(db, keep_days=30, apply=True)
    assert len(rows(db)) == 300                                                       # 再把 30 天前的成交清掉


def test_quote_hub_hands_total_volume_to_the_recorder(monkeypatch):
    seen = []

    class Fake:
        def record(self, *a):
            seen.append(a)

    monkeypatch.setattr(qh, "tick_recorder", Fake())
    hub = qh.QuoteHub()
    hub._inject_quote({"code": "TMFJ6", "close": 100.0, "volume": 2, "total_volume": 77, "tick_type": 1, "ts": 5.0})
    assert seen == [("TMFJ6", 5.0, 100.0, 2, 1, 77)]
