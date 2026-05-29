import asyncio
import logging
import uuid
from dataclasses import dataclass, field

import shioaji as sj

from core.broker import broker

logger = logging.getLogger(__name__)

_CONTRACT_FN = {
    "TMF": lambda: broker.tmf_contract(),
    "MXF": lambda: broker.mxf_contract(),
    "TXF": lambda: broker.txf_contract(),
}


@dataclass
class ManualWatch:
    id: str
    contract: str       # "TMF", "MXF", "TXF"
    direction: int      # 1=多, -1=空
    quantity: int
    entry_price: float  # 0 = 市價單尚未填入，等第一次查到部位後補
    stop_loss_pts: int
    take_profit_pts: int


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
        )
        logger.info(
            "ManualWatch 登記: id=%s %s %s SL=%d TP=%d",
            watch_id, contract, "多" if direction == 1 else "空",
            stop_loss_pts, take_profit_pts,
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
            }
            for w in self._watches.values()
        ]

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if not self._watches:
                continue
            try:
                loop = asyncio.get_running_loop()
                positions = await loop.run_in_executor(
                    None, broker.api.list_positions, broker.api.futopt_account
                )
            except Exception as e:
                logger.warning("ManualMonitor 查詢部位失敗: %s", e)
                continue

            for watch in list(self._watches.values()):
                await self._check(watch, positions)

    async def _check(self, watch: ManualWatch, positions: list) -> None:
        buy_val = "Buy"
        sell_val = "Sell"
        target_dir = buy_val if watch.direction == 1 else sell_val

        pos = next(
            (
                p for p in positions
                if p.code.startswith(watch.contract)
                and getattr(p.direction, "value", str(p.direction)) == target_dir
            ),
            None,
        )

        if pos is None:
            # 部位已不存在，移除監控
            self.remove(watch.id)
            return

        # 市價單補入場價
        if watch.entry_price == 0:
            watch.entry_price = float(pos.price)
            return

        current_price = float(getattr(pos, "last_price", pos.price))
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
        close_action = (
            sj.constant.Action.Sell if watch.direction == 1
            else sj.constant.Action.Buy
        )
        order = sj.FuturesOrder(
            action=close_action,
            price=0,
            quantity=watch.quantity,
            price_type=sj.constant.FuturesPriceType.MKT,
            order_type=sj.constant.OrderType.IOC,
            octype=sj.constant.FuturesOCType.Auto,
            account=broker.api.futopt_account,
        )
        loop = asyncio.get_running_loop()
        try:
            contract_fn = _CONTRACT_FN[watch.contract]
            await loop.run_in_executor(None, broker.api.place_order, contract_fn(), order)
            self.remove(watch.id)
            logger.info("手動停損停利平倉成功: %s", watch.id)
        except Exception as e:
            logger.error("手動停損停利平倉失敗 [%s]: %s", watch.id, e)


manual_monitor = ManualOrderMonitor()
