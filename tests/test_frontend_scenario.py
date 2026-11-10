"""持倉情境表（frontend/lib/scenario.ts、components/ScenarioCard.tsx；update.md 2026-11-10）。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/scenario_check.js：純函式手算＋靜態渲染）；這裡再做靜態檢查：只顯示、接進部位面板、沒有被下單路徑引用。
沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend"
SCRIPT = ROOT / "tests" / "frontend" / "scenario_check.js"
READY = shutil.which("node") is not None and (FRONT / "node_modules" / "react-dom").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_scenario_pure_functions_and_card():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL:" not in r.stdout, r.stdout + r.stderr
    assert " PASS" in r.stdout and ", 0 FAIL" in r.stdout


def test_scenario_is_display_only_and_wired_into_the_position_panel():
    for f in (FRONT / "lib" / "scenario.ts", FRONT / "components" / "ScenarioCard.tsx"):
        src = f.read_text(encoding="utf-8")
        for bad in ("placeOrder", "cancel", 'method: "POST"', 'method: "DELETE"', "api.order", "api.strategy", "fetch(", "localStorage"):
            assert bad not in src, (f.name, bad)
    panel = (FRONT / "components" / "PositionPanel.tsx").read_text(encoding="utf-8")
    assert 'import ScenarioCard from "./ScenarioCard";' in panel
    assert "<ScenarioCard positions={positions} last={quotes} margin={margin} stale={staleAge(dataAge, posAge, cadence) != null} />" in panel
    assert "positions.length > 0 && <ScenarioCard" in panel                                       # 沒有持倉就不出現
    for sub in ("api", "strategies", "core"):                                                      # 後端不認得這個東西
        for f in (ROOT / sub).glob("*.py"):
            assert "ScenarioCard" not in f.read_text(encoding="utf-8"), f


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules")
def test_typescript_compiles():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run([str(FRONT / "node_modules" / ".bin" / "tsc"), "--noEmit", "-p", str(FRONT)], capture_output=True, text=True, timeout=300, env=env, cwd=FRONT)
    assert r.returncode == 0, r.stdout + r.stderr
