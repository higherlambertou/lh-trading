"""api/routes_backtest.py：回測 API（背景子行程 + 輪詢）。

重點：不能拖垮交易行程——回測跑在子行程、同時最多一個、可以取消、有逾時；輸入要嚴格驗證（參數會變成命令列）。
大部分測試用假的「回測指令」（一小段 python）驗證工作管理本身；一個端到端測試用真的 core.backtest 跑小資料。"""
from __future__ import annotations

import random
import sys
import time
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes_backtest as rb
from core.market_store import MarketStore

OK_SCRIPT = """
import json, sys, time
for name in ("orb", "rsi"):
    time.sleep(0.15)
    print("══ " + name + "：3 組參數，10 天", flush=True)
json.dump([{"strategy": "orb", "rows": []}, {"strategy": "rsi", "rows": []}], open(sys.argv[1], "w"))
"""
SLOW_SCRIPT = "import time; print('══ orb：開始', flush=True); time.sleep(30)"
FAIL_SCRIPT = "import sys; print('some noise'); print('boom: 資料不夠'); sys.exit(3)"
NO_OUTPUT_SCRIPT = "print('══ orb', flush=True)"                # 結束代碼 0 但沒有寫結果檔


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rb, "manager", rb.BacktestManager())
    monkeypatch.setattr(rb, "ENABLED", True)
    app = FastAPI()
    app.include_router(rb.router, prefix="/api/backtest")
    c = TestClient(app)
    yield c
    rb.manager.shutdown()


def use_script(monkeypatch, script):
    monkeypatch.setattr(rb, "build_argv", lambda req, out: [sys.executable, "-c", script, out])


def wait(client, job_id, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/backtest/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("工作沒有在時限內結束")


# ── 輸入驗證 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    {"strategy": "nope"}, {"strategy": "scalp"}, {"strategy": "orb; rm -rf /"},
    {"cost": -1}, {"cost": 51}, {"split": 0.1}, {"split": 0.95}, {"null": 13}, {"null": -1}, {"top": 0}, {"top": 11},
    {"walk_forward": "abc"}, {"walk_forward": "3,1"}, {"walk_forward": "120,20,5"}, {"walk_forward": "120;ls"},
    {"source": "csv"}, {"session": "weekend"}])
def test_bad_requests_are_rejected_before_anything_runs(client, monkeypatch, body):
    use_script(monkeypatch, OK_SCRIPT)
    assert client.post("/api/backtest/run", json=body).status_code == 422
    assert client.get("/api/backtest/jobs").json() == []


def test_defaults_and_walk_forward_normalisation():
    r = rb.BacktestRequest()
    assert (r.strategy, r.cost, r.split, r.null, r.walk_forward, r.source, r.session) == ("all", 4.0, 0.6, 0, None, "bars", "day")
    assert rb.BacktestRequest(walk_forward="").walk_forward is None
    assert rb.BacktestRequest(walk_forward="120,20").walk_forward == "120,20"


def test_the_command_line_is_built_from_validated_fields_only():
    argv = rb.build_argv(rb.BacktestRequest(strategy="orb", cost=3.5, split=0.7, null=6, walk_forward="120,20", source="flow",
                                            session="night", top=5), "/tmp/out.json")
    i = argv.index("-m")
    assert argv[i + 1] == "core.backtest"
    flags = dict(zip(argv[i + 2::2], argv[i + 3::2]))
    assert flags == {"--strategy": "orb", "--cost": "3.5", "--split": "0.7", "--source": "flow", "--session": "night", "--top": "5",
                     "--workers": str(rb.WORKERS), "--json": "/tmp/out.json", "--null": "6", "--walk-forward": "120,20"}
    plain = rb.build_argv(rb.BacktestRequest(), "/tmp/o.json")
    assert "--null" not in plain and "--walk-forward" not in plain


# ── 工作管理 ──────────────────────────────────────────────────────

