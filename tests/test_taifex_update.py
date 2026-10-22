"""期交所日資料自動更新（發現 65）：什麼時候跑、成功記住（跨重啟）、失敗重試與上限、模擬盤不跑、指令是低優先權的獨立子行程、真的能跑子行程。"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

import pytest

from core import taifex_update as tu
from core.daytrade import TAIPEI


def tpe(*a):
    return datetime(*a, tzinfo=TAIPEI)


class Runner:
    def __init__(self, results=None):
        self.calls, self.results = [], results or {}

    async def __call__(self, job):
        self.calls.append(job)
        r = self.results.get(job, (True, "下載 1 個、略過 10 個、失敗 0 個"))
        if isinstance(r, Exception):
            raise r
        return r


def test_due_schedule(tmp_path):
    u = tu.TaifexUpdater(tmp_path / "s.json", Runner())
    assert u.due(tpe(2026, 10, 22, 15, 9)) == []
    assert u.due(tpe(2026, 10, 22, 15, 10)) == ["tx", "vrp"]
    assert u.due(tpe(2026, 10, 24, 16, 0)) == []                                      # 週末不跑


def test_success_is_remembered_across_restarts(tmp_path):
    p = tmp_path / "s.json"
    r = Runner()
    u = tu.TaifexUpdater(p, r)
    assert asyncio.run(u.tick(tpe(2026, 10, 22, 15, 15))) == {"tx": True, "vrp": True}
    assert r.calls == ["tx", "vrp"]
    assert json.loads(p.read_text())["tx"]["ok_date"] == "2026-10-22"
    u2 = tu.TaifexUpdater(p, r)                                                       # 重啟
    assert u2.due(tpe(2026, 10, 22, 18, 0)) == []
    assert u2.due(tpe(2026, 10, 23, 15, 10)) == ["tx"]                                 # TX 每天；IV 每 7 天
    assert u2.due(tpe(2026, 10, 29, 15, 10)) == ["tx", "vrp"]


def test_failures_retry_later_and_stop_after_three(tmp_path):
    r = Runner({"tx": (False, "下載失敗 tx_2026_10.csv：timeout"), "vrp": RuntimeError("boom")})
    u = tu.TaifexUpdater(tmp_path / "s.json", r)
    t = tpe(2026, 10, 22, 15, 10)
    assert asyncio.run(u.tick(t)) == {"tx": False, "vrp": False}
    assert u.due(datetime.fromtimestamp(t.timestamp() + 1799, TAIPEI)) == []
    for k in (1, 2):
        assert asyncio.run(u.tick(datetime.fromtimestamp(t.timestamp() + 1800 * k, TAIPEI))) == {"tx": False, "vrp": False}
    assert u.due(datetime.fromtimestamp(t.timestamp() + 1800 * 3, TAIPEI)) == []        # 一天最多 3 次
    assert r.calls.count("tx") == 3 and not (tmp_path / "s.json").exists()
    assert u.due(tpe(2026, 10, 23, 15, 10)) == ["tx", "vrp"]                            # 隔天重新來


def test_command_is_a_low_priority_subprocess():
    assert tu.command("tx") == ["nice", "-n", "10", sys.executable, "-m", "core.oi_study", "fetch", "--only", "tx"]
    assert tu.command("vrp")[:4] == ["nice", "-n", "10", sys.executable] and tu.command("vrp")[4:] == ["-m", "core.vrp_study", "fetch"]


def test_not_in_simulation_or_when_disabled(monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    assert not tu.enabled()
    asyncio.run(asyncio.wait_for(tu.taifex_update_loop(), 1))
    monkeypatch.setenv("SIMULATION", "false")
    monkeypatch.setenv("TAIFEX_UPDATE", "false")
    assert not tu.enabled()
    monkeypatch.delenv("TAIFEX_UPDATE")
    assert tu.enabled()


def test_run_subprocess_really_runs(monkeypatch):
    monkeypatch.setitem(tu.JOBS, "t", {"every_days": 1, "argv": ["-c", "print('第一行'); print('下載 0 個、略過 3 個、失敗 0 個')"], "text": "測試"})
    monkeypatch.setitem(tu.JOBS, "bad", {"every_days": 1, "argv": ["-c", "import sys; print('壞了'); sys.exit(1)"], "text": "測試"})
    assert asyncio.run(tu.run_subprocess("t")) == (True, "下載 0 個、略過 3 個、失敗 0 個")
    assert asyncio.run(tu.run_subprocess("bad")) == (False, "壞了")
    monkeypatch.setattr(tu, "TIMEOUT_SEC", 0.5)
    monkeypatch.setitem(tu.JOBS, "slow", {"every_days": 1, "argv": ["-c", "import time; time.sleep(30)"], "text": "測試"})
    ok, msg = asyncio.run(tu.run_subprocess("slow"))
    assert not ok and "已中止" in msg
