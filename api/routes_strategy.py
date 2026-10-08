import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.daily_summary import market_state
from strategies.base import BaseStrategy, StrategyState
from strategies.ma_cross import MACrossStrategy
from strategies.breakout import BreakoutStrategy
from strategies.rsi import RSIStrategy
from strategies.bollinger import BollingerStrategy
from strategies.momentum import MomentumStrategy
from strategies.scalp import ScalpStrategy
from strategies.orb import ORBStrategy
from strategies.vwap_revert import VWAPRevertStrategy

logger = logging.getLogger(__name__)
router = APIRouter()

# 停止策略前，除了策略自己記的部位，也向券商確認實際持倉（策略可能不知道：成交回報漏接、手動單、啟動前就有的部位）。
# STOP_POSITION_CHECK=false 恢復只看策略自己記的部位。
STOP_POSITION_CHECK = os.getenv("STOP_POSITION_CHECK", "true").lower() == "true"
STOP_CHECK_TIMEOUT_SEC = 3.0


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
            "orb": ORBStrategy(),
            "vwap_revert": VWAPRevertStrategy(),
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
        "events": s.state.events[-10:],
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


async def _sample_pnl() -> None:
    """啟停後立刻取樣一次策略日績效（日誌用），失敗不影響啟停。"""
    try:
        await market_state.sample_strategies(strategy_engine.strategies)
    except Exception as e:
        logger.debug("策略日績效取樣失敗: %r", e)


@router.post("/{name}/start")
async def start_strategy(name: str, req: StartRequest) -> dict[str, Any]:
    s = strategy_engine.strategies.get(name)
    if not s:
        raise HTTPException(404, f"Strategy '{name}' not found")
    if s.state.is_running:
        raise HTTPException(400, "Strategy already running")
    # 同帳戶、同合約（TMF）只允許單一策略執行，否則多策略會互相搶倉、
    # 透過 OcType.Auto 彼此平倉，造成幽靈部位與重複下單。
    running = [n for n, st in strategy_engine.strategies.items() if st.state.is_running]
    if running:
        raise HTTPException(
            409, f"已有策略執行中: {running[0]}（同帳戶同合約，請先停止再啟動其他策略）"
        )
    if strategy_engine.loop is None:
        raise HTTPException(503, "Event loop not ready")
    # 與今日市場狀態不符 → 只警告不擋（MARKET_STATE_GATE=off 可關閉）
    warning = market_state.check_strategy(name)
    await s.start(strategy_engine.loop, params=req.params or None)
    await _sample_pnl()
    resp: dict[str, Any] = {"status": "started", "name": name}
    if warning:
        logger.warning("策略 [%s] 啟動警告: %s", name, warning)
        resp["warning"] = warning
    return resp


async def _stop_risk(name: str, s: BaseStrategy) -> tuple[str, str]:
    """停止前的風險檢查，回傳 (種類, 描述)：
      ("strategy", 策略自己記的部位) ／ ("broker", 券商帳上的 TMF 部位，策略不知道) ／ ("unknown", 查不到券商的原因) ／ ("", "") 沒有風險。"""
    pos = s.state.position
    if pos != 0:
        return "strategy", f"{'多' if pos > 0 else '空'} {abs(pos)} 口（進場價 {s.state.entry_price:.0f}）"
    if not STOP_POSITION_CHECK:
        return "", ""
    try:
        net, gross, avg = await s._fetch_broker_tmf(timeout=STOP_CHECK_TIMEOUT_SEC)
    except Exception as e:
        return "unknown", f"{type(e).__name__}: {e}"
    if gross:
        side = "多" if net > 0 else "空" if net < 0 else "多空相抵"
        return "broker", f"{side} {abs(net)} 口（總 {gross} 口，均價 {avg:.0f}）"
    return "", ""


@router.post("/{name}/stop")
async def stop_strategy(name: str, force: bool = False) -> dict[str, str]:
    """停止策略。**有持倉時預設拒絕（409）**：停止會取消報價訂閱（策略不再檢查停損停利）並取消帳戶內所有未成交委託
    （含 scalp 掛在券商的停利單、使用者手動掛的限價單），部位會立刻失去保護（原本手冊寫「停止後停損停利照常執行」是錯的）。
    「有持倉」看兩邊：策略自己記的部位，以及券商帳上實際的 TMF 部位——策略可能不知道自己有部位（2026-10-08 成交回報漏接，
    策略以為空手，按停止沒被攔下）。查不到券商時也先不停止（fail-closed）。先手動平倉再停止；真的要停（例如策略失控）加 ?force=true。
    系統關機走 strategy_engine.stop_all()，不經過這個端點，不受影響。"""
    s = strategy_engine.strategies.get(name)
    if not s:
        raise HTTPException(404, f"Strategy '{name}' not found")
    if not s.state.is_running:
        raise HTTPException(400, "Strategy not running")
    kind, held = await _stop_risk(name, s)
    if kind:
        consequence = ("停止會取消報價訂閱（之後不再檢查停損停利），並取消帳戶內所有未成交委託"
                       "（含券商端的停利單與手動掛的限價單）")
        if kind == "strategy":
            head, tail = f"策略 {name} 目前持有{held}。", "部位會失去保護。請先到〈手動下單〉平倉；確定要這樣停止請加 ?force=true。"
        elif kind == "broker":
            head = f"策略 {name} 沒有記錄持倉，但券商帳上有 TMF {held}（可能是策略沒認出自己的成交，或手動單）。"
            tail = "這口部位不會有任何保護。請先到〈手動下單〉平倉；確定要這樣停止請加 ?force=true。"
        else:
            head = f"無法確認券商帳上有沒有 TMF 部位（{held}）。"
            tail = "為安全起見先不停止，請稍後再試；確定要停止請加 ?force=true。"
        if not force:
            raise HTTPException(409, f"{head}{consequence}，{tail}")
        logger.warning("策略 [%s] 被強制停止（%s：%s）：停損停利不再檢查、未成交委託將被取消", name, kind, held)
    await s.stop()
    await _sample_pnl()
    out = {"status": "stopped", "name": name}
    if kind == "strategy":
        out["warning"] = f"{name} 是在持倉中被強制停止的，目前持有{held}，已沒有任何停損停利保護，請立刻自行處理"
    elif kind == "broker":
        out["warning"] = f"{name} 是在券商帳上有持倉時被強制停止的：TMF {held}，已沒有任何停損停利保護，請立刻自行處理"
    elif kind == "unknown":
        out["warning"] = f"{name} 已強制停止，但當時無法確認券商帳上有沒有 TMF 部位（{held}），請立刻到〈部位〉確認"
    return out


@router.get("/{name}/state", response_model=None)
def get_state(name: str) -> StrategyState:
    s = strategy_engine.strategies.get(name)
    if not s:
        raise HTTPException(404, f"Strategy '{name}' not found")
    return s.state
