"""風控的「當日」＝交易日（15:00 起算）；當日損益 = 累計損益 − 換日當下的基準。

背景（2026-10-08，update.md 發現 20）：舊的 _risk_ok 比的是 state.realized_pnl + unrealized_pnl，而 realized_pnl 只會累加、
連 start() 都不重設——「當日最大虧損」其實是後端啟動以來的累計：昨天虧 2,500 → 今天一開始就被鎖；
昨天賺 3,000、今天虧 4,000 → 累計 -1,000 沒到上限，還能繼續開倉。另外換日用日曆日（00:00），夜盤（15:00～05:00）被切成兩半。"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

import pytest

import strategies.base as base
from strategies.base import BaseStrategy, risk_day_key

ROOT = Path(__file__).resolve().parent.parent


class Probe(BaseStrategy):
    """最小的具體策略：不下單、不碰券商，只走基底的風控與損益記帳。"""
    name = "probe"

    async def on_quote(self, quote: dict) -> None:
        pass


class Clock:
    def __init__(self, *args: int) -> None:
        self.dt = datetime(*args)

    def __call__(self) -> datetime:
        return self.dt

    def at(self, *args: int) -> None:
        self.dt = datetime(*args)


@pytest.fixture(autouse=True)
def default_day_start(monkeypatch):
    monkeypatch.setattr(base, "RISK_DAY_START", 1500)          # 不受 .env 的 RISK_DAY_START 影響


@pytest.fixture
def probe(monkeypatch):
    s = Probe()
    s.daily_max_loss = 2000
    clock = Clock(2026, 10, 5, 10, 0, 0)
    monkeypatch.setattr(s, "_now", clock)
    return s, clock


def tick(s: BaseStrategy, price: float) -> None:
    asyncio.run(s._on_quote_async({"code": "TMFJ6", "close": price}))


# ── 換日點 ────────────────────────────────────────────────────────

def test_the_trading_day_changes_at_1500_not_at_midnight():
    k = lambda *a: risk_day_key(datetime(*a))                  # noqa: E731
    assert k(2026, 10, 5, 14, 59) != k(2026, 10, 5, 15, 0)
    one_day = [k(2026, 10, 5, 15, 0), k(2026, 10, 5, 23, 59), k(2026, 10, 6, 0, 0), k(2026, 10, 6, 4, 59),
               k(2026, 10, 6, 8, 45), k(2026, 10, 6, 13, 44), k(2026, 10, 6, 14, 59)]
    assert len(set(one_day)) == 1                              # 夜盤＋隔天日盤是同一個交易日
    assert k(2026, 10, 6, 15, 0) != one_day[0]
    assert k(2026, 10, 9, 15, 0) == k(2026, 10, 10, 4, 59)     # 週五夜盤跨到週六凌晨也一樣


def test_start_of_zero_means_calendar_day():
    assert risk_day_key(datetime(2026, 10, 5, 23, 59), 0) != risk_day_key(datetime(2026, 10, 6, 0, 0), 0)
    assert risk_day_key(datetime(2026, 10, 5, 10, 0), 0) == risk_day_key(datetime(2026, 10, 5, 23, 59), 0)


@pytest.mark.parametrize("raw,expected", [("1500", 1500), ("0", 0), ("845", 845), ("2359", 2359),
                                          ("2400", 1500), ("1560", 1500), ("-100", 1500), ("abc", 1500), ("", 1500)])
def test_env_value_is_validated(raw, expected):
    assert base._parse_hhmm(raw, 1500) == expected


# ── 昨天的盈虧不該影響今天 ────────────────────────────────────────

def test_yesterdays_loss_does_not_lock_today(probe):
    s, clock = probe
    s._add_realized(-2500)
    assert s._risk_ok()[0] is False                            # 當天虧過上限：當天本來就該被鎖
    clock.at(2026, 10, 5, 15, 0, 1)                            # 換交易日
    ok, why = s._risk_ok()
    assert ok, why
    assert s._day_pnl() == 0
    assert s.state.realized_pnl == -2500                       # 累計不變（面板與策略日誌用的是累計，不能被改掉）


def test_yesterdays_profit_does_not_cover_todays_loss(probe):
    s, clock = probe
    s._add_realized(+3000)
    clock.at(2026, 10, 5, 15, 30)
    s._add_realized(-4000)                                     # 累計 -1,000 沒到 -2,000，但今天已經虧 4,000
    ok, why = s._risk_ok()
    assert not ok and "-4000" in why and "-2000" in why


def test_base_is_taken_once_per_day(probe):
    s, clock = probe
    clock.at(2026, 10, 5, 15, 5)
    s._add_realized(-300)
    clock.at(2026, 10, 5, 20, 0)
    s._add_realized(-200)
    s._roll_risk_day()
    assert s._day_pnl() == -500                                # 不會每次呼叫都重新取基準


# ── 夜盤跨午夜 ────────────────────────────────────────────────────

def test_the_night_session_is_one_day_across_midnight(probe):
    s, clock = probe
    s.max_trades_per_day = 2
    clock.at(2026, 10, 5, 16, 0)
    s._add_realized(-1500)
    s._trades_today = 2
    clock.at(2026, 10, 6, 0, 30)
    ok, why = s._risk_ok()
    assert not ok and "進場已達 2 次上限" in why                # 午夜沒有歸零
    s._add_realized(-600)                                      # 凌晨再虧：和 16:00 的 -1,500 合計 -2,100
    assert s._risk_ok()[0] is False and s._risk_halted
    clock.at(2026, 10, 6, 9, 0)                                # 隔天日盤仍是同一個交易日
    assert s._risk_ok()[0] is False
    clock.at(2026, 10, 6, 15, 0, 1)                            # 下一個夜盤開盤才換日
    ok, why = s._risk_ok()
    assert ok, why
    assert s._trades_today == 0 and s._risk_halted is False


@pytest.mark.parametrize("hh,mm,ok", [(15, 0, True), (23, 0, True), (2, 0, True), (4, 59, True),
                                      (5, 1, False), (10, 0, False), (14, 59, False)])
def test_overnight_trade_window_still_works(probe, hh, mm, ok):
    s, clock = probe
    s.daily_max_loss = 0
    s.trade_start_hhmm, s.trade_end_hhmm = 1500, 500           # 夜盤：start > end 代表跨午夜
    clock.at(2026, 10, 6, hh, mm)
    assert s._risk_ok()[0] is ok


# ── 基準必須在換日後的第一筆損益變動之前取 ─────────────────────────

def test_the_first_event_of_a_new_day_can_be_a_realized_loss(probe):
    s, clock = probe
    clock.at(2026, 10, 5, 14, 0)
    s._add_realized(100)
    clock.at(2026, 10, 5, 15, 0, 5)
    s._add_realized(-2500)         # 例如 scalp 的停損／停利成交回報：它前面沒有任何行情或進場檢查來替它換日
    ok, why = s._risk_ok()
    assert not ok and "-2500" in why


def test_a_gap_across_the_boundary_belongs_to_the_new_day(probe):
    s, clock = probe
    s.state.position, s.state.entry_price = 1, 20000.0
    clock.at(2026, 10, 5, 13, 44)
    tick(s, 20100.0)                                           # 日盤收盤前：未實現 +1,000
    assert s.state.unrealized_pnl == 1000
    clock.at(2026, 10, 5, 15, 0, 1)
    tick(s, 20050.0)                                           # 夜盤第一筆跳空 -50 點：未實現 +500
    assert s.state.unrealized_pnl == 500 and s._day_pnl() == -500
    s.daily_max_loss = 600
    assert s._risk_ok()[0] is True
    s.daily_max_loss = 400
    ok, why = s._risk_ok()
    assert not ok and "-500" in why


def test_take_profit_on_the_first_tick_after_the_boundary(probe, monkeypatch):
    s, clock = probe
    s.take_profit_pts = 10

    async def no_order(*a, **k):
        return None

    monkeypatch.setattr(s, "place_order", no_order)
    s.state.position, s.state.entry_price = 1, 20000.0
    clock.at(2026, 10, 5, 13, 44)
    tick(s, 20005.0)                                           # 未實現 +50
    clock.at(2026, 10, 5, 15, 0, 1)
    tick(s, 20010.0)                                           # 停利：已實現 +100
    assert s.state.position == 0 and s.state.realized_pnl == 100
    assert s._day_pnl() == 50                                  # 新的一天只多賺 5 點（50 元），不是 100


# ── 停止再啟動 ────────────────────────────────────────────────────

def test_restarting_in_the_same_trading_day_does_not_reset_the_loss(probe, monkeypatch):
    s, clock = probe

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(s, "_cancel_all_pending", noop)
    monkeypatch.setattr(s, "_sync_position_from_broker", noop)
    monkeypatch.setattr(base.broker, "subscribe", noop)
    monkeypatch.setattr(base.broker, "set_order_callback", lambda cb: None)
    monkeypatch.setattr(base.quote_hub, "subscribe_strategy", lambda *a, **k: None)
    loop = asyncio.new_event_loop()
    try:
        clock.at(2026, 10, 5, 10, 0)
        loop.run_until_complete(s.start(loop, {"daily_max_loss": 2000}))
        s._add_realized(-2500)
        assert not s._risk_ok()[0]
        s.state.is_running = False

        clock.at(2026, 10, 5, 10, 30)
        loop.run_until_complete(s.start(loop, {"daily_max_loss": 2000}))
        assert s._trades_today == 0                            # 維持舊行為：每次啟動重新計進場次數
        ok, why = s._risk_ok()
        assert not ok and "-2500" in why                       # 但當日虧損還在：重啟策略繞不過上限
        s.state.is_running = False

        clock.at(2026, 10, 5, 15, 30)                          # 下一個交易日再啟動：從 0 開始
        loop.run_until_complete(s.start(loop, {"daily_max_loss": 2000}))
        assert s._risk_ok()[0] is True and s._day_pnl() == 0
    finally:
        loop.close()


# ── 其他 ──────────────────────────────────────────────────────────

def test_the_limit_is_off_when_zero(probe):
    s, _ = probe
    s.daily_max_loss = 0
    s._add_realized(-10_000_000)
    assert s._risk_ok() == (True, "")


def test_no_strategy_updates_realized_pnl_behind_the_helpers_back():
    """已實現損益一律走 _add_realized（換日基準要在它之前取）。新增策略若直接 realized_pnl += …，這裡會擋下來。"""
    offenders = []
    for f in sorted((ROOT / "strategies").glob("*.py")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![A-Za-z_])realized_pnl\s*[-+*]?=", line) and "self.state.realized_pnl += amount" not in line:
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert not offenders, offenders


def test_param_labels_say_which_day_the_limits_count():
    from api.routes_strategy import strategy_engine
    assert strategy_engine.strategies
    for s in strategy_engine.strategies.values():
        labels = {p["key"]: p["label"] for p in s.param_schema}
        assert "交易日" in labels["daily_max_loss"] and "交易日" in labels["max_trades_per_day"], s.name
