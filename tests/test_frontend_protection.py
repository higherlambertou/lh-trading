"""〈沒有停損保護〉橫幅（frontend/components/UnprotectedBanner.tsx、lib/protection.ts）。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/protection_check.js，react-dom/server 渲染、hooks 換成替身）。
畫面吃的資料由這裡用 core/protection.py 現算，所以後端欄位改了、前端沒跟著改，這個測試會抓到。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.protection import unprotected

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "protection_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


def make_fixtures(d: Path) -> dict:
    pos = lambda code, direction, qty: {"code": code, "direction": direction, "quantity": qty, "price": 1.0}   # noqa: E731
    watch = {"id": "w", "match_code": "TMF", "direction": "Buy", "remaining": 2, "quantity": 2, "stop_loss_pts": 50, "take_profit_pts": 0,
             "is_option": False, "seen": True, "close_gave_up": False}
    out = {"single": unprotected([pos("TMFJ6", "Buy", 1)], []),
           "partial": unprotected([pos("TMFJ6", "Buy", 3)], [watch]),
           "two": unprotected([pos("TMFJ6", "Buy", 1), pos("MXFJ6", "Sell", 2)], []),
           "none": unprotected([pos("TMFJ6", "Buy", 1)], [watch])}
    (d / "fixtures.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_unprotected_banner(tmp_path):
    make_fixtures(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["PROT_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
