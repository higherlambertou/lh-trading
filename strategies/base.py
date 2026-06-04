import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import shioaji as sj

from core.broker import broker
from core.quote_hub import quote_hub

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
    events: list[str] = field(default_factory=list)   # 成交/委託事件流（給前端顯示）


class BaseStrategy(ABC):
    name: str = "base"
    point_value: int = POINT_VALUE_TMF   # 微台每點 10 元

    def __init__(self) -> None:
        self.state = StrategyState()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop_loss_pts: int = 0    # 0 = 停用
        self.take_profit_pts: int = 0  # 0 = 停用

    def _event(self, text: str) -> None:
        """記錄一筆事件到事件流（給前端顯示），只保留最近 30 筆。"""
        ts = datetime.now().strftime("%H:%M:%S")
        self.state.events.append(f"{ts} {text}")
        if len(self.state.events) > 30:
            self.state.events = self.state.events[-30:]

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
        self.state.events.clear()
        contract = broker.tmf_contract()
        broker.set_order_callback(self._order_callback)
        # 先清掉上一輪殘留的未成交委託（此時還沒訂閱報價，避免與報價串流爭用 shioaji client）
        self._cancel_all_pending()
        # 與券商對帳既有部位，避免帶倉重啟時策略以為自己空手而疊單
        self._sync_position_from_broker()
        # 清理完成後才訂閱報價、開始派發給策略
        quote_hub.ensure_contract_subscribed(contract)
        quote_hub.subscribe_strategy(self.name, self._on_quote_async)
        logger.info("策略 [%s] 已啟動", self.name)

    def _cancel_all_pending(self) -> None:
        """啟動時取消所有未成交的 TMF 委託，清除上一輪殘留的掛單。"""
        logger.info("策略 [%s] 啟動清理：查詢殘留委託…", self.name)
        try:
            broker.call(lambda: broker.api.update_status(broker.api.futopt_account))
            trades = broker.call(lambda: broker.api.list_trades())
            pending = [
                t for t in trades
                if not any(
                    k in str(t.status.status) for k in ("Filled", "Cancelled", "Cancel")
                )
            ]
            logger.info(
                "策略 [%s] 啟動清理：共 %d 筆委託，其中 %d 筆未成交待取消",
                self.name, len(trades), len(pending),
            )
            cancelled = 0
            for i, t in enumerate(pending, 1):
                try:
                    broker.call(lambda tr=t: broker.api.cancel_order(tr))
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
        if not triggered:
            return False

        # 重入防護：先把部位歸零，再送平倉單。
        # 否則 await place_order 期間，下一筆報價的 dispatch 會再次進來、
        # 看到 position 仍非 0 而重複觸發停損 → 重複平倉甚至 OcType.Auto 反向開倉。
        prev_pos   = self.state.position
        prev_entry = self.state.entry_price
        action = sj.constant.Action.Sell if prev_pos > 0 else sj.constant.Action.Buy
        qty = abs(prev_pos)
        self.state.position = 0
        self.state.entry_price = 0.0
        self.state.unrealized_pnl = 0.0
        try:
            await self.place_order(action, qty)
        except Exception as e:
            # 平倉失敗 → 還原部位，讓下一筆報價可重試
            self.state.position = prev_pos
            self.state.entry_price = prev_entry
            logger.error("策略 [%s] 停損停利平倉失敗，還原部位: %s", self.name, e)
            self.state.errors.append(f"停損停利平倉失敗: {e}")
            return True
        self.state.realized_pnl += pts * qty * self.point_value
        return True

    async def _go(self, direction: int, price: float) -> None:
        """通用進場：依 direction(+1 多 / -1 空) 建立 1 口部位。

        若目前持有反向部位，先平掉再反手。所有 state 變更都在 await 之前完成，
        避免報價 dispatch 重入時看到舊部位而重複下單（OcType.Auto 反向疊單）。
        """
        prev_pos   = self.state.position
        prev_entry = self.state.entry_price
        if direction > 0 and prev_pos > 0:
            return
        if direction < 0 and prev_pos < 0:
            return

        action = sj.constant.Action.Buy if direction > 0 else sj.constant.Action.Sell
        # 先樂觀更新 state，擋住重入
        self.state.position = direction
        self.state.entry_price = price
        self.state.unrealized_pnl = 0.0
        try:
            # 有反向部位 → 先平倉並結算已實現損益
            if prev_pos != 0:
                close_qty = abs(prev_pos)
                await self.place_order(action, close_qty)
                pts = (price - prev_entry) * (1 if prev_pos > 0 else -1)
                self.state.realized_pnl += pts * close_qty * self.point_value
            # 再開 1 口新倉
            await self.place_order(action, 1)
        except Exception as e:
            # 任一腳失敗 → 還原 state（保守處理，下一筆報價可重試）
            self.state.position = prev_pos
            self.state.entry_price = prev_entry
            logger.error("策略 [%s] 進場下單失敗，還原部位: %s", self.name, e)
            self.state.errors.append(f"進場下單失敗: {e}")
            return
        self._event(f"{'多' if direction > 0 else '空'}單進場 @ {price:.0f}")

    def _sync_position_from_broker(self) -> None:
        """啟動時與券商對帳 TMF 既有部位，避免帶倉重啟時策略以為自己空手而疊單。"""
        try:
            positions = broker.call(
                lambda: broker.api.list_positions(broker.api.futopt_account)
            )
        except Exception as e:
            logger.warning("策略 [%s] 啟動對帳部位失敗，略過: %s", self.name, e)
            return

        net = 0
        avg_price = 0.0
        for p in positions or []:
            code = str(getattr(p, "code", ""))
            if not code.startswith("TMF"):
                continue
            qty = int(getattr(p, "quantity", 0) or 0)
            direction = getattr(p, "direction", None)
            dir_str = str(getattr(direction, "value", direction))
            signed = qty if "Buy" in dir_str else -qty
            net += signed
            avg_price = float(getattr(p, "price", 0) or 0)

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
            msg = "保證金不足，略過開倉"
            self.state.errors.append(msg)
            raise RuntimeError(msg)

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
