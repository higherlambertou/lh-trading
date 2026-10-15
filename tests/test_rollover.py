"""換月（update.md 發現 38）：訂閱的近月合約結算後，後端在 health 要求 watchdog 重啟。判斷、時間點、狀態、broker 記錄解析結果、迴圈。
不碰網路與真實資料庫；通知用假的 notify。"""
from __future__ import annotations

import asyncio
import json
import queue
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import core.rollover as ro
from core.broker import BrokerClient
from core.daytrade import TAIPEI

OPEN = lambda d: True  # noqa: E731


def tpe(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TAIPEI)


E = date(2026, 10, 21)                          # TMFJ6 的最後交易日（第三個週三）
RES = {"TMF": "TMFJ6", "MXF": "TMFJ6".replace("TMF", "MXF"), "TXF": "TXFJ6"}
FRESH = {"expiry": None, "done_until": 0.0, "attempts": 0, "gave_up": False, "confirmed": False, "stale_code": ""}


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message, *, key=None, cooldown=0.0, priority="high", wait=False):
        self.calls.append({"title": title, "message": message, "key": key, "cooldown": cooldown, "priority": priority})
        return True


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ro, "_wanted", None)
    monkeypatch.delenv("ROLLOVER_RESTART", raising=False)


# ── 哪一個合約算「已結算」 ───────────────────────────────────

def test_not_stale_before_the_last_trade_time():
    assert ro.stale_contract(RES, tpe(2026, 10, 21, 13, 29), OPEN) is None
    assert ro.stale_contract(RES, tpe(2026, 10, 15, 10, 0), OPEN) is None


def test_stale_from_1330_on_the_last_trading_day():
    s = ro.stale_contract(RES, tpe(2026, 10, 21, 13, 30), OPEN)
    assert s["code"] and s["expiry"] == E and s["prefix"] in RES
    assert ro.stale_contract({"TMF": "TMFK6"}, tpe(2026, 10, 21, 20, 0), OPEN) is None          # 下個月的合約沒到期


def test_stale_across_year_end():
    assert ro.stale_contract({"TMF": "TMFL6"}, tpe(2027, 1, 5, 10, 0), OPEN)["expiry"] == date(2026, 12, 16)


def test_holiday_shifted_expiry_is_respected():
    closed = {E}
    is_open = lambda d: d not in closed and d.weekday() < 5  # noqa: E731
    assert ro.stale_contract({"TMF": "TMFJ6"}, tpe(2026, 10, 21, 14, 0), is_open) is None       # 最後交易日順延到 10/22
    assert ro.stale_contract({"TMF": "TMFJ6"}, tpe(2026, 10, 22, 13, 30), is_open)["expiry"] == date(2026, 10, 22)


@pytest.mark.parametrize("resolved", [{}, {"TMF": ""}, {"TMF": "TMFR1"}, {"TMF": "garbage"}, {"TMF": "TXO20000J6"}])
def test_unknown_codes_are_never_acted_on(resolved):
    assert ro.stale_contract(resolved, tpe(2026, 10, 25, 10, 0), OPEN) is None
    assert ro.evaluate(tpe(2026, 10, 25, 10, 0), resolved, OPEN, FRESH, 1000.0)[0] is None


def test_the_earliest_expiry_wins():
    s = ro.stale_contract({"TMF": "TMFJ6", "TXF": "TXFI6"}, tpe(2026, 10, 22, 10, 0), OPEN)
    assert s["code"] == "TXFI6"


# ── 什麼時候要求重啟 ─────────────────────────────────────────

def ev(now, state=None, uptime=1000.0, resolved=None):
    return ro.evaluate(now, resolved or {"TMF": "TMFJ6"}, OPEN, state or dict(FRESH), uptime)


def test_waits_for_the_contract_file_update_after_the_close():
    act, st, info = ev(tpe(2026, 10, 21, 13, 31))
    assert act is None and info["code"] == "TMFJ6"
    assert ev(tpe(2026, 10, 21, 14, 45, 59))[0] is None                  # 14:45 才更新、14:46 才試
    act, st, info = ev(tpe(2026, 10, 21, 14, 46))
    assert act == "restart" and st["attempts"] == 1 and info["attempts"] == 1 and info["total"] == 4


