"""每天換證重啟（core/relogin.py，update.md 發現 80）：什麼時候該換、空手怎麼判、迴圈怎麼要求 watchdog、health 怎麼帶旗標。"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import core.relogin as rl
from core.daytrade import TAIPEI

ROOT = Path(__file__).resolve().parent.parent


def at(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=TAIPEI)


def login(dt: datetime) -> float:
    return dt.timestamp()


# ── decide：下午的空檔 ────────────────────────────────────────────

def test_afternoon_restarts_when_the_token_dies_before_tomorrows_gap():
    """昨天 14:00 登入（正常的錨點）：今天 14:00 的檢查要換，因為 token 今天 14:00 就到期、撐不到明天 13:50。"""
    now = at(2026, 11, 11, 14, 0, 10)
    r = rl.decide(now, login(at(2026, 11, 10, 14, 0, 40)))
    assert r and "11/11 14:00" in r and "空檔換證" in r


def test_afternoon_restarts_when_the_token_would_die_at_tomorrows_open():
    """今天 08:52 被假死重啟（現況）：14:00 就換，不要等到明天 08:52 死在日盤裡。"""
    assert rl.decide(at(2026, 11, 10, 14, 0, 5), login(at(2026, 11, 10, 8, 52, 56)))


def test_afternoon_skips_when_the_token_outlives_tomorrows_gap():
    """13:00 登入的（到期明天 13:00，在 13:50 前）還是要換；13:55 登入的（到期明天 13:55，撐得過明天的空檔開始）才不用。"""
    assert rl.decide(at(2026, 11, 10, 14, 0, 5), login(at(2026, 11, 10, 13, 0))) is not None
    assert rl.decide(at(2026, 11, 10, 14, 0, 5), login(at(2026, 11, 10, 13, 55))) is None


def test_afternoon_window_is_1400_to_1450_only():
    tok = login(at(2026, 11, 10, 8, 52))
    assert rl.decide(at(2026, 11, 10, 13, 59, 59), tok) is None                 # 目標時間之前不動（13:45 收盤後還在結算部位）
    assert rl.decide(at(2026, 11, 10, 14, 0, 0), tok)
    assert rl.decide(at(2026, 11, 10, 14, 50, 0), tok)
    assert rl.decide(at(2026, 11, 10, 14, 50, 1), tok) is None                  # 15:00 夜盤要開了，太晚
    assert rl.decide(at(2026, 11, 10, 15, 30), tok) is None


def test_weekend_keeps_the_daily_rhythm():
    """週五 14:00 換過 → 週六、週日 14:00 也換（不然週一 14:00 前就到期：11/09 的 08:48 就是這樣來的）。"""
    fri = at(2026, 11, 6, 14, 0, 30)
    assert rl.decide(at(2026, 11, 7, 14, 0, 10), login(fri))                     # 週六
    assert rl.decide(at(2026, 11, 8, 14, 0, 10), login(at(2026, 11, 7, 14, 0, 30)))   # 週日
    assert rl.decide(at(2026, 11, 9, 14, 0, 10), login(at(2026, 11, 8, 14, 0, 30)))   # 週一照常


# ── decide：早上的補救空檔 ─────────────────────────────────────────

def test_morning_restarts_only_when_the_token_dies_during_the_day_session():
    tok_bad = login(at(2026, 11, 9, 8, 52))                                       # 到期 11/10 08:52：日盤裡
    tok_ok = login(at(2026, 11, 9, 14, 0, 30))                                    # 到期 11/10 14:00：空檔裡，下午那次會處理
    assert rl.decide(at(2026, 11, 10, 8, 0, 5), tok_bad) and "日盤裡" in rl.decide(at(2026, 11, 10, 8, 0, 5), tok_bad)
    assert rl.decide(at(2026, 11, 10, 8, 0, 5), tok_ok) is None
    tok_edge = login(at(2026, 11, 9, 13, 44))                                     # 到期 11/10 13:44：收盤前一分鐘，還是日盤
    assert rl.decide(at(2026, 11, 10, 8, 10), tok_edge)
    assert rl.decide(at(2026, 11, 10, 8, 10), login(at(2026, 11, 9, 13, 46))) is None   # 13:46 到期：已經收盤


def test_morning_window_is_0800_to_0830_only():
    tok = login(at(2026, 11, 9, 8, 52))
    assert rl.decide(at(2026, 11, 10, 7, 59, 59), tok) is None
    assert rl.decide(at(2026, 11, 10, 8, 0), tok)
    assert rl.decide(at(2026, 11, 10, 8, 30), tok)
    assert rl.decide(at(2026, 11, 10, 8, 30, 1), tok) is None                     # 08:45 要開盤了
    assert rl.decide(at(2026, 11, 10, 5, 30), tok) is None                        # 05:05～08:00 不動：寧可晚一點換，token 撐得久一點


def test_a_token_that_lasts_exactly_to_tomorrows_gap_is_kept():
    """到期剛好在明天 13:50:00：撐得到明天的空檔開始，不換（晚一秒就要換）。"""
    exact = login(at(2026, 11, 10, 13, 50, 0))                                     # 到期 11/11 13:50:00
    assert rl.decide(at(2026, 11, 10, 14, 30), exact) is None
    assert rl.decide(at(2026, 11, 10, 14, 30), exact - 1) is not None


def test_an_already_expired_token_is_still_renewed_in_a_window():
    """昨天 13:30 登入、今天 13:30 已經到期（空檔裡沒人發現）：14:00 一樣換。"""
    assert rl.decide(at(2026, 11, 10, 14, 1), login(at(2026, 11, 9, 13, 30)))


def test_no_login_time_means_nothing():
    assert rl.decide(at(2026, 11, 10, 14, 1), None) is None


def test_steady_state_is_exactly_one_restart_a_day():
    """模擬 10 天：每 30 秒檢查一次、該換就換（登入在要求後 40 秒完成）：每天剛好一次、都在 14:00～14:01。"""
    now = at(2026, 11, 10, 8, 53)
    tok = login(now - timedelta(seconds=10))
    restarts = []
    end = now + timedelta(days=10)
    while now < end:
        if rl.decide(now, tok) and not restarts or (rl.decide(now, tok) and now - restarts[-1] > timedelta(minutes=5)):
            restarts.append(now)
            tok = login(now + timedelta(seconds=40))
        now += timedelta(seconds=30)
    assert len(restarts) == 10, [f"{r:%m/%d %H:%M:%S}" for r in restarts]
    assert all(r.hour == 14 and r.minute <= 1 for r in restarts), [f"{r:%m/%d %H:%M:%S}" for r in restarts]
    assert restarts[0].date() == at(2026, 11, 10, 0, 0).date()                   # 今天下午就先換，不等明天 08:52 死在日盤


def test_token_never_expires_inside_a_session_in_steady_state():
    """接著上面：穩態下每個到期時間都落在 13:50～15:00 的空檔。"""
    now = at(2026, 11, 10, 8, 53)
    tok = login(now - timedelta(seconds=10))
    expiries = []
    end = now + timedelta(days=7)
    while now < end:
        if rl.decide(now, tok):
            tok = login(now + timedelta(seconds=40))
            expiries.append(rl.expiry(tok))
        now += timedelta(seconds=30)
    assert expiries and all(e.hour == 14 and e.minute <= 2 for e in expiries), expiries


# ── 空手 ────────────────────────────────────────────────────────

def test_busy_reason():
    flat = rl.busy_reason([], 5.0, False, [], False)
    assert flat is None
    assert rl.busy_reason([{"code": "TMFK6", "quantity": 1}], 5.0, False, [], False) == "有部位（1 筆）"
    assert rl.busy_reason([{"code": "TMFK6", "quantity": 0}], 5.0, False, [], False) is None           # 數量 0 的殘影不算
    assert rl.busy_reason([{"code": "TMFK6", "quantity": "2"}], 5.0, False, [], False) == "有部位（1 筆）"
    assert rl.busy_reason([{"code": "TMFK6", "quantity": -1}], 5.0, False, [], False) == "有部位（1 筆）"     # 負數（空單用負數表示的話）也算
    assert rl.busy_reason([], 5.0, True, [], False) == "有未完成的委託"
    assert rl.busy_reason([], 5.0, False, [{"id": "w1"}], False) == "有手動停損停利監控（1 個）"
    assert rl.busy_reason([], 5.0, False, [], True) == "有策略在跑"
    assert rl.busy_reason([], -1, False, [], False) == "部位快取不是最新的（還沒拿到過）"
    assert rl.busy_reason([], None, False, [], False).startswith("部位快取不是最新的")
    assert rl.busy_reason(None, 5.0, False, [], False).startswith("部位快取不是最新的")
    assert rl.busy_reason([], rl.MAX_POSITIONS_AGE + 1, False, [], False) == f"部位快取不是最新的（{rl.MAX_POSITIONS_AGE + 1:.0f} 秒前）"
    assert rl.busy_reason([], rl.MAX_POSITIONS_AGE, False, [], False) is None
    assert rl.MAX_POSITIONS_AGE >= 300                                             # 週末部位 120 秒刷新一次，要容得下幾次沒刷到


# ── 迴圈 ────────────────────────────────────────────────────────

def _run(monkeypatch, nows, login_at, busy, iterations=None):
    """跑 relogin_loop：每次 sleep 之後 datetime.now 回 nows 裡的下一個值。回傳最後的 wanted 與 busy 被問了幾次。"""
    it = iter(nows)
    cur = {"now": nows[0]}

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cur["now"]

    asked = {"n": 0}

    def get_busy():
        asked["n"] += 1
        return busy() if callable(busy) else busy

    n = {"i": 0}

    async def fake_sleep(_):
        n["i"] += 1
        try:
            cur["now"] = next(it)
        except StopIteration:
            raise asyncio.CancelledError

    monkeypatch.setattr(rl, "datetime", FakeDT)
    monkeypatch.setattr(rl.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(rl, "_wanted", None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rl.relogin_loop(lambda: login_at, get_busy))
    return rl.wanted(), asked["n"]


def test_loop_requests_a_restart_once_when_flat(monkeypatch):
    nows = [at(2026, 11, 10, 14, 0, 10), at(2026, 11, 10, 14, 0, 40), at(2026, 11, 10, 14, 1, 10)]
    wanted, asked = _run(monkeypatch, nows, login(at(2026, 11, 10, 8, 52)), None)
    assert wanted == "relogin" and asked == 1                                       # 要求過就不再問空手


def test_loop_waits_while_busy_then_requests(monkeypatch, caplog):
    seq = iter(["有部位（1 筆）", "有部位（1 筆）", None])
    nows = [at(2026, 11, 10, 14, 0, 10), at(2026, 11, 10, 14, 0, 40), at(2026, 11, 10, 14, 1, 10)]
    with caplog.at_level("INFO", logger="core.relogin"):
        wanted, asked = _run(monkeypatch, nows, login(at(2026, 11, 10, 8, 52)), lambda: next(seq))
    assert wanted == "relogin" and asked == 3
    assert sum("換證重啟延後" in r.message for r in caplog.records) == 1          # 同一個理由只記一次


def test_loop_does_nothing_outside_the_windows(monkeypatch):
    nows = [at(2026, 11, 10, 9, 0), at(2026, 11, 10, 13, 0), at(2026, 11, 10, 16, 0)]
    wanted, asked = _run(monkeypatch, nows, login(at(2026, 11, 10, 8, 52)), None)
    assert wanted is None and asked == 0


def test_loop_withdraws_the_request_when_the_window_passes(monkeypatch, caplog):
    """要求了但沒人重啟（沒有 watchdog）：空檔過了就收回，不然之後 watchdog 一起來就在盤中重啟。"""
    nows = [at(2026, 11, 10, 14, 0, 10), at(2026, 11, 10, 14, 51)]
    with caplog.at_level("WARNING", logger="core.relogin"):
        wanted, _ = _run(monkeypatch, nows, login(at(2026, 11, 10, 8, 52)), None)
    assert wanted is None and any("收回重啟要求" in r.message for r in caplog.records)


def test_loop_survives_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("x")
    nows = [at(2026, 11, 10, 14, 0, 10), at(2026, 11, 10, 14, 0, 40)]
    wanted, _ = _run(monkeypatch, nows, login(at(2026, 11, 10, 8, 52)), boom)
    assert wanted is None                                                           # 空手檢查壞了就不重啟，迴圈也不死


def test_loop_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RELOGIN_RESTART", "false")
    monkeypatch.setattr(rl, "_wanted", None)

    async def no_sleep(_):
        raise AssertionError("關掉了就不該進迴圈")
    monkeypatch.setattr(rl.asyncio, "sleep", no_sleep)
    asyncio.run(asyncio.wait_for(rl.relogin_loop(lambda: 0.0, lambda: None), 2))   # 直接結束、不丟、不睡
    assert rl.wanted() is None
    monkeypatch.setenv("RELOGIN_RESTART", "true")
    assert rl.enabled()
    monkeypatch.setenv("RELOGIN_RESTART", "FALSE")
    assert not rl.enabled()


# ── 接線（靜態）────────────────────────────────────────────────

def test_main_wires_health_and_the_task():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "elif relogin.wanted():" in src and 'out["restart_wanted"] = relogin.wanted()' in src
    assert src.index("if rollover.wanted():") < src.index("elif relogin.wanted():")       # 換月優先
    assert re.search(r"relogin_task = loop\.create_task\(relogin\.relogin_loop\(lambda: session_watch\.connected_at, _relogin_busy\)\)", src)
    assert '("換證", relogin_task)' in src and "relogin_task.cancel()" in src
    body = src[src.index("def _relogin_busy"):src.index("@asynccontextmanager")]
    for piece in ("get_positions()", "positions_age_sec", "has_unfinished(_trades_cache)", "manual_monitor.list_watches()", "_strategy_running()"):
        assert piece in body, piece
    assert "await" not in body and "broker." not in body                            # 只讀快取，不查券商


def test_watchdog_scripts_treat_relogin_like_rollover():
    for name in ("run_live.sh", "run_sim.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert '*\'"restart_wanted":"relogin"\'*) echo "relogin" ;;' in text, name
        i = text.index('if [ "$state" = "rollover" ] || [ "$state" = "relogin" ]; then')
        branch = text[i:text.index('if [ "$state" = "healthy" ]; then')]
        assert "換證：後端要求重啟" in branch and "ROLLOVER_MIN_GAP" in branch and "planned=1" in branch, name
