import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from core.broker import broker
from core.quote_hub import quote_hub

logger = logging.getLogger(__name__)


def _txo_round_tick(p: float) -> float:
    """把選擇權權利金 round 到合法跳動點。"""
    if p < 10:
        return round(p * 10) / 10
    if p < 50:
        return round(p * 2) / 2
    if p < 500:
        return float(round(p))
    if p < 1000:
        return float(round(p / 5) * 5)
    return float(round(p / 10) * 10)


@dataclass
class ManualWatch:
    id: str
    contract: str
    direction: int
    quantity: int
    entry_price: float
    stop_loss_pts: int
    take_profit_pts: int
    order_id: str = ""
    seen: bool = False
    waited: int = 0
    # 選擇權專用
    is_option: bool = False
    match_code: str = ""
    multiplier: float = 0.0
    exit_buffer_pts: float = 0.0
    # 選擇權合約識別（取代原 contract_obj）
    delivery_month: str = ""
    strike_price: int = 0
    option_right: str = ""
    option_category: str = "TXO"


class ManualOrderMonitor:
    def __init__(self) -> None:
        self._watches: dict[str, ManualWatch] = {}
        self._task: asyncio.Task | None = None

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        self._task = loop.create_task(self._poll_loop())
        logger.info("ManualOrderMonitor 已啟動")

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ManualOrderMonitor 已關閉")

    def add(
        self,
        contract: str,
        direction: int,
        quantity: int,
        entry_price: float,
        stop_loss_pts: int,
        take_profit_pts: int,
        order_id: str = "",
        is_option: bool = False,
        match_code: str = "",
        multiplier: float = 0.0,
        exit_buffer_pts: float = 0.0,
        delivery_month: str = "",
        strike_price: int = 0,
        option_right: str = "",
        option_category: str = "TXO",
    ) -> str:
        watch_id = str(uuid.uuid4())[:8]
        self._watches[watch_id] = ManualWatch(
            id=watch_id,
            contract=contract,
            direction=direction,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss_pts=stop_loss_pts,
            take_profit_pts=take_profit_pts,
            order_id=order_id,
            is_option=is_option,
            match_code=match_code or contract,
            multiplier=multiplier,
            exit_buffer_pts=exit_buffer_pts,
            delivery_month=delivery_month,
            strike_price=strike_price,
            option_right=option_right,
            option_category=option_category,
        )
        # 選擇權：立刻訂閱報價（fire-and-forget，不阻塞）
        if is_option and delivery_month and strike_price and option_right:
            broker.subscribe_option_sync(delivery_month, strike_price, option_right, option_category)
        logger.info(
            "ManualWatch 登記: id=%s %s %s SL=%d TP=%d%s",
            watch_id, match_code or contract, "多/買" if direction == 1 else "空/賣",
            stop_loss_pts, take_profit_pts,
            "（選擇權）" if is_option else "",
        )
        return watch_id

    def remove(self, watch_id: str) -> None:
        self._watches.pop(watch_id, None)

    def update(
        self,
        watch_id: str,
        stop_loss_pts: int | None = None,
        take_profit_pts: int | None = None,
    ) -> bool:
        watch = self._watches.get(watch_id)
        if not watch:
            return False
        if stop_loss_pts is not None:
            watch.stop_loss_pts = stop_loss_pts
        if take_profit_pts is not None:
            watch.take_profit_pts = take_profit_pts
        logger.info(
            "ManualWatch 更新: id=%s SL=%d TP=%d",
            watch_id, watch.stop_loss_pts, watch.take_profit_pts,
        )
        return True

    def list_watches(self) -> list[dict]:
        return [
            {
                "id": w.id,
                "contract": w.contract,
                "direction": "Buy" if w.direction == 1 else "Sell",
                "quantity": w.quantity,
                "entry_price": w.entry_price,
                "stop_loss_pts": w.stop_loss_pts,
                "take_profit_pts": w.take_profit_pts,
                "is_option": w.is_option,
                "match_code": w.match_code,
            }
            for w in self._watches.values()
        ]

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if not self._watches:
                continue
            try:
                positions = await asyncio.wait_for(broker.list_positions(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("ManualMonitor 查詢部位逾時")
                continue
            except Exception as e:
                logger.warning("ManualMonitor 查詢部位失敗: %s", e)
                continue

            order_status: dict[str, str] = {}
            if any(not w.seen for w in self._watches.values()):
                try:
                    trades = await asyncio.wait_for(broker.list_trades_with_status(), timeout=5)
                    for t in trades:
                        tid = str(t.get("id", "") or "")
                        if tid:
                            order_status[tid] = str(t.get("status", ""))
                except asyncio.TimeoutError:
                    logger.warning("ManualMonitor 查詢委託狀態逾時")
                except Exception as e:
                    logger.warning("ManualMonitor 查詢委託狀態失敗: %s", e)

            for watch in list(self._watches.values()):
                await self._check(watch, positions, order_status)

    _LIVE_ORDER_STATES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}
    _DEAD_ORDER_STATES = {"Cancelled", "Canceled", "Failed", "Inactive", "Expired"}

    async def _check(
        self, watch: ManualWatch, positions: list[dict], order_status: dict[str, str]
    ) -> None:
        # 選擇權：確保仍在訂閱（重連後自動補回）
        if watch.is_option and watch.delivery_month and watch.strike_price and watch.option_right:
            broker.subscribe_option_sync(
                watch.delivery_month, watch.strike_price,
                watch.option_right, watch.option_category,
            )

        target_dir = "Buy" if watch.direction == 1 else "Sell"

        def _code_match(code: str) -> bool:
            return code == watch.match_code if watch.is_option else code.startswith(watch.match_code)

        pos = next(
            (
                p for p in positions
                if _code_match(p.get("code", ""))
                and p.get("direction", "") == target_dir
            ),
            None,
        )

        if pos is None:
            if watch.seen:
                logger.info("手動監控 [%s] 部位已平倉，移除監控", watch.id)
                self.remove(watch.id)
                return

            status = order_status.get(watch.order_id) if watch.order_id else None
            if status in self._LIVE_ORDER_STATES:
                watch.waited = 0
                return
            if status in self._DEAD_ORDER_STATES:
                logger.info(
                    "手動監控 [%s] 委託單已取消/失效（%s）且未成交，移除監控",
                    watch.id, status,
                )
                self.remove(watch.id)
                return
            watch.waited += 1
            if watch.waited >= 600:
                logger.info("手動監控 [%s] 逾 10 分鐘無單無部位，移除監控", watch.id)
                self.remove(watch.id)
            return

        watch.seen = True
        watch.waited = 0

        if watch.entry_price == 0:
            watch.entry_price = float(pos.get("price", 0))
            return

        if watch.is_option:
            cached = quote_hub.get_last_price(watch.match_code)
            if cached is None:
                return
            current_price = float(cached)
        else:
            current_price = float(pos.get("last_price") or pos.get("price", 0))

        pts = (current_price - watch.entry_price) * watch.direction

        triggered = False
        reason = ""
        if watch.take_profit_pts > 0 and pts >= watch.take_profit_pts:
            triggered = True
            reason = f"停利 +{pts:.0f}點"
        elif watch.stop_loss_pts > 0 and pts <= -watch.stop_loss_pts:
            triggered = True
            reason = f"停損 {pts:.0f}點"

        if triggered:
            logger.info("手動下單 [%s] %s @ %.0f，執行平倉", watch.id, reason, current_price)
            await self._close(watch)

    async def _close(self, watch: ManualWatch) -> None:
        close_action = "Sell" if watch.direction == 1 else "Buy"

        try:
            if watch.is_option and watch.delivery_month:
                last = quote_hub.get_last_price(watch.match_code) or watch.entry_price
                buf = watch.exit_buffer_pts or 0
                raw = last - buf if watch.direction == 1 else last + buf
                limit_price = _txo_round_tick(max(0.1, raw))
                await broker.place_option_order(
                    delivery_month=watch.delivery_month,
                    strike=watch.strike_price,
                    right=watch.option_right,
                    category=watch.option_category,
                    action=close_action,
                    quantity=watch.quantity,
                    price=limit_price,
                    order_type="IOC",
                )
            else:
                await broker.place_order(
                    contract_code=watch.contract,
                    action=close_action,
                    quantity=watch.quantity,
                    price=0,
                    price_type="MKT",
                    order_type="IOC",
                    octype="Auto",
                )
            self.remove(watch.id)
            logger.info("手動停損停利平倉成功: %s", watch.id)
        except Exception as e:
            logger.error("手動停損停利平倉失敗 [%s]: %s", watch.id, e)


manual_monitor = ManualOrderMonitor()
