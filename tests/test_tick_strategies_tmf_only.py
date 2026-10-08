"""逐 tick 策略（ma_cross／breakout／rsi／bollinger／momentum）只吃 TMF 的行情；scalp 與 K 棒策略維持舊行為。

背景（THRESHOLDS.md）：行情事件是 TMF/MXF/TXF 三個合約交錯進來的，約一半的相鄰兩筆是不同合約，
這幾個策略的價格序列、停損停利、未實現損益全被合約之間的價差來回拉扯。"""
from __future__ import annotations

import asyncio

import pytest

import strategies.base as base
from strategies.bollinger import BollingerStrategy
from strategies.breakout import BreakoutStrategy
from strategies.ma_cross import MACrossStrategy
from strategies.momentum import MomentumStrategy
from strategies.orb import ORBStrategy
from strategies.rsi import RSIStrategy
from strategies.scalp import ScalpStrategy
from strategies.vwap_revert import VWAPRevertStrategy

TICK_STRATEGIES = [MACrossStrategy, BreakoutStrategy, RSIStrategy, BollingerStrategy, MomentumStrategy]


def q(code: str, close: float) -> dict:
    return {"code": code, "close": close, "volume": 1, "tick_type": 1, "total_volume": 1, "ts": 1.0}


async def noop_order(*a, **k):
    return None


@pytest.fixture(autouse=True)
def filter_on(monkeypatch):
    monkeypatch.setattr(base, "QUOTE_PREFIX_FILTER", True)


@pytest.mark.parametrize("cls", TICK_STRATEGIES)
def test_other_contracts_never_reach_the_price_series(cls):
    s = cls()
    s.place_order = noop_order

    async def run():
        for i in range(40):
            await s._on_quote_async(q("TMFJ6", 100.0 + i))
            await s._on_quote_async(q("MXFJ6", 500.0))           # 完全不同的價位：只要混進來就一目了然
            await s._on_quote_async(q("TXFJ6", 900.0))

    asyncio.run(run())
    assert 500.0 not in s.prices and 900.0 not in s.prices and max(s.prices) < 200
    assert s.state.last_price == 139.0                              # 最後一筆 TMF，不是被 TXF 蓋掉


def test_ma_cross_does_not_churn_on_contract_interleaving(monkeypatch):
    def flips(filter_on: bool) -> int:
        monkeypatch.setattr(base, "QUOTE_PREFIX_FILTER", filter_on)
        s, calls = MACrossStrategy(), []

        async def fake_go(direction, price):
            calls.append(direction)

        s._go = fake_go

        async def run():
            for _ in range(200):
                await s._on_quote_async(q("TMFJ6", 100.0))        # TMF 價格完全不動
                await s._on_quote_async(q("MXFJ6", 104.0))        # MXF 固定高 4 點（正常的合約價差）

        asyncio.run(run())
        return len(calls)

    assert flips(True) <= 1                                         # 只看 TMF：價格沒變，不會一直切換訊號
    assert flips(False) > 100                                       # 舊行為：4 點的價差讓快慢線每一筆都交叉


def test_stop_loss_is_evaluated_on_tmf_prices_only():
    s = RSIStrategy()
    s.stop_loss_pts = 10
    s.state.position, s.state.entry_price = 1, 100.0
    orders = []

    async def fake_order(action, qty, **k):
        orders.append((action, qty, k.get("kind")))

    s.place_order = fake_order
    asyncio.run(s._on_quote_async(q("MXFJ6", 80.0)))                # 別的合約掉到 80：不是我的部位，不該觸發停損
    assert orders == [] and s.state.position == 1 and s.state.unrealized_pnl == 0.0
    asyncio.run(s._on_quote_async(q("TMFJ6", 89.0)))                # 自己的合約跌 11 點：照常觸發
    assert orders == [("Sell", 1, "sl")] and s.state.position == 0


def test_a_quote_without_a_code_is_ignored_by_the_filtered_strategies():
    s = BreakoutStrategy()
    asyncio.run(s._on_quote_async({"close": 100.0}))
    assert len(s.prices) == 0                                       # 不明合約的行情不進價格序列


@pytest.mark.parametrize("cls", [ScalpStrategy, ORBStrategy, VWAPRevertStrategy])
def test_scalp_and_bar_strategies_keep_their_old_behaviour(cls):
    assert cls.quote_prefix is None                                 # scalp 的外/內盤算法有自己的 flow_source；K 棒策略本來就只吃 TMF 的 bar


def test_the_switch_restores_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(base, "QUOTE_PREFIX_FILTER", False)         # TICK_STRATEGIES_TMF_ONLY=false
    s = BreakoutStrategy()
    s.place_order = noop_order
    asyncio.run(s._on_quote_async(q("MXFJ6", 500.0)))
    assert 500.0 in s.prices


def test_the_filter_is_on_by_default_in_production():
    """conftest 之外確認程式本身的預設是開的。用獨立子進程驗證，不在測試進程裡 reload 模組。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = "import strategies.base as b; print(b.QUOTE_PREFIX_FILTER)"

    def run(env_value):
        env = {k: v for k, v in os.environ.items() if k != "TICK_STRATEGIES_TMF_ONLY"}
        if env_value is not None:
            env["TICK_STRATEGIES_TMF_ONLY"] = env_value
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=120)
        return out.stdout.strip().splitlines()[-1]

    assert run(None) == "True" and run("true") == "True" and run("false") == "False"
