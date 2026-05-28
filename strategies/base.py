import asyncio
import logging
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

    @property
    def params(self) -> dict[str, Any]:
        """子類別覆寫：回傳目前參數值"""
        return {}

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        """子類別覆寫：回傳參數欄位定義（供前端動態渲染表單）"""
        return []

    def _apply_params(self, params: dict[str, Any]) -> None:
        """子類別覆寫：套用外部傳入的參數"""
        pass

    def start(self, loop: asyncio.AbstractEventLoop, params: dict[str, Any] | None = None) -> None:
        if params:
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

    def stop(self) -> None:
        self.state.is_running = False
        try:
            broker.api.quote.unsubscribe(
                broker.tmf_contract(),
                quote_type=sj.constant.QuoteType.Quote,
                version=sj.constant.QuoteVersion.v1,
            )
        except Exception as e:
            logger.warning("取消訂閱時發生錯誤: %s", e)
        logger.info("策略 [%s] 已停止", self.name)

    def _setup_quote_callback(self) -> None:
        def on_quote(exchange: sj.Exchange, quote: sj.QuoteFOPv1) -> None:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._on_quote_async(quote), self._loop
                )

        broker.api.set_on_quote_fop_v1_callback(on_quote)

    async def _on_quote_async(self, quote: sj.QuoteFOPv1) -> None:
        price = float(quote.close)
        self.state.last_price = price

        if self.state.position != 0:
            self.state.unrealized_pnl = (
                (price - self.state.entry_price)
                * self.state.position
                * self.point_value
            )

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

    def place_order(
        self,
        action: sj.constant.Action,
        quantity: int = 1,
        price: Optional[float] = None,
        price_type: sj.constant.FuturesPriceType = sj.constant.FuturesPriceType.MKT,
        order_type: sj.constant.OrderType = sj.constant.OrderType.IOC,
    ):
        order = sj.FuturesOrder(
            action=action,
            price=price or 0,
            quantity=quantity,
            price_type=price_type,
            order_type=order_type,
            octype=sj.constant.FuturesOCType.Auto,
            account=broker.api.futopt_account,
        )
        try:
            trade = broker.api.place_order(broker.tmf_contract(), order)
            logger.info(
                "策略 [%s] 下單: %s %d口 @ %s",
                self.name, action.value, quantity, price or "市價"
            )
            return trade
        except Exception as e:
            logger.error("策略 [%s] 下單失敗: %s", self.name, e)
            self.state.errors.append(f"下單失敗: {e}")
            raise
