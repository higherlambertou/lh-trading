"""策略面板「停止」按鈕與確認框的邏輯。

前端沒有 DOM 測試工具，所以用 node 腳本把 React hooks 換成替身、直接呼叫元件函式檢查（tests/frontend/stop_confirm_check.js）。
涵蓋：持倉時不呼叫 API 而是開確認框、後端 409（畫面空手但券商帳上有部位）的說明放進確認框、強制停止才帶 force、
輪詢後確認框的去留。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "stop_confirm_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "typescript").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_stop_button_and_confirm_box_logic():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
