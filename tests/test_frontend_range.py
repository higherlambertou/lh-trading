"""機率範圍帶的前端（frontend/lib/range.ts、components/RangeSection.tsx，update.md 發現 48）。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/range_check.js，react-dom/server 渲染、hooks 換成替身）。
畫面吃的資料由這裡用 core/range_suggest.py＋真正的實測表（core/range_calibration.json）現算，所以後端欄位改了、前端沒跟著改，這個測試會抓到。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import core.range_suggest as rs

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "range_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


def at(y, mo, d, hh, mm, ss=30):
    return time.mktime((y, mo, d, hh, mm, ss, 0, 0, -1))


def flow(now_ts, n=31, base=30000.0, step=3.0):
    b = int(now_ts // 60) * 60
    return [{"ts": b - 60 * i, "close": base + (i % 2) * step} for i in range(1, n + 1)]


def make_fixtures(d: Path) -> dict:
    t = rs.load_table()
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Taipei"
    time.tzset()
    try:
        d13, d10, n20 = at(2026, 10, 14, 13, 0), at(2026, 10, 14, 10, 15), at(2026, 10, 14, 20, 0)
        out = {"day": rs.range_now(flow(d13), d13, table=t), "morning": rs.range_now(flow(d10), d10, table=t), "night": rs.range_now(flow(n20), n20, table=t),
               "nosigma": rs.range_now(flow(d10, n=10), d10, table=t), "noprice": rs.range_now([], d10, table=t), "uncal": rs.range_now(flow(d10), d10, table=None),
               "stale": rs.range_now(flow(d10)[5:], d10, table=t)}                                    # 最後一筆是 6 分多鐘前，但前 30 分鐘的成交夠算 σ
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()
    (d / "fixtures.json").write_text(json.dumps(out, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return out


def test_fixtures_cover_the_cases_the_screen_must_handle(tmp_path):
    fx = make_fixtures(tmp_path)
    assert set(fx["day"]["bands"]) == {"5", "15", "30", "60", "close"} and fx["day"]["bands"]["close"]["bucket"] == "c31_60"
    assert "close" in fx["morning"]["bands"] and fx["morning"]["bands"]["close"]["bucket"] == "c121_270"
    assert "close" not in fx["night"]["bands"] and all(b["few"] for b in fx["night"]["bands"].values())
    assert fx["nosigma"]["bands"] == {} and "成交太少" in fx["nosigma"]["note"] and fx["noprice"]["bands"] == {} and "成交價" in fx["noprice"]["note"]
    assert fx["uncal"]["bands"] == {} and fx["uncal"]["calibrated"] is False
    assert fx["stale"]["stale"] is True and fx["stale"]["bands"]


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_range_section(tmp_path):
    make_fixtures(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["RG_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "\nFAIL " not in "\n" + r.stdout and "全部通過" in r.stdout, r.stdout + r.stderr
