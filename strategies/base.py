import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import shioaji as sj

from core.broker import broker

logger = logging.getLogger(__name__)

POINT_VALUE_TXF = 200   # 大台每點 200 元
POINT_VALUE_MXF = 50    # 小台每點 50 元
POINT_VALUE_TMF = 10    # 微台每點 10 元


@dataclass
class StrategyState:
    is_running: bool = False
    position: int = 0           # 正數=多單口數, 負數=空單口數
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    last_price: float = 0.0
    errors: list[str] = field(default_factory=list)


class BaseStrategy(ABC):
    name: str = "base"
    point_value: int = POINT_VALUE_TMF   # 微台每點 10 元

    def __init__(self) -> None:
        self.state = StrategyState()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop_loss_pts: int = 0    # 0 = 停用
        self.take_profit_pts: int = 0  # 0 = 停用

    @property
    def _base_params(self) -> dict[str, Any]:
        return {"stop_loss_pts": self.stop_loss_pts, "take_profit_pts": self.take_profit_pts}

    @property
    def _base_param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "stop_loss_pts",   "label": "停損點數（0=停用）", "type": "number", "min": 0, "max": 1000},
            {"key": "take_profit_pts", "label": "停利點數（0=停用）", "type": "number", "min": 0, "max": 1000},
        ]

    @property
    def params(self) -> dict[str, Any]:
        """子類別覆寫：回傳目前參數值"""
        return self._base_params

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        """子類別覆寫：回傳參數欄位定義（供前端動態渲染表單）"""
        return self._base_param_schema

    def _apply_params(self, params: dict[str, Any]) -> None:
        """子類別覆寫：套用外部傳入的參數"""
        pass

    def start(self, loop: asyncio.AbstractEventLoop, params: dict[str, Any] | None = None) -> None:
        if params:
            self.stop_loss_pts   = int(params.get("stop_loss_pts",   self.stop_loss_pts))
            self.take_profit_pts = int(params.get("take_profit_pts", self.take_profit_pts))
            self._apply_params(params)
        self._loop = loop
        self.state.is_running = True
        contract = broker.tmf_contract()
        broker.api.quote.subscribe(
            contract,
            quote_type=sj.constant.QuoteType.Quote,
            version=sj.constant.QuoteVersion.v1,
        )
        broker.api.set_order_callback(self._order_callback)
        self._setup_quote_callback()
        logger.info("策略 [%s] 已啟動", self.name)

    async def stop(self) -> None:
        self.state.is_running = False
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: broker.api.quote.unsubscribe(
                        broker.tmf_contract(),
                        quote_type=sj.constant.QuoteType.Quote,
                        version=sj.constant.QuoteVersion.v1,
                    ),
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("策略 [%s] 取消訂閱逾時，強制停止", self.name)
        except Exception as e:
            logger.warning("策略 [%s] 取消訂閱時發生錯誤: %s", self.name, e)
        logger.info("策略 [%s] 已停止", self.name)

    def _setup_quote_callback(self) -> None:
        def on_quote(exchange: sj.Exchange, quote: sj.QuoteFOPv1) -> None:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._on_quote_async(quote), self._loop
                )

        broker.api.set_on_quote_fop_v1_callback(on_quote)

    async def _check_sl_tp(self, price: float) -> bool:
        """檢查停損停利，觸發時自動平倉並回傳 True。"""
        if self.state.position == 0:
            return False
        pts = (price - self.state.entry_price) * (1 if self.state.position > 0 else -1)
        triggered = False
        if self.take_profit_pts > 0 and pts >= self.take_profit_pts:
            logger.info("策略 [%s] 停利觸發: +%.0f點 @ %.0f", self.name, pts, price)
            triggered = True
        elif self.stop_loss_pts > 0 and pts <= -self.stop_loss_pts:
            logger.info("策略 [%s] 停損觸發: %.0f點 @ %.0f", self.name, pts, price)
            triggered = True
        if triggered:
            action = sj.constant.Action.Sell if self.state.position > 0 else sj.constant.Action.Buy
            qty = abs(self.state.position)
            await self.place_order(action, qty)
            self.state.realized_pnl += pts * qty * self.point_value
            self.state.position = 0
            self.state.entry_price = 0.0
            self.state.unrealized_pnl = 0.0
        return triggered

    async def _on_quote_async(self, quote: sj.QuoteFOPv1) -> None:
        price = float(quote.close)
        self.state.last_price = price

        if self.state.position != 0:
            self.state.unrealized_pnl = (
                (price - self.state.entry_price)
                * self.state.position
                * self.point_value
            )

        if await self._check_sl_tp(price):
            return

        try:
            await self.on_quote(quote)
        except Exception as e:
            logger.error("策略 [%s] on_quote 發生錯誤: %s", self.name, e)
            self.state.errors.append(str(e))

    @abstractmethod
    async def on_quote(self, quote: sj.QuoteFOPv1) -> None:
        """子類別實作策略邏輯"""
        ...

    def _order_callback(self, stat: Any, msg: Any) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.on_order_event(stat, msg), self._loop
            )

    async def on_order_event(self, stat: Any, msg: Any) -> None:
        """可選覆寫：處理成交/委託回報"""
        logger.info("策略 [%s] 委託回報: %s", self.name, msg)

    def _margin_ok(self) -> bool:
        """正式盤才檢查可用保證金，模擬盤直接放行。"""
        if os.getenv("SIMULATION", "true").lower() == "true":
            return True
        try:
            m = broker.api.margin(broker.api.futopt_account)
            equity_amount = float(getattr(m, "equity_amount", 0) or 0)
            margin_call   = float(getattr(m, "margin_call",   0) or 0)
            if equity_amount <= 0:
                logger.warning("策略 [%s] 可用保證金不足 (equity_amount=%.0f)", self.name, equity_amount)
                return False
            if margin_call > 0:
                logger.warning("策略 [%s] 已觸發追繳 (margin_call=%.0f)，停止開倉", self.name, margin_call)
                return False
            return True
        except Exception as e:
            logger.warning("策略 [%s] 無法取得保證金資訊，略過檢查: %s", self.name, e)
            return True  # 查不到時不阻擋，避免過度保守

    async def place_order(
        self,
        action: sj.constant.Action,
        quantity: int = 1,
        price: Optional[float] = None,
        price_type: sj.constant.FuturesPriceType = sj.constant.FuturesPriceType.MKT,
        order_type: sj.constant.OrderType = sj.constant.OrderType.IOC,
    ):
        # 平倉單（降低風險）永遠放行；開倉單才做保證金檢查
        is_reducing = (
            (action == sj.constant.Action.Buy  and self.state.position < 0) or
            (action == sj.constant.Action.Sell and self.state.position > 0)
        )
        if not is_reducing and not self._margin_ok():
            self.state.errors.append("保證金不足，略過開倉")
            return None

        order = sj.FuturesOrder(
            action=action,
            price=price or 0,
            quantity=quantity,
            price_type=price_type,
            order_type=order_type,
            octype=sj.constant.FuturesOCType.Auto,
            account=broker.api.futopt_account,
        )
        loop = asyncio.get_running_loop()
        try:
            trade = await loop.run_in_executor(
                None, broker.api.place_order, broker.tmf_contract(), order
            )
            logger.info(
                "策略 [%s] 下單: %s %d口 @ %s",
                self.name, action.value, quantity, price or "市價"
            )
            return trade
        except Exception as e:
            logger.error("策略 [%s] 下單失敗: %s", self.name, e)
            self.state.errors.append(f"下單失敗: {e}")
            raise
