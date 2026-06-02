import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from strategies.base import BaseStrategy, StrategyState
from strategies.ma_cross import MACrossStrategy
from strategies.breakout import BreakoutStrategy
from strategies.rsi import RSIStrategy
from strategies.bollinger import BollingerStrategy
from strategies.momentum import MomentumStrategy
from strategies.scalp import ScalpStrategy

logger = logging.getLogger(__name__)
router = APIRouter()


class StartRequest(BaseModel):
    params: dict[str, Any] = {}


class StrategyEngine:
    def __init__(self) -> None:
        self.strategies: dict[str, BaseStrategy] = {
            "ma_cross": MACrossStrategy(),
            "breakout": BreakoutStrategy(),
            "rsi": RSIStrategy(),
            "bollinger": BollingerStrategy(),
            "momentum": MomentumStrategy(),
            "scalp": ScalpStrategy(),
        }
        self.loop: asyncio.AbstractEventLoop | None = None

    async def stop_all(self) -> None:
        for s in self.strategies.values():
            if s.state.is_running:
                try:
                    await s.stop()
                except Exception as e:
                    logger.warning("停止策略時發生錯誤: %s", e)


strategy_engine = StrategyEngine()


def _strategy_summary(name: str, s: BaseStrategy) -> dict[str, Any]:
    return {
        "name": name,
        "is_running": s.state.is_running,
        "position": s.state.position,
        "entry_price": s.state.entry_price,
        "last_price": s.state.last_price,
        "unrealized_pnl": s.state.unrealized_pnl,
        "realized_pnl": s.state.realized_pnl,
        "errors": s.state.errors[-5:],
        "params": s.params,
        "param_schema": s.param_schema,
    }


@router.get("/")
def list_strategies() -> list[dict[str, Any]]:
    return [_strategy_summary(n, s) for n, s in strategy_engine.strategies.items()]


@router.get("/{name}")
def get_strategy(name: str) -> dict[str, Any]:
    s = strategy_engine.strategies.get(name)
    if not s:
        raise HTTPException(404, f"Strategy '{name}' not found")
    return _strategy_summary(name, s)


@router.post("/{name}/start")
def start_strategy(name: str, req: StartRequest) -> dict[str, str]:
    s = strategy_engine.strategies.get(name)
    if not s:
        raise HTTPException(404, f"Strategy '{name}' not found")
    if s.state.is_running:
        raise HTTPException(400, "Strategy already running")
    if strategy_engine.loop is None:
        raise HTTPException(503, "Event loop not ready")
    s.start(strategy_engine.loop, params=req.params or None)
    return {"status": "started", "name": name}


@router.post("/{name}/stop")
async def stop_strategy(name: str) -> dict[str, str]:
    s = strategy_engine.strategies.get(name)
    if not s:
        raise HTTPException(404, f"Strategy '{name}' not found")
    if not s.state.is_running:
        raise HTTPException(400, "Strategy not running")
    await s.stop()
    return {"status": "stopped", "name": name}


@router.get("/{name}/state", response_model=None)
def get_state(name: str) -> StrategyState:
    s = strategy_engine.strategies.get(name)
    if not s:
        raise HTTPException(404, f"Strategy '{name}' not found")
    return s.state
