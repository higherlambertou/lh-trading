"""〈資訊〉面板測試用的 intel.db：schema 是 lh-intel 契約（SCHEMA.md／intel/db.py）的複本；另有測試在 lh-intel 就在旁邊時，用它真正的 schema 與寫入函式對照。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE observations(id INTEGER PRIMARY KEY, source TEXT NOT NULL, series TEXT NOT NULL, event_date TEXT, event_time REAL, value REAL, value_text TEXT, unit TEXT,
  published_at REAL, fetched_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0, backfilled INTEGER NOT NULL DEFAULT 0, missing_reason TEXT, detail_json TEXT, raw_id INTEGER);
CREATE TABLE events(id INTEGER PRIMARY KEY, uid TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, kind TEXT NOT NULL, country TEXT, title TEXT NOT NULL,
  scheduled_time REAL, scheduled_date TEXT, importance INTEGER, status TEXT NOT NULL, actual TEXT, forecast TEXT, previous TEXT, url TEXT, published_at REAL, fetched_at REAL NOT NULL,
  backfilled INTEGER NOT NULL DEFAULT 0, missing_reason TEXT, detail_json TEXT, raw_id INTEGER);
CREATE TABLE news(id INTEGER PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL, url_canonical TEXT NOT NULL, title TEXT NOT NULL, title_hash TEXT NOT NULL, published_at REAL,
  fetched_at REAL NOT NULL, language TEXT, dup_of INTEGER, detail_json TEXT);
CREATE TABLE collector_runs(id INTEGER PRIMARY KEY, collector TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, status TEXT, new_rows INTEGER NOT NULL DEFAULT 0, error TEXT, detail_json TEXT);
"""


def make_db(path: Path, version: int = 1) -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    c.execute("INSERT INTO schema_meta VALUES('schema_version', ?)", (str(version),))
    c.commit()
    return c


def obs(c, source: str, series: str, d: str, value: Optional[float], fetched_at: float, *, revision: int = 0, backfilled: int = 0, published_at: Optional[float] = None) -> None:
    c.execute("INSERT INTO observations(source, series, event_date, value, fetched_at, revision, backfilled, published_at, missing_reason) VALUES(?,?,?,?,?,?,?,?,?)",
              (source, series, d, value, fetched_at, revision, backfilled, published_at, None if value is not None else "blank_in_source"))
    c.commit()


def event(c, uid: str, title: str, *, when: Optional[float] = None, date: str = "2026-10-29", importance: int = 3, kind: str = "macro", status: str = "scheduled", revision: int = 0,
          fetched_at: float = 1.0, source: str = "fed", country: str = "US", detail: Optional[dict[str, Any]] = None, backfilled: int = 0, published_at: Optional[float] = None) -> None:
    c.execute("INSERT INTO events(uid, revision, source, kind, country, title, scheduled_time, scheduled_date, importance, status, fetched_at, detail_json, backfilled, published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, revision, source, kind, country, title, when, date, importance, status, fetched_at, json.dumps(detail) if detail else None, backfilled, published_at))
    c.commit()


def headline(c, source: str, title: str, *, published_at: Optional[float], fetched_at: float, dup_of: Optional[int] = None, publisher: Optional[str] = None, url: str = "") -> int:
    cur = c.execute("INSERT INTO news(source, url, url_canonical, title, title_hash, published_at, fetched_at, dup_of, detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (source, url or f"https://x.example/{title}", f"https://x.example/{source}/{title}", title, title, published_at, fetched_at, dup_of, json.dumps({"publisher": publisher}) if publisher else None))
    c.commit()
    return int(cur.lastrowid)


def run(c, name: str, start: float, status: Optional[str]) -> None:
    c.execute("INSERT INTO collector_runs(collector, started_at, finished_at, status) VALUES(?,?,?,?)", (name, start, None if status is None else start + 1, status))
    c.commit()
