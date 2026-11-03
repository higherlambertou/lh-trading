"""價量分布的前端（frontend/lib/volumeProfile.ts、lib/profilePrimitive.ts、PriceChart 的開關與說明列，update.md 發現 77）。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/profile_check.js：純函式＋假的畫布與價格軸；元件的部分在 chart_check.js）。
沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend"
SCRIPT = ROOT / "tests" / "frontend" / "profile_check.js"
READY = shutil.which("node") is not None and (FRONT / "node_modules" / "typescript").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_profile_bars_levels_caption_and_drawing():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL:" not in r.stdout, r.stdout + r.stderr
    assert " PASS" in r.stdout and ", 0 FAIL" in r.stdout


def test_profile_front_end_is_display_only_and_wired_to_the_endpoint():
    """只顯示：價量分布的檔案不能出現下單、取消委託、改停損；而且確實接到 /market/profile。"""
    files = [FRONT / "lib" / "volumeProfile.ts", FRONT / "lib" / "profilePrimitive.ts"]
    for f in files:
        src = f.read_text(encoding="utf-8")
        for bad in ("placeOrder", "cancel", "method: \"POST\"", "method: \"DELETE\"", "api.order", "api.strategy", "fetch("):
            assert bad not in src, (f.name, bad)
    api = (FRONT / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "/market/profile" in api and "profile: (q" in api
    chart = (FRONT / "components" / "PriceChart.tsx").read_text(encoding="utf-8")
    assert "api.market.profile" in chart and "ProfilePrimitive" in chart and "levelLines(profile)" in chart
    assert "attachPrimitive" in chart


@pytest.mark.skipif(shutil.which("node") is None or not (FRONT / "node_modules" / "typescript").exists(), reason="需要 node 與 frontend/node_modules")
def test_typescript_compiles_with_the_real_lightweight_charts_types():
    """繪圖層用的是 lightweight-charts 真正的型別（ISeriesPrimitive 等）；型別對不上（升級套件之後）這裡會抓到。"""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run([str(FRONT / "node_modules" / ".bin" / "tsc"), "--noEmit", "-p", str(FRONT)], capture_output=True, text=True, timeout=300, env=env, cwd=FRONT)
    assert r.returncode == 0, r.stdout + r.stderr
