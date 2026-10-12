"""盤中即時面板的三張新卡片（買賣掛單／均價與基差／時段脈絡）：純函式格式、各種資料形狀下的畫面（日盤／夜盤／舊後端／資料壞掉）。

前端沒有 DOM 測試工具，用 node 腳本把 React hooks 換成替身、以 react-dom/server 渲染（tests/frontend/intraday_check.js）。
沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "intraday_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_intraday_cards_and_panel():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
