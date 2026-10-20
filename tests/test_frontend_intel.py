"""〈資訊〉面板（frontend/lib/intel.ts、components/IntelPanel.tsx）。

前端沒有 DOM 測試工具，用 node 腳本（tests/frontend/intel_check.js，react-dom/server 渲染、hooks 換成替身）。
畫面吃的資料由這裡用 core/intel_reader.py 在種好資料的 intel.db 上現算，所以後端欄位改了、前端沒跟著改，這個測試會抓到。沒有 node 或 frontend/node_modules 就略過。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import core.intel_reader as ir
from intel_fixture import event, headline, make_db, obs, run

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "intel_check.js"
READY = shutil.which("node") is not None and (ROOT / "frontend" / "node_modules" / "react-dom").exists()
NOW = datetime(2026, 10, 19, 20, 0, tzinfo=ir.TAIPEI).timestamp()
H = 3600.0


def seed_taiwan(c) -> None:
    for sym, series, vals in (("twse", "taiex.close", (47111.26, 46822.08)), ("taifex", "vixtw.close", (30.61, 26.37))):
        for d, v in zip(("2026-10-15", "2026-10-16"), vals):
            obs(c, sym, series, d, v, NOW - 6 * H)
    days = ["2026-09-%02d" % d for d in range(14, 31) if datetime(2026, 9, d).weekday() < 5][:12] + ["2026-10-16"]
    for i, d in enumerate(days):
        obs(c, "twse", "market.turnover_twd", d, (8000 + 10 * i) * 1e8 if d != "2026-10-16" else 8667e8, NOW - 6 * H)
    for k, v in (("up", 684), ("down", 286), ("flat", 106), ("limit_up", 19), ("limit_down", 0)):
        obs(c, "twse", f"breadth.stock.{k}", "2026-10-16", v, NOW - 6 * H)


def seed(c) -> None:
    seed_taiwan(c)
    """一份「什麼都有一點」的資料庫：市場（有的缺、有的太舊）、期貨（三大法人與缺漏）、選擇權、PCR、現貨三大法人、融資融券、事件、休市日、新聞、收集器。"""
    for sym, series, vals in (("cboe", "VIX.close", (16.46, 19.99)), ("nasdaq", "SOX.close", (11581.22, 11137.29)), ("nasdaq", "TSM.close", (422.25, 412.61)), ("fred", "SP500", (7744.06, 7659.44))):
        for d, v in zip(("2026-10-15", "2026-10-16"), vals):
            obs(c, sym, series, d, v, NOW - 6 * H)
    for d, v in (("2026-10-14", 5.19), ("2026-10-15", 5.10)):
        obs(c, "fred", "DGS10", d, v, NOW - 6 * H)
    for d, v in (("2026-10-08", 31.94), ("2026-10-09", 31.94)):
        obs(c, "fred", "DEXTAUS", d, v, NOW - 6 * H)
    for k, d in enumerate(("2026-10-15", "2026-10-16")):                                                             # 大台：外資與自營商
        for who, lg, sh in (("foreign", 900 + 100 * k, 5200 - 200 * k), ("dealer", 250 + 50 * k, 220 - 20 * k)):
            for s, v in (("long_oi_lots", lg), ("short_oi_lots", sh), ("net_oi_lots", lg - sh)):
                obs(c, "taifex", f"futinst.TX.{who}.{s}", d, v, NOW - 6 * H)
    for d, k in (("2026-10-15", 1), ("2026-10-16", 2)):
        obs(c, "taifex", "optinst.TXO.CALL.foreign.net_oi_lots", d, -169 * k, NOW - 6 * H)
        obs(c, "taifex", "optinst.TXO.PUT.foreign.net_oi_lots", d, 5820 * k, NOW - 6 * H)
        obs(c, "taifex", "pcr.oi_ratio_pct", d, 60.0 * k, NOW - 6 * H)
        obs(c, "taifex", "pcr.vol_ratio_pct", d, 80.0 * k, NOW - 6 * H)
    for i, d in enumerate(("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")):
        for s, v in (("foreign", -100 - i), ("trust", 50 + i), ("dealer_self", -10), ("dealer_hedge", -20 - i), ("total", -80 - 3 * i)):
            obs(c, "twse", f"inst.{s}.net", d, v * 1e8, NOW - 6 * H)
    for d, a, s in (("2026-10-15", 646_415_871, 229_150), ("2026-10-16", 645_594_048, 233_602)):
        obs(c, "twse", "margin.financing_amt.today_balance", d, a, NOW - 6 * H)
        obs(c, "twse", "margin.short_lots.today_balance", d, s, NOW - 6 * H)
    event(c, "fomc-2026-10-28", "FOMC 利率決策", when=datetime(2026, 10, 29, 3, 0, tzinfo=ir.TAIPEI).timestamp() - 3600, date="2026-10-28", importance=3, detail={"zh": "FOMC 利率決策"})
    event(c, "bls-1", "Consumer Price Index", when=NOW + 30 * H, date="2026-10-21", importance=3, source="bls", detail={"zh": "美國 CPI"})
    event(c, "bls-2", "JOLTS", when=NOW - 2 * H, date="2026-10-19", importance=2, source="bls", detail={"zh": "美國職缺 JOLTS"})
    event(c, "fred-53", "Gross Domestic Product", when=None, date="2026-10-22", importance=3, source="fred", detail={"zh": "美國 GDP"})
    event(c, "h-sun", "光復節（週日）", date="2026-10-25", importance=None, kind="holiday", source="twse_calendar", country="TW", detail={"closed": True})
    event(c, "h-mon", "國慶補假", date="2026-10-26", importance=None, kind="holiday", source="twse_calendar", country="TW", detail={"closed": True})
    a = headline(c, "cna", "台股收跌 289 點", published_at=NOW - 600, fetched_at=NOW - 500)
    headline(c, "google_news", "台股收跌289點！", published_at=NOW - 590, fetched_at=NOW - 480, dup_of=a, publisher="經濟日報")
    headline(c, "google_news", "費半連八跌", published_at=NOW - 3 * H, fetched_at=NOW - 3 * H + 5, publisher="理財周刊")
    for i, st in enumerate(["ok", "error", "error"]):
        run(c, "twse_inst", NOW - 1000 + i * 100, st)
    run(c, "news_rss", NOW - 300, "ok")
    run(c, "news_rss", NOW - 100, "not_ready")
    run(c, "fomc_calendar", NOW - 10, None)


def make_fixtures(d: Path) -> dict:
    ir._cache.update(key=None, at=0.0, data=None)
    db_path = d / "intel.db"
    c = make_db(db_path)
    seed(c)
    c.close()
    (d / "heartbeat").write_text(f"{NOW - 45:.0f}", encoding="utf-8")
    full = ir.summary(NOW, db_path)
    (d / "heartbeat").write_text(f"{NOW - 3600:.0f}", encoding="utf-8")
    ir._cache.update(key=None, at=0.0, data=None)
    stale = ir.summary(NOW, db_path)
    ir._cache.update(key=None, at=0.0, data=None)
    out = {"full": full, "noheartbeat": stale, "unavailable": ir.summary(NOW, d / "missing.db")}
    (d / "fixtures.json").write_text(json.dumps(out, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    ir._cache.update(key=None, at=0.0, data=None)
    return out


def test_fixtures_cover_the_cases_the_screen_must_handle(tmp_path):
    fx = make_fixtures(tmp_path)
    f = fx["full"]
    assert f["available"] is True and "N225" in [m["key"] for m in f["markets"] if m["value"] is None] and any(m["age_days"] > 3 for m in f["markets"] if m["value"] is not None)
    assert len(f["chips"]["futures"]) == 1 and f["chips"]["futures"][0]["commodity"] == "TX" and f["chips"]["options"] and f["chips"]["pcr"] and f["chips"]["inst"] and f["chips"]["margin"]
    assert [e["uid"] for e in f["events"]["upcoming"]][0] == "bls-2" and f["events"]["upcoming"][0]["past"] is True and [c["date"] for c in f["events"]["closures"]] == ["2026-10-26"]
    assert f["collectors"]["service_ok"] is True and f["collectors"]["problems"] == ["twse_inst"] and fx["noheartbeat"]["collectors"]["service_ok"] is False
    assert fx["unavailable"]["available"] is False and "找不到資料庫" in fx["unavailable"]["reason"]


@pytest.mark.skipif(not READY, reason="需要 node 與 frontend/node_modules（npm install）")
def test_intel_panel(tmp_path):
    make_fixtures(tmp_path)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env["IN_FIXTURES"] = str(tmp_path)
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "\nFAIL " not in "\n" + r.stdout and "全部通過" in r.stdout, r.stdout + r.stderr
