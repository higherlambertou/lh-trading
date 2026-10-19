"""〈口數提示〉（frontend/components/SizeHint.tsx、lib/size.ts）。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/size_check.js，react-dom/server 渲染、hooks 換成替身）。
畫面吃的資料由這裡用 core/position_size.py 現算，所以後端欄位改了、前端沒跟著改，這個測試會抓到。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core import position_size as ps

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "size_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


def size(equity=52_000, stop=30, contract="TMF", qty=None, watches=(), positions=(), cap=None, age=0.0):
    risk = ps.open_risk(list(positions), list(watches), 2.0)
    out = ps.compute(equity, stop, contract, risk_pct=1.0, slip=2.0, total_pct=3.0, open_twd=risk["twd"], cap=cap, qty=qty, equity_age=age)
    out["open_risk"] = {**risk, "pct": None if not out["equity"] else round(risk["twd"] / out["equity"] * 100, 3)}
    out["lot_cap"] = cap
    out["as_of"] = 1.0
    return out


def make_fixtures(d: Path) -> dict:
    watch = {"contract": "TMF", "direction": "Buy", "stop_loss_pts": 40, "remaining": 3, "quantity": 3, "seen": True, "is_option": False}
    pos = [{"code": "TMFJ6", "direction": "Buy", "quantity": 4}, {"code": "TXO22000J6", "direction": "Buy", "quantity": 1}]
    out = {"one": size(), "over": size(qty=2), "in_limit": size(qty=1), "zero": size(52_000, 100, "MXF"), "nostop": size(stop=None), "noequity": size(equity=None), "stale": size(age=999.0),
           "total": size(stop=10, watches=[watch], positions=pos), "cap": size(1_000_000, 50, cap=3), "tiny": size(1_000, 30)}
    (d / "fixtures.json").write_text(json.dumps(out, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return out


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_size_hint(tmp_path):
    make_fixtures(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["SZ_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