def test_a_job_runs_in_the_background_and_returns_its_reports(client, monkeypatch):
    use_script(monkeypatch, OK_SCRIPT)
    r = client.post("/api/backtest/run", json={"strategy": "all"})
    assert r.status_code == 200
    jid = r.json()["id"]
    first = client.get(f"/api/backtest/jobs/{jid}").json()
    assert first["status"] == "running" and first["progress"]["total"] == 7           # 立刻回應，不等回測跑完
    j = wait(client, jid)
    assert j["status"] == "done" and [x["strategy"] for x in j["reports"]] == ["orb", "rsi"]
    assert j["progress"]["done"] == j["progress"]["total"] and j["error"] == ""
    assert any("══ rsi" in line for line in j["log_tail"])


def test_progress_follows_the_strategy_headers_in_the_output(client, monkeypatch):
    use_script(monkeypatch, "import time\nfor n in ('a','b','c'):\n    print('══ ' + n, flush=True); time.sleep(0.4)\nopen(__import__('sys').argv[1],'w').write('[]')")
    jid = client.post("/api/backtest/run", json={"strategy": "all"}).json()["id"]
    seen = set()
    t0 = time.time()
    while time.time() - t0 < 10:
        j = client.get(f"/api/backtest/jobs/{jid}").json()
        seen.add(j["progress"]["done"])
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert {1, 2} <= seen                                                              # 中途看得到逐步增加
    assert j["status"] == "done" and j["progress"]["done"] == 7


def test_only_one_job_runs_at_a_time_and_a_cancel_frees_the_slot(client, monkeypatch):
    use_script(monkeypatch, SLOW_SCRIPT)
    jid = client.post("/api/backtest/run", json={"strategy": "orb"}).json()["id"]
    assert client.post("/api/backtest/run", json={"strategy": "rsi"}).status_code == 409
    assert client.post(f"/api/backtest/jobs/{jid}/cancel").json() == {"cancelled": True}
    j = wait(client, jid, timeout=10)
    assert j["status"] == "cancelled" and "取消" in j["error"]
    assert client.post(f"/api/backtest/jobs/{jid}/cancel").json() == {"cancelled": False}      # 已經結束的不能再取消
    use_script(monkeypatch, OK_SCRIPT)
    assert client.post("/api/backtest/run", json={"strategy": "orb"}).status_code == 200


def test_cancelling_kills_the_whole_process_group(client, monkeypatch):
    """回測會開平行的子行程；取消只殺主行程的話，它們會變成孤兒繼續吃 CPU。"""
    script = ("import subprocess, sys, time\n"
              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "print('child', p.pid, flush=True)\ntime.sleep(60)")
    use_script(monkeypatch, script)
    jid = client.post("/api/backtest/run", json={"strategy": "orb"}).json()["id"]
    child = None
    t0 = time.time()
    while child is None and time.time() - t0 < 10:
        for line in client.get(f"/api/backtest/jobs/{jid}").json()["log_tail"]:
            if line.startswith("child "):
                child = int(line.split()[1])
        time.sleep(0.05)
    assert child
    client.post(f"/api/backtest/jobs/{jid}/cancel")
    wait(client, jid, timeout=10)
    import os
    for _ in range(60):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        raise AssertionError("取消後孫行程還活著")


def test_a_failing_run_reports_the_tail_of_its_output(client, monkeypatch):
    use_script(monkeypatch, FAIL_SCRIPT)
    j = wait(client, client.post("/api/backtest/run", json={}).json()["id"])
    assert j["status"] == "failed" and "boom: 資料不夠" in j["error"] and j["reports"] is None


def test_exit_code_zero_without_a_result_file_is_a_failure_not_an_empty_success(client, monkeypatch):
    use_script(monkeypatch, NO_OUTPUT_SCRIPT)
    j = wait(client, client.post("/api/backtest/run", json={}).json()["id"])
    assert j["status"] == "failed" and "讀不到回測結果" in j["error"]


