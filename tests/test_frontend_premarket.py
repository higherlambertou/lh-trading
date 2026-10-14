"""〈盤前跨市場快照〉面板：格式化、缺值原因、檢視元件、容器的載入／錯誤／空白狀態。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/premarket_check.js，react-dom/server 渲染、hooks 換成替身）。
畫面吃的資料由這裡用 core/premarket_log.py 現算（真的跑一遍：日曆、來源替身、ticks.db、只新增的資料庫、更正與作廢、補記、預覽），
所以後端欄位改了、前端沒跟著改，這個測試會抓到。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path

import pytest

import core.premarket_log as pm

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "premarket_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


def _load_log_tests():
    """借用 test_premarket_log.py 的替身世界（日曆、Nasdaq／Cboe 替身、ticks.db 產生器）。"""
    spec = importlib.util.spec_from_file_location("premarket_log_tests", ROOT / "tests" / "test_premarket_log.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["premarket_log_tests"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_fixtures(d: Path) -> dict:
    t = _load_log_tests()
    w = t.World(d, prev=date(2026, 10, 13))
    store = w.store
    cal, http = w.cal, w.http

    def record(day: date, prev: date | None, hh=8, mm=30, ss=5):          # 平常是 T 之後幾秒內記下；補記的才晚很多
        db = d / f"ticks_{day}.db"
        if prev is not None:
            t.make_ticks(db, t.session_ticks(prev))
        res = pm.run_once(day, now=datetime.combine(day, dtime(hh, mm, ss), tzinfo=pm.TPE), store=store, cal=cal, ticks_path=db, http=http, writer="live", force=True)
        assert res["status"] == "written", res

    full = {k: list(v) for k, v in w.series.items()}

    def set_sources(last):                                  # 來源（Nasdaq、Cboe）的資料停在 last；None＝全部上線
        for k in ("sox", "tsm", "vix"):
            w.series[k] = [b for b in full[k] if last is None or b.date <= last]
        http.routes["/SOX/historical"], http.routes["/TSM/historical"], http.routes["VIX_History.csv"] = w.nasdaq("sox"), w.nasdaq("tsm"), w.cboe()

    set_sources(date(2026, 10, 5))                         # 2026-10-08：來源比 T 晚更新（還停在 10-05，要 10-07）→ 先帶空值寫入
    record(date(2026, 10, 8), date(2026, 10, 7))
    set_sources(None)                                      # 來源後來更新了 → 自動補值
    res = pm.backfill_once(date(2026, 10, 8), now=datetime(2026, 10, 8, 12, 16, tzinfo=pm.TPE), store=store, cal=cal, ticks_path=d / "ticks_2026-10-08.db", http=http)
    assert res["status"] == "filled" and res["filled"] == ["sox", "tsm", "vix"], res
    record(date(2026, 10, 12), None)                       # 連假後第一個交易日、本機沒有夜盤紀錄 → 夜盤欄位 null
    record(date(2026, 10, 13), date(2026, 10, 12))
    record(date(2026, 5, 26), date(2026, 5, 25))          # 美股休市（陣亡將士紀念日）→ 三個美股欄位 null
    record(date(2026, 10, 14), date(2026, 10, 13), 10, 12, 0)  # 後端 10:12 才啟動：補記
    record(date(2026, 10, 15), date(2026, 10, 14))
    store.add_correction(date(2026, 10, 15), {"vix": {"value": 15.5, "source": "手動", "missing_reason": None}}, "手動更正")
    record(date(2026, 10, 16), date(2026, 10, 15))
    store.add_void(date(2026, 10, 16), "颱風假，期交所休市")
    store.log_error("missed", "x", "2026-10-20")
    now = datetime(2026, 10, 16, 16, 30, tzinfo=pm.TPE)
    rows = store.effective()
    pv_same = pm.preview(date(2026, 10, 15), now=now, store=store, cal=cal, ticks_path=d / "ticks_2026-10-15.db", http=http, use_cache=False)      # 最新一筆有效紀錄的那天
    pv_voided = pm.preview(date(2026, 10, 16), now=now, store=store, cal=cal, ticks_path=d / "ticks_2026-10-16.db", http=http, use_cache=False)    # 已作廢的那天
    pv_other = pm.preview(date(2026, 10, 19), now=datetime(2026, 10, 19, 9, 0, tzinfo=pm.TPE), store=store, cal=cal, ticks_path=d / "ticks_2026-10-16.db", http=http, use_cache=False)
    status = pm.status(store, cal=cal, now=now)
    empty = pm.PremarketStore(d / "empty.db")
    status0 = pm.status(empty, cal=cal, now=datetime(2026, 10, 14, 7, 0, tzinfo=pm.TPE))
    pm.alerts.count, pm.alerts.last = 2, {"ts": 1.0, "where": "run", "trading_date": "2026-10-14", "error": "database is locked"}
    status_alert = pm.status(store, cal=cal, now=now)
    pm.alerts.count, pm.alerts.last = 0, None
    out = {"snapshots": rows, "status": status, "status_empty": status0, "status_disabled": {**status0, "enabled": False}, "status_alert": status_alert,
           "preview_same": pv_same, "preview_voided": pv_voided, "preview_other": pv_other}
    for name, body in out.items():
        (d / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return out


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_premarket_panel(tmp_path, monkeypatch):
    for k in ("PREMARKET_TIME", "PREMARKET_SOURCES", "PREMARKET_LOG"):
        monkeypatch.delenv(k, raising=False)
    pm._breaker.clear()
    pm._preview_cache.clear()
    make_fixtures(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["PM_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