def test_each_slot_is_used_once_and_older_slots_are_voided():
    st = dict(FRESH)
    act, st, _ = ev(tpe(2026, 10, 21, 14, 50), st)
    assert act == "restart"
    assert ev(tpe(2026, 10, 21, 14, 55), st)[0] is None                  # 剛試過：要等下一個時間點
    assert ev(tpe(2026, 10, 21, 17, 15), st)[0] is None
    act, st, info = ev(tpe(2026, 10, 21, 17, 16), st)
    assert act == "restart" and info["attempts"] == 2
    assert ev(tpe(2026, 10, 22, 7, 50), st)[0] is None
    act, st, info = ev(tpe(2026, 10, 22, 7, 51), st)
    assert act == "restart" and info["attempts"] == 3
    act, st, info = ev(tpe(2026, 10, 22, 8, 1), st)
    assert act == "restart" and info["attempts"] == 4


def test_gives_up_once_after_the_last_slot():
    st = dict(FRESH)
    for t in (tpe(2026, 10, 21, 14, 46), tpe(2026, 10, 21, 17, 16), tpe(2026, 10, 22, 7, 51), tpe(2026, 10, 22, 8, 1)):
        act, st, _ = ev(t, st)
        assert act == "restart"
    act, st, info = ev(tpe(2026, 10, 22, 8, 5), st)
    assert act == "gave_up" and st["gave_up"] is True and info["attempts"] == 4
    assert ev(tpe(2026, 10, 22, 9, 0), st)[0] is None                    # 只通知一次、之後不再動


def test_a_late_start_tries_once_then_gives_up():
    """結算日之後好幾天才啟動、還是解析到舊合約：所有更新時間都過了，試一次就放棄，不會連續重啟四次。"""
    act, st, _ = ev(tpe(2026, 10, 25, 10, 0))
    assert act == "restart"
    assert ev(tpe(2026, 10, 25, 10, 5), st)[0] == "gave_up"


def test_fresh_process_is_given_time_to_subscribe():
    assert ev(tpe(2026, 10, 21, 15, 0), uptime=ro.MIN_UPTIME - 1)[0] is None
    assert ev(tpe(2026, 10, 21, 15, 0), uptime=ro.MIN_UPTIME)[0] == "restart"


def test_a_new_expiry_resets_the_state():
    old = {"expiry": "2026-09-16", "done_until": 9e12, "attempts": 4, "gave_up": True, "confirmed": True, "stale_code": "TMFI6"}
    act, st, info = ev(tpe(2026, 10, 21, 15, 0), old)
    assert act == "restart" and st["expiry"] == "2026-10-21" and st["attempts"] == 1
    assert st["confirmed"] is False and st["stale_code"] == "TMFJ6"


def test_resolved_to_the_next_month_means_nothing_to_do():
    assert ev(tpe(2026, 10, 21, 15, 0), resolved={"TMF": "TMFK6", "TXF": "TXFK6"})[0] is None


# ── 狀態檔 ───────────────────────────────────────────────────

def test_state_roundtrip_and_bad_files(tmp_path):
    p = tmp_path / "s.json"
    assert ro.load_state(p) == FRESH                                      # 沒有檔
    full = {"expiry": "2026-10-21", "done_until": 12.5, "attempts": 2, "gave_up": False, "confirmed": True, "stale_code": "TMFJ6"}
    ro.save_state(p, full)
    assert ro.load_state(p) == full
    p.write_text("not json")
    assert ro.load_state(p) == FRESH
    p.write_text(json.dumps({"version": 9}))
    assert ro.load_state(p) == FRESH
    ro.save_state(tmp_path / "no" / "such" / "dir" / "s.json", FRESH)       # 會建目錄
    assert (tmp_path / "no" / "such" / "dir" / "s.json").exists()


def test_state_path_follows_the_mode(monkeypatch):
    monkeypatch.setenv("SIMULATION", "false")
    assert ro.state_path().name == "rollover_state_live.json"
    monkeypatch.setenv("SIMULATION", "true")
    assert ro.state_path().name == "rollover_state_sim.json"