def test_a_runaway_job_is_stopped_at_the_timeout(client, monkeypatch):
    use_script(monkeypatch, SLOW_SCRIPT)
    monkeypatch.setattr(rb, "JOB_TIMEOUT_SEC", 0.6)
    j = wait(client, client.post("/api/backtest/run", json={}).json()["id"], timeout=10)
    assert j["status"] == "failed" and "超過" in j["error"]


def test_the_job_list_is_newest_first_trimmed_and_without_reports(client, monkeypatch):
    use_script(monkeypatch, OK_SCRIPT)
    monkeypatch.setattr(rb, "KEEP_JOBS", 3)
    ids = []
    for s in ("orb", "rsi", "breakout", "momentum", "bollinger"):
        jid = client.post("/api/backtest/run", json={"strategy": s}).json()["id"]
        wait(client, jid)
        ids.append(jid)
    lst = client.get("/api/backtest/jobs").json()
    assert [j["id"] for j in lst] == ids[::-1][:3]
    assert all("reports" not in j and j["has_reports"] for j in lst)
    assert client.get(f"/api/backtest/jobs/{ids[0]}").status_code == 404                  # 太舊的被收掉


def test_unknown_job_is_404(client):
    assert client.get("/api/backtest/jobs/nope").status_code == 404
    assert client.post("/api/backtest/jobs/nope/cancel").status_code == 404


def test_the_whole_api_can_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(rb, "ENABLED", False)
    for method, url in (("get", "/api/backtest/info"), ("post", "/api/backtest/run"), ("get", "/api/backtest/jobs")):
        r = getattr(client, method)(url, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 503 and "BACKTEST_API=false" in r.json()["detail"]


def test_shutdown_stops_a_running_job(client, monkeypatch):
    use_script(monkeypatch, SLOW_SCRIPT)
    jid = client.post("/api/backtest/run", json={}).json()["id"]
    rb.manager.shutdown()
    assert wait(client, jid, timeout=10)["status"] == "cancelled"


# ── 資料範圍與端到端 ──────────────────────────────────────────────

def make_store(tmp_path, n_days=40):
    ms = MarketStore(tmp_path / "ms.db")
    rng = random.Random(5)
    rows = []
    for k in range(n_days):
        d = (date(2026, 1, 5) + timedelta(days=k)).isoformat()
        px = 100.0 + k
        for i in range(60):
            o = px
            px += rng.choice((-3, -1, 1, 3, 5, -5))
            end = datetime.strptime(f"{d} 08:46", "%Y-%m-%d %H:%M") + timedelta(minutes=i)
            rows.append({"date": d, "hhmm": end.hour * 100 + end.minute, "open": o, "high": max(o, px) + 1, "low": min(o, px) - 1,
                         "close": px, "volume": 10 + i})
    ms.upsert_bars_1m("TXF", rows)
    return str(tmp_path / "ms.db")


def test_info_describes_the_local_data(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_DB_PATH", make_store(tmp_path))
    j = client.get("/api/backtest/info").json()
    assert j["bars"] == {"code": "TXF", "days": 40, "first": "2026-01-05", "last": "2026-02-13"}
    assert j["flow"]["minutes"] == 0 and j["workers"] == rb.WORKERS


def test_end_to_end_with_the_real_command(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_DB_PATH", make_store(tmp_path))
    jid = client.post("/api/backtest/run", json={"strategy": "ma_cross", "walk_forward": "20,5", "null": 1, "top": 2}).json()["id"]
    j = wait(client, jid, timeout=120)
    assert j["status"] == "done", j["error"]
    (rep,) = j["reports"]
    assert rep["strategy"] == "ma_cross" and rep["combos"] == 12 and rep["days"] == 40 and len(rep["rows"]) == 12
    assert rep["walk_forward"]["windows"] >= 1 and len(rep["null"]) == 1 and rep["cost"] == 4.0
    assert j["progress"] == {"done": 1, "total": 1}
