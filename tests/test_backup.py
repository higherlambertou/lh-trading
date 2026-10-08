"""資料庫自動備份（core/backup.py）與每日排程。"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date, datetime

import pytest

import core.daily_summary as ds
from core.backup import _prune, backup_databases
from core.daily_summary import Config, MarketStateService
from core.market_store import MarketStore
from core.trade_log import TradeLog


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    store = MarketStore(d / "market_state.db")
    for i in range(30):
        store.upsert_iv(f"2026-09-{i + 1:02d}", 15.0 + i * 0.1, "csv")
    t = TradeLog(d / "trade_log.db")
    t.start()
    t.record_order(contract="TMF", action="Buy", qty=1, trade_id="A", status="PendingSubmit")
    t.stop()
    return d


def count(path, table):
    with closing(sqlite3.connect(path)) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_backup_copies_everything_and_verifies(data_dir, tmp_path):
    root = tmp_path / "bk"
    r = backup_databases("2026-10-08", data_dir=data_dir, dest_root=root)
    assert [f["file"] for f in r["files"]] == ["market_state.db", "trade_log.db"] and all(f["ok"] for f in r["files"])
    d = root / "2026-10-08"
    assert count(d / "market_state.db", "iv_history") == 30 and count(d / "trade_log.db", "orders") == 1
    assert sorted(p.name for p in d.iterdir()) == ["market_state.db", "trade_log.db"]      # 沒有留下 .tmp 或 -wal/-shm
    with closing(sqlite3.connect(d / "market_state.db")) as c:                             # 來源是 WAL，副本要是獨立單檔
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_backup_is_consistent_while_a_writer_is_active(data_dir, tmp_path):
    stop = threading.Event()
    wrote = []

    def writer():
        c = sqlite3.connect(data_dir / "market_state.db", timeout=10)
        i = 0
        while not stop.is_set():
            c.execute("INSERT OR REPLACE INTO iv_history VALUES (?,?,?,?,?)", (f"2027-01-{i % 28 + 1:02d}-{i}", 20.0, "csv", "", time.time()))
            c.commit()
            i += 1
            wrote.append(i)
        c.close()

    th = threading.Thread(target=writer)
    th.start()
    time.sleep(0.2)
    try:
        r = backup_databases("2026-10-08", data_dir=data_dir, dest_root=tmp_path / "bk")
    finally:
        stop.set()
        th.join()
    assert r["files"][0]["ok"]
    n = count(tmp_path / "bk" / "2026-10-08" / "market_state.db", "iv_history")
    assert 30 <= n <= 30 + len(wrote)                                                      # 是某個一致的快照


def test_backup_skips_missing_files_and_reruns_overwrite(data_dir, tmp_path):
    (data_dir / "trade_log.db").unlink()
    for ext in ("-wal", "-shm"):
        (data_dir / f"trade_log.db{ext}").unlink(missing_ok=True)
    r1 = backup_databases("2026-10-08", data_dir=data_dir, dest_root=tmp_path / "bk")
    r2 = backup_databases("2026-10-08", data_dir=data_dir, dest_root=tmp_path / "bk")
    assert [f["file"] for f in r1["files"]] == ["market_state.db"] == [f["file"] for f in r2["files"]]


def test_prune_only_removes_old_date_folders(tmp_path):
    for name in ("2026-09-01", "2026-09-23", "2026-09-24", "2026-10-08", "notes", "2026-13-99"):
        (tmp_path / name).mkdir()
    assert _prune(tmp_path, keep_days=14, today="2026-10-08") == ["2026-09-01", "2026-09-23"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-24", "2026-10-08", "2026-13-99", "notes"]


# ── 每日排程 ──────────────────────────────────────────────────────

def freeze(monkeypatch, when: datetime):
    class _D(date):
        @classmethod
        def today(cls):
            return when.date()

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(ds, "date", _D)
    monkeypatch.setattr(ds, "datetime", _DT)


class NoBroker:
    is_connected = False


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    s = MarketStateService(MarketStore(tmp_path / "m.db"), Config(iv_auto=False, backup=True, backup_hhmm=1400), NoBroker())
    s._pre_done = s._post_done = "2026-10-08"                      # 只測備份這一支排程
    return s


def test_backup_runs_once_after_the_scheduled_time(svc, monkeypatch):
    calls = []
    monkeypatch.setattr(ds, "backup_databases", lambda day, keep_days: calls.append((day, keep_days)) or {"dir": "x", "files": [{"file": "a", "ok": True, "bytes": 2048}]})
    freeze(monkeypatch, datetime(2026, 10, 8, 13, 59))
    asyncio.run(svc.tick({}))
    assert calls == []                                              # 還沒到時間
    freeze(monkeypatch, datetime(2026, 10, 8, 14, 5))
    asyncio.run(svc.tick({}))
    asyncio.run(svc.tick({}))
    assert calls == [("2026-10-08", 14)]                            # 當天只備份一次


def test_backup_skips_weekends_and_can_be_disabled(svc, monkeypatch):
    calls = []
    monkeypatch.setattr(ds, "backup_databases", lambda day, keep_days: calls.append(day) or {"dir": "x", "files": []})
    freeze(monkeypatch, datetime(2026, 10, 10, 15, 0))              # 週六
    asyncio.run(svc.tick({}))
    assert calls == []
    svc.cfg = Config(iv_auto=False, backup=False)
    freeze(monkeypatch, datetime(2026, 10, 8, 15, 0))
    asyncio.run(svc.tick({}))
    assert calls == []


def test_failed_backup_is_retried_later_not_every_tick(svc, monkeypatch):
    calls = []

    def flaky(day, keep_days):
        calls.append(day)
        if len(calls) == 1:
            raise OSError("disk full")
        return {"dir": "x", "files": [{"file": "a", "ok": True, "bytes": 1024}]}

    monkeypatch.setattr(ds, "backup_databases", flaky)
    freeze(monkeypatch, datetime(2026, 10, 8, 14, 5))
    asyncio.run(svc.tick({}))
    asyncio.run(svc.tick({}))
    assert len(calls) == 1 and svc._backup_done == ""               # 失敗後冷卻中，不會每 30 秒重試
    svc._retry_at["backup"] = 0                                     # 冷卻結束
    asyncio.run(svc.tick({}))
    assert len(calls) == 2 and svc._backup_done == "2026-10-08"


def test_partial_failure_counts_as_failure(svc, monkeypatch):
    monkeypatch.setattr(ds, "backup_databases", lambda day, keep_days: {"dir": "x", "files": [{"file": "a", "ok": False, "error": "x"}]})
    freeze(monkeypatch, datetime(2026, 10, 8, 14, 5))
    asyncio.run(svc.tick({}))
    assert svc._backup_done == "" and svc._retry_at["backup"] > 0
