"""GET /api/intel/summary：永遠回 200、資料庫不在也不壞、不在事件迴圈上做同步讀取。"""
from __future__ import annotations

import asyncio
import json
import threading

import api.routes_intel as ri
import core.intel_reader as ir
import main
from intel_fixture import make_db, obs


def test_the_route_returns_the_reader_summary_in_a_thread(monkeypatch):
    seen = {}

    def fake():
        seen["thread"] = threading.current_thread() is not threading.main_thread()
        return {"available": True, "as_of": 1.0}
    monkeypatch.setattr(ri.intel_reader, "summary", fake)
    assert asyncio.run(ri.get_summary()) == {"available": True, "as_of": 1.0} and seen["thread"] is True                  # 同步的 SQLite 讀取丟 executor，不佔 event loop


def test_the_route_reads_the_database_named_by_intel_db_and_never_raises(monkeypatch, tmp_path):
    ir._cache.update(key=None, at=0.0, data=None)
    monkeypatch.setenv("INTEL_DB", str(tmp_path / "nope.db"))
    out = asyncio.run(ri.get_summary())
    assert out["available"] is False and "找不到資料庫" in out["reason"]
    c = make_db(tmp_path / "intel.db")
    obs(c, "cboe", "VIX.close", "2026-10-16", 19.99, 1.0)
    c.close()
    monkeypatch.setenv("INTEL_DB", str(tmp_path / "intel.db"))
    ir._cache.update(key=None, at=0.0, data=None)
    out = asyncio.run(ri.get_summary())
    assert out["available"] is True and {m["key"]: m for m in out["markets"]}["VIX"]["value"] == 19.99
    json.dumps(out, allow_nan=False)
    ir._cache.update(key=None, at=0.0, data=None)


def test_the_route_is_mounted_on_the_app_under_api_intel():
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert "/api/intel/summary" in paths
