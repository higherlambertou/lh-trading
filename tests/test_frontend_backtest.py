"""回測面板的前端邏輯：表單驗證（會變成後端的命令列參數）、結論顏色（不能只憑一個數字就亮綠燈）、面板的點擊與各種工作狀態。

前端沒有 DOM 測試工具，用 node 腳本把 React hooks 換成替身、直接呼叫元件函式檢查（tests/frontend/backtest_check.js）。
沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "backtest_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "typescript").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_backtest_panel_logic():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
