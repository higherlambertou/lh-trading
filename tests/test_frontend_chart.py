"""走勢圖的前端邏輯（update.md 發現 42）：合併 K 棒、換週期、均線、VWAP、場次水位、持倉／停損停利線、設定還原、控制項的點擊。

前端沒有 DOM 測試工具，用 node 腳本把 React hooks 換成替身、直接呼叫元件函式檢查（tests/frontend/chart_check.js）。
沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "chart_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "typescript").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_chart_logic_and_controls():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL:" not in r.stdout, r.stdout + r.stderr          # 失敗的項目會印「FAIL: …」；結尾的「N PASS, 0 FAIL」是摘要
    assert " PASS" in r.stdout and ", 0 FAIL" in r.stdout


def test_the_chart_component_is_read_only():
    """走勢圖只讀：不能出現下單、取消委託、改停損的呼叫。"""
    src = (ROOT / "frontend" / "components" / "PriceChart.tsx").read_text(encoding="utf-8")
    lib = (ROOT / "frontend" / "lib" / "chart.ts").read_text(encoding="utf-8")
    for bad in ("placeOrder", "cancel", "method: \"POST\"", "method: \"DELETE\"", "api.order.place", "api.strategy"):
        assert bad not in src and bad not in lib, bad
    assert "api.market.bars" in src and "api.position.list" in src and "api.order.watches" in src
