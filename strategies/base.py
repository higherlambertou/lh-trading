import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from core.broker import broker
from core.quote_hub import quote_hub

logger = logging.getLogger(__name__)

POINT_VALUE_TXF = 200
POINT_VALUE_MXF = 50
POINT_VALUE_TMF = 10


@dataclass
class StrategyState:
    is_running: bool = False
    position: int = 0
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    last_price: float = 0.0
    errors: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


class BaseStrategy(ABC):
    name: str = "base"
    point_value: int = POINT_VALUE_TMF

    def __init__(self) -> None:
        self.state = StrategyState()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop_loss_pts: int = 0
        self.take_profit_pts: int = 0
        self.daily_max_loss: int = 0
        self.max_trades_per_day: int = 0
        self.trade_start_hhmm: int = 0
        self.trade_end_hhmm: int = 0
        self._trades_today: int = 0
        self._risk_day: str = ""
        self._risk_halted: bool = False

    def _event(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.state.events.append(f"{ts} {text}")
        if len(self.state.events) > 30:
            self.state.events = self.state.events[-30:]

    @property
    def _base_params(self) -> dict[str, Any]:
        return {
            "stop_loss_pts": self.stop_loss_pts,
            "take_profit_pts": self.take_profit_pts,
            "daily_max_loss": self.daily_max_loss,
            "max_trades_per_day": self.max_trades_per_day,
            "trade_start_hhmm": self.trade_start_hhmm,
            "trade_end_hhmm": self.trade_end_hhmm,
        }

    @property
    def _base_param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "stop_loss_pts",      "label": "停損點數（0=停用）",          "type": "number", "min": 0, "max": 1000},
            {"key": "take_profit_pts",    "label": "停利點數（0=停用）",          "type": "number", "min": 0, "max": 1000},
            {"key": "daily_max_loss",     "label": "當日最大虧損元（0=停用）",     "type": "number", "min": 0, "max": 1000000},
            {"key": "max_trades_per_day", "label": "當日最大進場次數（0=停用）",   "type": "number", "min": 0, "max": 500},
            {"key": "trade_start_hhmm",   "label": "可開倉起始 HHMM（0=不限）",   "type": "number", "min": 0, "max": 2359},
            {"key": "trade_end_hhmm",     "label": "可開倉結束 HHMM（0=不限）",   "type": "number", "min": 0, "max": 2359},
        ]

    @property
    def params(self) -> dict[str, Any]:
        return self._base_params

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return self._base_param_schema

    def _apply_params(self, params: dict[str, Any]) -> None:
        pass

    async def start(self, loop: asyncio.AbstractEventLoop, params: dict[str, Any] | None = None) -> None:
        if params:
            self.stop_loss_pts      = int(params.get("stop_loss_pts",      self.stop_loss_pts))
            self.take_profit_pts    = int(params.get("take_profit_pts",    self.take_profit_pts))
            self.daily_max_loss     = int(params.get("daily_max_loss",     self.daily_max_loss))
            self.max_trades_per_day = int(params.get("max_trades_per_day", self.max_trades_per_day))
            self.trade_start_hhmm   = int(params.get("trade_start_hhmm",   self.trade_start_hhmm))
            self.trade_end_hhmm     = int(params.get("trade_end_hhmm",     self.trade_end_hhmm))
            self._apply_params(params)
        self._trades_today = 0
        self._risk_day = datetime.now().strftime("%Y-%m-%d")
        self._risk_halted = False
        self._loop = loop
        self.state.is_running = True
        self.state.events.clear()
        broker.set_order_callback(self._order_callback)
        await self._cancel_all_pending()
        await self._sync_position_from_broker()
        # 訂閱報價（worker 不重複訂閱）
        try:
            await broker.subscribe("TMF")
        except Exception as e:
            logger.warning("策略 [%s] 訂閱 TMF 失敗: %s", self.name, e)
        quote_hub.subscribe_strategy(self.name, self._on_quote_async)
        logger.info("策略 [%s] 已啟動", self.name)

    async def _cancel_all_pending(self) -> None:
        logger.info("策略 [%s] 啟動清理：查詢殘留委託…", self.name)
        try:
            trades = await asyncio.wait_for(broker.list_trades_with_status(), timeout=5)
            pending = [
                t for t in trades
                if not any(k in t.get("status", "") for k in ("Filled", "Cancelled", "Cancel"))
            ]
            logger.info(
                "策略 [%s] 啟動清理：共 %d 筆委託，其中 %d 筆未成交待取消",
                self.name, len(trades), len(pending),
            )
            cancelled = 0
            for i, t in enumerate(pending, 1):
                try:
                    await asyncio.wait_for(broker.cancel_order(t["id"]), timeout=5)
                    cancelled += 1
                    logger.info("策略 [%s] 取消殘留委託 %d/%d", self.name, i, len(pending))
                except Exception as e:
                    logger.warning("策略 [%s] 取消殘留委託失敗: %s", self.name, e)
            logger.info("策略 [%s] 啟動清理完成：已取消 %d 筆", self.name, cancelled)
        except Exception as e:
            logger.warning("策略 [%s] 啟動清單查詢失敗，略過: %s", self.name, e)

    async def stop(self) -> None:
        self.state.is_running = False
        quote_hub.unsubscribe_strategy(self.name)
        logger.info("策略 [%s] 已停止", self.name)

    async def _check_sl_tp(self, price: float) -> bool:
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
        if not triggered:
            return False

        prev_pos   = self.state.position
        prev_entry = self.state.entry_price
        action = "Sell" if prev_pos > 0 else "Buy"
        qty = abs(prev_pos)
        self.state.position = 0
        self.state.entry_price = 0.0
        self.state.unrealized_pnl = 0.0
        try:
            await self.place_order(action, qty)
        except Exception as e:
            self.state.position = prev_pos
            self.state.entry_price = prev_entry
            logger.error("策略 [%s] 停損停利平倉失敗，還原部位: %s", self.name, e)
            self.state.errors.append(f"停損停利平倉失敗: {e}")
            return True
        self.state.realized_pnl += pts * qty * self.point_value
        return True

    async def _go(self, direction: int, price: float) -> None:
        prev_pos   = self.state.position
        prev_entry = self.state.entry_price
        if direction > 0 and prev_pos > 0:
            return
        if direction < 0 and prev_pos < 0:
            return

        action = "Buy" if direction > 0 else "Sell"
        self.state.position = direction
        self.state.entry_price = price
        self.state.unrealized_pnl = 0.0
        try:
            if prev_pos != 0:
                close_qty = abs(prev_pos)
                await self.place_order(action, close_qty)
                pts = (price - prev_entry) * (1 if prev_pos > 0 else -1)
                self.state.realized_pnl += pts * close_qty * self.point_value
            await self.place_order(action, 1)
        except Exception as e:
            self.state.position = prev_pos
            self.state.entry_price = prev_entry
            logger.error("策略 [%s] 進場下單失敗，還原部位: %s", self.name, e)
            self.state.errors.append(f"進場下單失敗: {e}")
            return
        self._event(f"{'多' if direction > 0 else '空'}單進場 @ {price:.0f}")

    async def _sync_position_from_broker(self) -> None:
        try:
            positions = await asyncio.wait_for(broker.list_positions(), timeout=5)
        except Exception as e:
            logger.warning("策略 [%s] 啟動對帳部位失敗，略過: %s", self.name, e)
            return

        net = 0
        avg_price = 0.0
        for p in positions or []:
            code = p.get("code", "")
            if not code.startswith("TMF"):
                continue
            qty     = int(p.get("quantity", 0) or 0)
            dir_str = str(p.get("direction", ""))
            signed  = qty if "Buy" in dir_str else -qty
            net    += signed
            avg_price = float(p.get("price", 0) or 0)

        self.state.position = net
        self.state.entry_price = avg_price if net != 0 else 0.0
        self.state.unrealized_pnl = 0.0
        if net != 0:
            logger.info(
                "策略 [%s] 啟動對帳：券商既有 TMF 部位 %+d 口 @ %.0f",
                self.name, net, avg_price,
            )
            self._event(f"啟動對帳：既有部位 {net:+d}口 @ {avg_price:.0f}")
        else:
            logger.info("策略 [%s] 啟動對帳：券商無 TMF 部位", self.name)

        self._on_position_synced(net, self.state.entry_price)

    def _on_position_synced(self, net: int, avg_price: float) -> None:
        pass

    async def _on_quote_async(self, quote: dict) -> None:
        price = float(quote["close"])
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
    async def on_quote(self, quote: dict) -> None:
        ...

    def _order_callback(self, event: dict) -> None:
        """由 broker event_reader thread 透過 call_soon_threadsafe 呼叫，在 event loop 上執行。"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.on_order_event(event, ""), self._loop
            )

    async def on_order_event(self, event: dict, _unused: Any = None) -> None:
        logger.info("策略 [%s] 委託回報: state=%s trade_id=%s",
                    self.name, event.get("state", "?"), event.get("trade_id", "?"))

    def _risk_ok(self) -> tuple[bool, str]:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if today != self._risk_day:
            self._risk_day = today
            self._trades_today = 0
            self._risk_halted = False

        if self._risk_halted:
            return False, "已觸發當日虧損上限，今日停止開倉"

        if self.daily_max_loss > 0:
            day_pnl = self.state.realized_pnl + self.state.unrealized_pnl
            if day_pnl <= -self.daily_max_loss:
                self._risk_halted = True
                msg = f"當日虧損 {day_pnl:.0f} 元已達上限 -{self.daily_max_loss}，停止開倉（持倉停損停利照常）"
                logger.warning("策略 [%s] %s", self.name, msg)
                self._event(f"⛔ {msg}")
                return False, msg

        if self.max_trades_per_day > 0 and self._trades_today >= self.max_trades_per_day:
            return False, f"當日進場已達 {self.max_trades_per_day} 次上限"

        if self.trade_start_hhmm or self.trade_end_hhmm:
            hhmm = now.hour * 100 + now.minute
            start, end = self.trade_start_hhmm, self.trade_end_hhmm
            if start and end:
                in_window = (start <= hhmm <= end) if start <= end else (hhmm >= start or hhmm <= end)
            elif start:
                in_window = hhmm >= start
            else:
                in_window = hhmm <= end
            if not in_window:
                return False, f"非可開倉時段（{start:04d}~{end:04d}）"

        return True, ""

    async def _margin_ok(self) -> bool:
        if os.getenv("SIMULATION", "true").lower() == "true":
            return True
        try:
            m = await asyncio.wait_for(broker.margin(), timeout=5)
            equity_amount = float(m.get("equity_amount", 0) or 0)
            margin_call   = float(m.get("margin_call",   0) or 0)
            if equity_amount <= 0:
                logger.warning("策略 [%s] 可用保證金不足 (equity_amount=%.0f)", self.name, equity_amount)
                return False
            if margin_call > 0:
                logger.warning("策略 [%s] 已觸發追繳 (margin_call=%.0f)，停止開倉", self.name, margin_call)
                return False
            return True
        except Exception as e:
            logger.warning("策略 [%s] 無法取得保證金資訊，略過檢查: %s", self.name, e)
            return True

    async def place_order(
        self,
        action: str,
        quantity: int = 1,
        price: Optional[float] = None,
        price_type: str = "MKT",
        order_type: str = "IOC",
    ):
        # action is "Buy" or "Sell"
        is_reducing = (
            (action == "Buy"  and self.state.position < 0) or
            (action == "Sell" and self.state.position > 0)
        )
        if not is_reducing:
            ok, reason = self._risk_ok()
            if not ok:
                self.state.errors.append(reason)
                raise RuntimeError(reason)
            if not await self._margin_ok():
                msg = "保證金不足，略過開倉"
                self.state.errors.append(msg)
                raise RuntimeError(msg)

        try:
            trade = await broker.place_order(
                contract_code="TMF",
                action=action,
                quantity=quantity,
                price=price or 0,
                price_type=price_type,
                order_type=order_type,
                octype="Auto",
            )
            if not is_reducing:
                self._trades_today += 1
            logger.info(
                "策略 [%s] 下單: %s %d口 @ %s",
                self.name, action, quantity, price or "市價"
            )
            return trade
        except Exception as e:
            logger.error("策略 [%s] 下單失敗: %s", self.name, e)
            self.state.errors.append(f"下單失敗: {e}")
            raise
