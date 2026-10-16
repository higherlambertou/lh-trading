"""〈進場判斷標記〉卡片（frontend/components/EntryLabelCard.tsx、lib/entryLabel.ts）。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/entry_label_check.js，react-dom/server 渲染、hooks 換成替身）。
畫面吃的資料由這裡用 core/entry_log.fetch_entries 現算（一筆持有中、一筆出場後才標、一筆沒標＋沒記到內外盤、一筆策略單），
所以後端欄位（含方向是 long／short 不是 Buy／Sell）改了、前端沒跟著改，這個測試會抓到。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import core.entry_log as el
import core.trade_log as tl
from test_entry_flow_label import SNAP
from test_entry_log import T0, add_deal, add_order, new_sync

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "entry_label_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()


def make_fixtures(d: Path) -> dict:
    db = d / "trade_log.db"
    c = sqlite3.connect(db)
    c.executescript(tl._SCHEMA)
    tl.TradeLog._migrate(c)
    c.commit()
    s = new_sync(c)

    def enter(tid, strategy, action, ts, px, snap=SNAP):
        el.flow_provider = (lambda: snap) if snap else (lambda: None)
        add_order(c, tid, mode="sim", strategy=strategy, action=action, ts=ts, signal=px)
        add_deal(c, tid, px, 1, ts=ts + 1)

    def leave(tid, action, ts, px):
        add_order(c, tid, mode="sim", strategy="manual", reason="tp", action=action, ts=ts, signal=px)
        add_deal(c, tid, px, 1, ts=ts + 1)

    try:
        enter("M3", "manual", "Buy", T0, 48900.0, snap=None)
        leave("X3", "Sell", T0 + 1000, 48950.0)
        enter("M2", "manual", "Sell", T0 + 12000, 49010.0)
        leave("X2", "Buy", T0 + 13200, 48985.0)
        enter("M1", "manual", "Buy", T0 + 19000, 48981.0)
        enter("S1", "scalp", "Buy", T0 + 19300, 48990.0)
        s.step()
    finally:
        el.flow_provider = None
        c.close()
    el.add_label(db, "sim", "M2", "short", "盤勢偏弱", now=T0 + 16000)           # 出場（T0+13200）之後才標
    el.add_label(db, "sim", "M1", "multi", None, now=T0 + 19100)
    out = {"rows": el.fetch_entries(db, "sim"), "labels": list(el.LABELS), "note_max": el.NOTE_MAX}
    (d / "fixtures.json").write_text(json.dumps(out, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return out


def test_fixture_rows_cover_the_cases_the_screen_must_handle(tmp_path):
    fx = make_fixtures(tmp_path)
    by = {r["trade_id"]: r for r in fx["rows"]}
    assert set(by) == {"M1", "M2", "M3", "S1"}
    assert (by["M1"]["status"], by["M1"]["trend_label"], by["M1"]["label_after_exit"]) == ("open", "multi", False)
    assert (by["M2"]["status"], by["M2"]["trend_label"], by["M2"]["label_after_exit"]) == ("closed", "short", True)
    assert by["M3"]["trend_label"] is None and by["M3"]["label_missing_reason"] == "not_entered" and by["M3"]["flow_missing_reason"]
    assert by["S1"]["label_missing_reason"] == "strategy_order"
    assert by["M1"]["direction"] == "long" and by["M2"]["direction"] == "short"


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_entry_label_card(tmp_path):
    make_fixtures(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["EL_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "\nFAIL " not in "\n" + r.stdout and "全部通過" in r.stdout, r.stdout + r.stderr
