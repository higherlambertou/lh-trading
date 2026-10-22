"""已驗證無效的面板收起來（frontend/components/Folded.tsx、DashboardTabs.tsx，發現 62）：tests/frontend/folded_check.js。沒有 node 就略過。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules")
def test_folded_panels():
    r = subprocess.run(["node", str(ROOT / "tests" / "frontend" / "folded_check.js")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
