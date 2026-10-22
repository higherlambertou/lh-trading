"""〈下單前檢查〉卡（frontend/components/PrecheckCard.tsx、lib/precheck.ts）：用 core/precheck.py 現算的資料渲染（tests/frontend/precheck_check.js）。沒有 node 就略過。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from core import event_reminder as er
from core import precheck as pc

ROOT = Path(__file__).resolve().parent.parent
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules")
def test_precheck_card(tmp_path):
    now = datetime(2026, 11, 10, 9, 0, tzinfo=er.TAIPEI).timestamp()
    cpi = {"uid": "c", "source": "bls", "title_en": "Consumer Price Index", "scheduled_time": datetime(2026, 11, 10, 21, 30, tzinfo=er.TAIPEI).timestamp(), "local_date": "2026-11-10"}
    day = {"expect": {"lo": 488.6, "mid": 681.5, "hi": 999.6}, "r20": 542.4, "vix": 29.69, "vix_date": "2026-11-09"}
    full = pc.build({"day_iv": day}, {"ready": True, "iv": {"value": 27.12, "percentile": 85.0, "label": "偏高", "history_n": 252, "note": ""}}, pc.upcoming_major(now, [cpi]), now)
    quiet = pc.build({"day_iv": day}, {"ready": True, "iv": {"value": 18.0, "percentile": None, "label": "累積中", "history_n": 10, "note": "x"}}, [], now)
    empty = pc.build(None, {"ready": False}, None, now)
    eve = datetime(2026, 11, 10, 17, 0, tzinfo=er.TAIPEI).timestamp()                     # 傍晚：今天的日盤那一行沒有了，換成夜盤與下一個日盤（發現 67）
    iv = {"ready": True, "iv": {"value": 27.12, "percentile": 85.0, "label": "偏高", "history_n": 252, "note": ""}}
    night = {"expect": {"lo": 599.5, "mid": 898.9, "hi": 1363.5}, "r20n": 683.6, "vix": 30.31, "vix_date": "2026-11-10", "day_range": 1120.0, "prev_day": "2026-11-10", "n": 2276}
    nxt = {"expect": {"lo": 497.0, "mid": 693.4, "hi": 1016.9}, "r20": 579.8, "vix": 30.31, "vix_date": "2026-11-10", "atm_equiv": 28.19, "n": 1578}
    evening = pc.build(None, iv, [], eve, {"prev_day": "2026-11-10", "night": night, "next_day": nxt})
    waiting = pc.build(None, iv, [], eve, {"prev_day": "2026-11-10", "night": {"note": "等 11-10 收盤的臺指選擇權波動率指數（lh-intel 約 16:15 抓到）"},
                                           "next_day": {"note": "等 11-10 收盤的臺指選擇權波動率指數（lh-intel 約 16:15 抓到）"}})
    broken = pc.build(None, iv, [], eve, {"error": "RuntimeError"})
    hits_few = pc.build(None, iv, [], eve, None, {"day": {"n": 3, "inside": 2, "rate": 0.667, "ratio_median": 1.0}, "night": {"n": 1, "inside": 1, "rate": 1.0, "ratio_median": 1.1}, "min_show": 5})
    hits_ok = pc.build(None, iv, [], eve, None, {"day": {"n": 20, "inside": 14, "rate": 0.7, "ratio_median": 0.98}, "night": {"n": 3, "inside": 2, "rate": 0.667, "ratio_median": 1.0}, "min_show": 5})
    (tmp_path / "fixtures.json").write_text(json.dumps({"full": full, "quiet": quiet, "empty": empty, "evening": evening, "waiting": waiting, "broken": broken,
                                                       "hits_few": hits_few, "hits_ok": hits_ok},
                                                       ensure_ascii=False, allow_nan=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["PC_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(ROOT / "tests" / "frontend" / "precheck_check.js")], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
