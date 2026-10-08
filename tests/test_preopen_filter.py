"""開盤前試算行情的隔離（core/quote_hub.py）：只顯示在畫面，不餵策略／K 棒／即時狀態／落地。

背景（THRESHOLDS.md、update.md 發現 10）：08:30~08:45 的行情是試算價，TMF/MXF/TXF 同一分鐘的價差平均 189 點、最大 270 點，
vwap_revert 若在 08:30 前啟動，VWAP 被試算 K 棒拉低，08:45 第一根就會被誤導做空。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest

import core.quote_hub as qh
from core.live_state import LiveState
from core.shioaji_worker import _extract_quote


def at(h: int, m: int, s: int = 0, day: int = 8) -> float:
    return time.mktime((2026, 10, day, h, m, s, 0, 0, -1))


def snap(t: float, price: float = 49500.0, code: str = "TMFJ6", vol: int = 1, total: int = 10, simtrade: bool = False) -> dict:
    return {"code": code, "close": price, "volume": vol, "total_volume": total, "tick_type": 1, "ts": t, "simtrade": simtrade}


class Rig:
    """把 QuoteHub 的下游換成假的，記錄誰收到了行情（conftest 預設關掉隔離，這裡打開）。"""

    def __init__(self, monkeypatch, real_bars: bool = False):
        monkeypatch.setattr(qh, "FILTER_PREOPEN", True)
        self.recorded: list = []
        self.fed_bars: list = []
        self.strategy_got: list[float] = []
        self.done_bars: list = []
        self.ls = LiveState()
        monkeypatch.setattr(qh, "live_state", self.ls)
        monkeypatch.setattr(qh, "tick_recorder", SimpleNamespace(record=lambda *a: self.recorded.append(a)))
        self.hub = qh.QuoteHub()
        if not real_bars:
            self.hub.bars = SimpleNamespace(feed=lambda *a: self.fed_bars.append(a) or None)

    async def run(self, *snaps: dict) -> "Rig":
        self.hub.setup(asyncio.get_running_loop())
        self.ws: asyncio.Queue = asyncio.Queue()

        async def on_quote(s):
            self.strategy_got.append(s["close"])

        async def on_bar(b):
            self.done_bars.append(b)

        self.hub.subscribe_strategy("t", on_quote)
        self.hub.subscribe_strategy_bars("t", on_bar)
        self.hub.add_ws_client(self.ws)
        for s in snaps:
            self.hub._inject_quote(s)
        await asyncio.sleep(0.05)
        return self


@pytest.mark.parametrize("h,m,s,expect", [
    (8, 29, 59, False), (8, 30, 0, True), (8, 44, 59, True), (8, 45, 0, False),      # 日盤開盤前 15 分鐘
    (14, 49, 59, False), (14, 50, 0, True), (14, 59, 59, True), (15, 0, 0, False),    # 夜盤開盤前 10 分鐘
    (3, 0, 0, False), (10, 0, 0, False), (13, 45, 0, False),
])
def test_window_boundaries(h, m, s, expect):
    assert qh.in_preopen(at(h, m, s)) is expect


def test_preopen_tick_is_only_shown_on_screen(monkeypatch):
    rig = asyncio.run(Rig(monkeypatch).run(snap(at(8, 31), price=49479.0, simtrade=True)))
    assert rig.strategy_got == [] and rig.recorded == [] and rig.fed_bars == []       # 策略／落地／K 棒都沒收到
    assert rig.ls.snapshot()["flow"]["100"]["n"] == 0 and rig.ls.last_price is None   # 買賣力道與即時狀態也沒算進去
    assert json.loads(rig.ws.get_nowait())["close"] == 49479.0                        # 畫面照常看得到（維持原本的顯示）
    assert rig.hub.get_last_price("TMFJ6") == 49479.0                                 # 最新價照舊更新
    assert rig.hub.daily_ohlc()["TMFJ6"]["high"] == 0.0                               # 但試算價不算日高日低


def test_regular_tick_is_untouched(monkeypatch):
    rig = asyncio.run(Rig(monkeypatch).run(snap(at(9, 0), price=49500.0)))
    assert rig.strategy_got == [49500.0] and len(rig.recorded) == 1 and len(rig.fed_bars) == 1
    assert rig.ls.snapshot()["flow"]["100"]["n"] == 1
    assert json.loads(rig.ws.get_nowait())["close"] == 49500.0
    assert rig.hub.daily_ohlc()["TMFJ6"] == {"last": 49500.0, "high": 49500.0, "low": 49500.0}


def test_the_switch_restores_the_old_behaviour(monkeypatch):
    rig = Rig(monkeypatch)
    monkeypatch.setattr(qh, "FILTER_PREOPEN", False)                                  # FILTER_PREOPEN_QUOTES=false
    asyncio.run(rig.run(snap(at(8, 31), price=49479.0)))
    assert rig.strategy_got == [49479.0] and len(rig.recorded) == 1 and len(rig.fed_bars) == 1
    assert rig.ls.snapshot()["flow"]["100"]["n"] == 1


def test_every_contract_is_isolated_not_just_tmf(monkeypatch):
    rig = asyncio.run(Rig(monkeypatch).run(
        snap(at(8, 31, 1), 49770.0, "TXFJ6"), snap(at(8, 31, 2), 49687.0, "MXFJ6"), snap(at(8, 31, 3), 49479.0, "TMFJ6")))
    assert rig.strategy_got == [] and rig.recorded == []
    assert rig.ws.qsize() == 3                                                        # 畫面三個合約都還是看得到


def test_preopen_bars_never_reach_the_strategies(monkeypatch):
    """用真的 BarBuilder：盤前的試算 K 棒不存在，策略收到的第一根就是 08:45 的日盤 K 棒——VWAP 不會被試算價拉低。"""
    rig = Rig(monkeypatch, real_bars=True)
    ticks = [snap(at(8, 30, 10), 49463.0, total=1), snap(at(8, 31, 10), 49474.0, total=2), snap(at(8, 44, 50), 49500.0, total=3),
             snap(at(8, 45, 10), 49535.0, total=4), snap(at(8, 45, 40), 49540.0, total=5), snap(at(8, 46, 5), 49543.0, total=6)]
    asyncio.run(rig.run(*ticks))
    assert [(time.strftime("%H:%M", time.localtime(b.ts)), b.open, b.close) for b in rig.done_bars] == [("08:45", 49535.0, 49540.0)]


def test_logs_entering_and_leaving_with_counts(monkeypatch, caplog):
    rig = Rig(monkeypatch)
    with caplog.at_level(logging.INFO, logger=qh.logger.name):
        asyncio.run(rig.run(snap(at(8, 30, 5), 49470.0, total=1, simtrade=True), snap(at(8, 31), 49471.0, total=2, simtrade=True),
                            snap(at(8, 32), 49472.0, total=3), snap(at(8, 45, 0), 49500.0, total=4)))
    assert caplog.text.count("進入開盤前試算時段") == 1
    assert "開盤前試算時段結束：略過 3 筆試算行情（其中帶 simtrade 旗標 2 筆）" in caplog.text
    assert rig.strategy_got == [49500.0]                                              # 08:45:00 起才是真實行情


def test_simtrade_flag_outside_the_window_is_reported_but_never_dropped(monkeypatch, caplog):
    """旗標若誤判（例如恆為 True），丟掉真實行情會讓策略變瞎子、停損失效——所以只靠時段隔離，旗標只記錄。"""
    rig = Rig(monkeypatch)
    ticks = [snap(at(10, 0, i), 49500.0 + i, total=i + 1, simtrade=True) for i in range(8)]
    with caplog.at_level(logging.WARNING, logger=qh.logger.name):
        asyncio.run(rig.run(*ticks))
    assert len(rig.strategy_got) == 8 and len(rig.recorded) == 8
    assert caplog.text.count("試算時段以外收到 simtrade") == 5                          # 最多記 5 次，不洗版


def test_worker_carries_the_simtrade_flag():
    base = dict(close=100.0, code="TMFJ6", ts=1_000_000_000, volume=1, total_volume=5, tick_type=1)
    assert _extract_quote(SimpleNamespace(**base, simtrade=True))["simtrade"] is True
    assert _extract_quote(SimpleNamespace(**base, simtrade=False))["simtrade"] is False
    assert _extract_quote(SimpleNamespace(**base))["simtrade"] is False               # SDK 沒這欄 → 當一般行情，維持舊行為


def test_the_filter_is_on_by_default_in_production():
    """conftest 為了測試穩定把它關掉；確認程式本身的預設是開的。用獨立子進程驗證，不在測試進程裡 reload 模組（會讓其他模組拿到舊的單例）。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = "import core.quote_hub as q; print(q.FILTER_PREOPEN)"

    def run(env_value):
        env = {k: v for k, v in os.environ.items() if k != "FILTER_PREOPEN_QUOTES"}
        if env_value is not None:
            env["FILTER_PREOPEN_QUOTES"] = env_value
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        return out.stdout.strip().splitlines()[-1]

    assert run(None) == "True" and run("true") == "True" and run("false") == "False"
