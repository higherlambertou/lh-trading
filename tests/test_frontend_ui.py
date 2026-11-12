"""2026-11-12 的直覺性修正（頁首行情順序、說明開關、委託卡空狀態、下單面板的今日判斷一行）：tests/frontend/ui_check.js。
沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend"
SCRIPT = ROOT / "tests" / "frontend" / "ui_check.js"
READY = shutil.which("node") is not None and (FRONT / "node_modules" / "typescript").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_ui_components():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL:" not in r.stdout and "\nFAIL " not in r.stdout, r.stdout + r.stderr
    assert "全部通過" in r.stdout
