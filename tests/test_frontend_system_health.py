"""系統健康面板（發現 66）：用 core/system_health.py 現算的資料渲染（tests/frontend/system_health_check.js）。沒有 node 就略過。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_system_health import NOW, WD, make_backup, ts
from core import system_health as sh

ROOT = Path(__file__).resolve().parent.parent
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules")
def test_system_health_panel(tmp_path):
    (tmp_path / "wd.log").write_text(WD, encoding="utf-8")
    make_backup(tmp_path / "bk", "2026-10-22")
    good_bk = tmp_path / "bk2"
    make_backup(good_bk, "2026-10-22")
    (good_bk / "mirror_state.json").write_text(json.dumps({"last_attempt_day": "2026-10-22", "last_status": "ok", "last_ok_day": "2026-10-22"}))
    (tmp_path / "st.json").write_text(json.dumps({"tx": {"ok_date": "2026-10-22"}, "night": {"ok_date": "2026-10-22"}, "vrp": {"ok_date": "2026-10-22"}}))
    ok_intel = {"available": True, "service_ok": True, "heartbeat_age_sec": 5, "items": [{}] * 18, "problems": []}
    kw = dict(watchdog_log=tmp_path / "wd.log", app_log=tmp_path / "no.log", taifex_state=tmp_path / "st.json")
    good = sh.collect(ts(2026, 10, 24, 12, 0), "live", True, None, {"last_quote_age": 2.0}, {"connections": 1, "percent": 2.8}, 3600.0, lambda: ok_intel, good_bk, **kw)
    mixed = sh.collect(NOW, "live", True, None, {"last_quote_age": 2.0}, {"connections": 1, "percent": 2.8}, 3600.0, lambda: {"available": False, "reason": "找不到"}, tmp_path / "bk", **kw)
    (tmp_path / "fixtures.json").write_text(json.dumps({"good": good, "mixed": mixed}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["SH_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(ROOT / "tests" / "frontend" / "system_health_check.js")], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