# ── 迴圈：先存狀態再要求重啟、通知、health ────────────────────

class _Clock:
    def __init__(self, now):
        self.now = now


def _run_loop(monkeypatch, tmp_path, resolved, now, iterations=1, uptime=1000.0):
    rec = Rec()
    monkeypatch.setattr(ro, "notify", rec)
    monkeypatch.setattr(ro, "_STARTED", __import__("time").time() - uptime)
    monkeypatch.setattr(ro.reminders, "is_open_day", OPEN)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(ro, "datetime", FakeDT)
    n = {"i": 0}

    async def fake_sleep(_):
        n["i"] += 1
        if n["i"] > iterations:
            raise asyncio.CancelledError

    monkeypatch.setattr(ro.asyncio, "sleep", fake_sleep)
    path = tmp_path / "state.json"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ro.rollover_loop(lambda: resolved, path))
    return rec, path


def test_loop_requests_a_restart_and_saves_the_state_first(monkeypatch, tmp_path):
    rec, path = _run_loop(monkeypatch, tmp_path, {"TMF": "TMFJ6"}, tpe(2026, 10, 21, 14, 50))
    assert ro.wanted() == "rollover"
    st = ro.load_state(path)
    assert st["expiry"] == "2026-10-21" and st["attempts"] == 1
    assert len(rec.calls) == 1
    c = rec.calls[0]
    assert c["key"] == "rollover:2026-10-21:1" and c["priority"] == "high" and "TMFJ6" in c["title"] and "1/4" in c["title"]


def test_loop_does_nothing_when_the_contract_is_current(monkeypatch, tmp_path):
    rec, path = _run_loop(monkeypatch, tmp_path, {"TMF": "TMFK6"}, tpe(2026, 10, 21, 14, 50))
    assert ro.wanted() is None and rec.calls == [] and not path.exists()


def test_loop_with_no_resolved_codes_yet(monkeypatch, tmp_path):
    rec, path = _run_loop(monkeypatch, tmp_path, {}, tpe(2026, 10, 21, 14, 50))
    assert ro.wanted() is None and rec.calls == []


def test_loop_gives_up_urgently(monkeypatch, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"version": 1, "expiry": "2026-10-21", "done_until": tpe(2026, 10, 22, 8, 1).timestamp(), "attempts": 4, "gave_up": False,
                                                      "confirmed": False, "stale_code": "TMFJ6"}))
    rec, path = _run_loop(monkeypatch, tmp_path, {"TMF": "TMFJ6"}, tpe(2026, 10, 22, 9, 0))
    assert ro.wanted() is None
    assert len(rec.calls) == 1 and rec.calls[0]["priority"] == "urgent" and rec.calls[0]["key"] == "rollover:2026-10-21:gaveup"
    assert ro.load_state(path)["gave_up"] is True


def test_loop_announces_success_once_after_the_restart(monkeypatch, tmp_path):
    """重啟過、而且現在訂閱的合約是下個月 → 通知一次（之後不再通知）。"""
    (tmp_path / "state.json").write_text(json.dumps({"version": 1, "expiry": "2026-10-21", "done_until": tpe(2026, 10, 21, 14, 46).timestamp(), "attempts": 1,
                                                      "gave_up": False, "confirmed": False, "stale_code": "TMFJ6"}))
    rec, path = _run_loop(monkeypatch, tmp_path, {"TMF": "TMFK6", "TXF": "TXFK6"}, tpe(2026, 10, 21, 15, 5), iterations=2)
    assert len(rec.calls) == 1
    c = rec.calls[0]
    assert c["key"] == "rollover:2026-10-21:done" and "TMFK6" in c["title"] and "TMFJ6" in c["message"] and c["priority"] == "default"
    assert ro.load_state(path)["confirmed"] is True and ro.wanted() is None


def test_no_success_message_when_the_code_cannot_be_read(monkeypatch, tmp_path):
    """重啟過，但拿到的是 TMFR1 這種認不得的代碼：不能說「換月完成」。"""
    (tmp_path / "state.json").write_text(json.dumps({"version": 1, "expiry": "2026-10-21", "done_until": 1.0, "attempts": 1, "gave_up": False,
                                                      "confirmed": False, "stale_code": "TMFJ6"}))
    rec, path = _run_loop(monkeypatch, tmp_path, {"TMF": "TMFR1"}, tpe(2026, 10, 21, 15, 5), iterations=2)
    assert rec.calls == [] and ro.load_state(path)["confirmed"] is False


