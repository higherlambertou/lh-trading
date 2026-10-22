"""〈選擇權試算〉面板（買跨式／勒式／單向的試算、已買部位的平倉試算）：表單驗證與請求內容、結果畫面、容器的操作流程。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/options_check.js）：純函式直接呼叫、結果檢視用 react-dom/server 渲染、
容器把 React hooks 換成替身後直接呼叫。畫面吃的資料由這裡用 core/options_calc.py 現算（不是寫死的假資料），
所以後端欄位改了、前端沒跟著改，這個測試會抓到。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import core.options_calc as oc

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "options_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()

NOW, EXPIRY = datetime(2026, 10, 12, 18, 44), datetime(2026, 10, 21, 13, 30)
DAYS = (EXPIRY - NOW).total_seconds() / 86400
META = {"source": "live", "category": "TXO", "delivery_month": "202610", "expiry": "2026-10-21T13:30", "quote_ts": 1.79e9, "as_of": 1.79e9}


def _closes(n=1500, seed=4):
    rng = random.Random(seed)
    out = [30000.0]
    for _ in range(n):
        out.append(out[-1] * math.exp(rng.gauss(0, 0.011)))
    return out


def _chain(spread_pct=0.03):
    return oc.build_chain(oc.model_quotes(49283.0, 20.3, DAYS, spread_pct=spread_pct, min_spread=1.0), DAYS / 365.0)


def make_fixtures(d: Path) -> None:
    ch = _chain()
    straddle = oc.analyze(ch, oc.select_legs(ch, "straddle"), kind="straddle", lots=2, budget_twd=150_000, hold_hours=6, closes=_closes())
    wide = _chain(spread_pct=0.25)
    strangle = oc.analyze(wide, oc.select_legs(wide, "strangle", select="move", k=1.0, ref_move=600.0), kind="strangle", ref_move=600.0, hold_hours=6)
    call = oc.analyze(ch, oc.select_legs(ch, "call", select="delta", delta=0.4), kind="call", hold_hours=6)
    doc_legs = [{"right": "C", "strike": 100.0, "qty": 1, "entry": 50.0}, {"right": "P", "strike": 100.0, "qty": 1, "entry": 50.0}]
    doc = oc.close_analysis(doc_legs, F=130.0, T=20 / 365, iv=0.25, marks=[{"bid": 78.0, "ask": 82.0}, {"bid": 20.0, "ask": 20.0}], hold_hours=24)
    legs = oc.select_legs(ch, "straddle")
    held = [{"right": l["right"], "strike": l["strike"], "qty": 1, "entry": l["entry"] + (40 if l["right"] == "C" else -60)} for l in legs]
    live = oc.close_analysis(held, F=ch["F"], T=ch["T"], iv=ch["atm_iv"], marks=[{"bid": l["bid"], "ask": l["ask"], "iv": l["iv"]} for l in legs], hold_hours=24)
    cmp = oc.compare_structures(ch, lots=2, hold_days=1)
    cmp_exp = oc.compare_structures(ch, select="move", k=1.0, ref_move=600.0, hold_days=30)
    for name, body in (("compare", cmp), ("compare_expiry", cmp_exp)):
        (d / f"{name}.json").write_text(json.dumps({**body, **META}, allow_nan=False), encoding="utf-8")
    for name, body, extra in (("calc_straddle", straddle, META), ("calc_strangle_user", strangle, META), ("calc_call", call, META),
                              ("close_doc", doc, {**META, "source": "manual", "delivery_month": None, "expiry": None}), ("close_live", live, META)):
        (d / f"{name}.json").write_text(json.dumps({**body, **extra}, allow_nan=False), encoding="utf-8")


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_option_panel(tmp_path):
    make_fixtures(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["OPT_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "FAIL" not in r.stdout, r.stdout + r.stderr
