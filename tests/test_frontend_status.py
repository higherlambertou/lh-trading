"""連線狀態燈（frontend/components/StatusDot.tsx，發現 65）：tests/frontend/status_check.js。沒有 node 就略過。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "typescript").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules")
def test_status_dot():
    r = subprocess.run(["node", str(ROOT / "tests" / "frontend" / "status_check.js")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
