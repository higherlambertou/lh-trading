"""〈建議停損停利〉卡片（frontend/components/StopSuggestCard.tsx、lib/stopSuggest.ts）。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/stop_suggest_check.js，react-dom/server 渲染、hooks 換成替身）。
畫面吃的資料由這裡用 core/stop_suggest.py 現算（含真的校準表），所以後端欄位改了、前端沒跟著改，這個測試會抓到。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import core.stop_suggest as ss

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "stop_suggest_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


def make_fixtures(d: Path) -> dict:
    table = ss.load_table()
    day = ss.build(17.6, 30, session="day", horizon=15, contract="TMF", equity=52318.0, table=table,
                   today={"r20": 700.0, "early": 150.0, "done": 90.0, "expect_lo": 100.0, "expect_mid": 160.0, "expect_hi": 240.0})
    night = ss.build(8.5, 30, session="night", horizon=5, contract="TMF", equity=52318.0, table=table)
    far = ss.build(17.6, 30, session="day", horizon=15, contract="TMF", equity=52318.0, table=table,
                   today={"r20": 700.0, "early": 150.0, "done": 90.0, "expect_lo": 20.0, "expect_mid": 30.0, "expect_hi": 50.0})
    uns = copy.deepcopy(table)
    th = uns["day"]["table"]["15"]
    th["unstable_touch"][3] = True                                        # 高勝率型的停損 2.0σ
    th["unstable_win"][1][1] = True                                       # 平衡
    unstable = ss.build(17.6, 30, session="day", horizon=15, contract="TMF", equity=52318.0, table=uns)
    out = {"day": day, "night": night, "noequity": ss.build(17.6, 30, session="day", horizon=15, equity=None, table=table), "far": far,
           "note": ss.build(None, 12, session="day", horizon=15, equity=52318.0, table=table),
           "uncal": ss.build(17.6, 30, session="day", horizon=15, equity=52318.0, table=None), "unstable": unstable}
    (d / "fixtures.json").write_text(json.dumps(out, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return out


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_stop_suggest_card(tmp_path):
    make_fixtures(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["SS_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
