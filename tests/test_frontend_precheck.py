"""〈下單前檢查〉卡（frontend/components/PrecheckCard.tsx、lib/precheck.ts）：用 core/precheck.py 現算的資料渲染（tests/frontend/precheck_check.js）。沒有 node 就略過。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from core import event_reminder as er
from core import precheck as pc

ROOT = Path(__file__).resolve().parent.parent
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules")
def test_precheck_card(tmp_path):
    now = datetime(2026, 11, 10, 9, 0, tzinfo=er.TAIPEI).timestamp()
    cpi = {"uid": "c", "source": "bls", "title_en": "Consumer Price Index", "scheduled_time": datetime(2026, 11, 10, 21, 30, tzinfo=er.TAIPEI).timestamp(), "local_date": "2026-11-10"}
    day = {"expect": {"lo": 488.6, "mid": 681.5, "hi": 999.6}, "r20": 542.4, "vix": 29.69, "vix_date": "2026-11-09"}
    full = pc.build({"day_iv": day}, {"ready": True, "iv": {"value": 27.12, "percentile": 85.0, "label": "偏高", "history_n": 252, "note": ""}}, pc.upcoming_major(now, [cpi]), now)
    quiet = pc.build({"day_iv": day}, {"ready": True, "iv": {"value": 18.0, "percentile": None, "label": "累積中", "history_n": 10, "note": "x"}}, [], now)
    empty = pc.build(None, {"ready": False}, None, now)
    (tmp_path / "fixtures.json").write_text(json.dumps({"full": full, "quiet": quiet, "empty": empty}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["PC_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(ROOT / "tests" / "frontend" / "precheck_check.js")], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
