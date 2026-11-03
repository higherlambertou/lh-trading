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
    assert "api.market.profile" in chart and "ProfilePrimitive" in chart and "attachPrimitive" in chart
    # 價位線與標籤都畫在繪圖層裡（跟著價格軸、避開 K 棒）；不再用座標軸上的價格線（會擠成一排色塊）
    assert "levelLines" not in chart
    assert "createPriceLine" not in (FRONT / "lib" / "profilePrimitive.ts").read_text(encoding="utf-8")


def test_profile_reserves_blank_space_on_the_right_and_keeps_one_background_color():
    """價量分布畫在 K 棒右邊的空白裡：開著就把最新一根往左推（rightOffset 與視野的右緣都加同樣的留白）；
    直條用和圖背景混出來的實心色，所以背景色只能有一個來源；格線半透明，才不會在直條上切出深色細線（直條畫在格線下面）。"""
    chart = (FRONT / "components" / "PriceChart.tsx").read_text(encoding="utf-8")
    assert "PROFILE_PAD_BARS" in chart and "toggles.profile ? PROFILE_PAD_BARS : PAD_BARS" in chart
    assert "ts.applyOptions({ rightOffset: pad })" in chart                      # scrollToRealTime 也會留同樣的空白
    assert "to: agg.length + pad" in chart                                       # 第一次與換週期時視野右緣留白
    assert "${pad}" in chart                                                     # 開關價量分布要重設視野
    assert "toggles.vwap, toggles.profile]" in chart                             # 畫 K 棒那個效果的依賴（開關價量分布要重畫、重設視野）
    assert "color: PANE_BG" in chart and '"#141420"' not in chart                # 圖背景色的唯一來源
    grid = [ln for ln in chart.splitlines() if ln.strip().startswith("const GRID_COLOR")][0]
    assert "rgba(" in grid
    prof = (FRONT / "lib" / "volumeProfile.ts").read_text(encoding="utf-8")
    assert 'export const PANE_BG = "#141420"' in prof


@pytest.mark.skipif(shutil.which("node") is None or not (FRONT / "node_modules" / "typescript").exists(), reason="需要 node 與 frontend/node_modules")
def test_typescript_compiles_with_the_real_lightweight_charts_types():
    """繪圖層用的是 lightweight-charts 真正的型別（ISeriesPrimitive 等）；型別對不上（升級套件之後）這裡會抓到。"""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    r = subprocess.run([str(FRONT / "node_modules" / ".bin" / "tsc"), "--noEmit", "-p", str(FRONT)], capture_output=True, text=True, timeout=300, env=env, cwd=FRONT)
    assert r.returncode == 0, r.stdout + r.stderr