def test_no_success_message_when_no_restart_was_requested(monkeypatch, tmp_path):
    rec, path = _run_loop(monkeypatch, tmp_path, {"TMF": "TMFK6"}, tpe(2026, 10, 25, 9, 0))
    assert rec.calls == []


def test_unparsable_codes_are_warned_once(monkeypatch, tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="core.rollover"):
        _run_loop(monkeypatch, tmp_path, {"TMF": "TMFR1"}, tpe(2026, 10, 21, 15, 0), iterations=3)
    assert len([r for r in caplog.records if "解析不出實際合約" in r.getMessage()]) == 1


def test_loop_is_off_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("ROLLOVER_RESTART", "false")
    rec = Rec()
    monkeypatch.setattr(ro, "notify", rec)
    asyncio.run(asyncio.wait_for(ro.rollover_loop(lambda: {"TMF": "TMFJ6"}, tmp_path / "s.json"), timeout=3))
    assert rec.calls == [] and ro.wanted() is None


def test_loop_survives_errors(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("no broker")
    monkeypatch.setattr(ro, "notify", Rec())
    n = {"i": 0}

    async def fake_sleep(_):
        n["i"] += 1
        if n["i"] > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(ro.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ro.rollover_loop(boom, tmp_path / "s.json"))
    assert n["i"] == 3                                      # 第一輪的例外被吞掉，迴圈照跑


def test_health_carries_the_flag_only_when_wanted(monkeypatch):
    import main
    monkeypatch.setattr(ro, "_wanted", None)
    assert "restart_wanted" not in main.health()
    monkeypatch.setattr(ro, "_wanted", "rollover")
    assert main.health()["restart_wanted"] == "rollover"
    assert main.health()["status"] == "ok" and "broker_connected" in main.health()


# ── broker 記下 worker 回報的解析結果 ─────────────────────────

def _client():
    c = BrokerClient()
    c._cmd_q = queue.Queue()
    return c


def test_broker_records_resolved_codes_from_subscribe_responses():
    c = _client()
    c._dispatch({"type": "response", "req_id": "1", "result": {"prefix": "TMF", "code": "TMFR1", "target_code": "TMFJ6"}})
    c._dispatch({"type": "response", "req_id": "2", "result": {"prefix": "TXF", "code": "TXFR1", "target_code": ""}})        # 沒有 target_code：退回 code
    c._dispatch({"type": "response", "req_id": "3", "result": True})                                                          # 一般回覆不受影響
    c._dispatch({"type": "response", "req_id": "4", "result": [{"code": "TMFJ6"}]})
    assert c.resolved_codes == {"TMF": "TMFJ6", "TXF": "TXFR1"}


def test_broker_reconnect_clears_and_resubscribes_with_request_ids():
    c = _client()
    c._known_subs = ["TMF", "MXF"]
    c.resolved_codes["TMF"] = "TMFI6"
    c._dispatch({"type": "connected"})
    assert c.resolved_codes == {}                                       # 新的 worker 會重新解析：舊紀錄作廢
    sent = [c._cmd_q.get_nowait() for _ in range(2)]
    assert [m["contract_code"] for m in sent] == ["TMF", "MXF"]
    assert all(m["method"] == "subscribe" and m.get("req_id") for m in sent)        # 帶 req_id，worker 才會回覆


def test_worker_subscribe_reply_includes_the_resolved_code():
    """worker 在 SDK 子行程裡跑，這裡只做靜態檢查：subscribe 的回覆要帶 prefix 與 target_code。"""
    src = (Path(__file__).resolve().parent.parent / "core" / "shioaji_worker.py").read_text(encoding="utf-8")
    i = src.index('if method == "subscribe":')
    block = src[i:i + 900]
    assert '"prefix"' in block and '"target_code"' in block and "getattr(c, \"target_code\"" in block
