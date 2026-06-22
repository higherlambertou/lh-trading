import asyncio
import logging
import uuid
from dataclasses import dataclass, field

import shioaji as sj

from core.broker import broker
from core.quote_hub import quote_hub

logger = logging.getLogger(__name__)

_CONTRACT_FN = {
    "TMF": lambda: broker.tmf_contract(),
    "MXF": lambda: broker.mxf_contract(),
    "TXF": lambda: broker.txf_contract(),
}


def _txo_round_tick(p: float) -> float:
    """把選擇權權利金 round 到合法跳動點，避免限價單因 tick 不合被退。
    台指選擇權級距：<10→0.1, 10~50→0.5, 50~500→1, 500~1000→5, >=1000→10。"""
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
    contract: str       # 期貨="TMF"/"MXF"/"TXF"；選擇權=合約類別如 "TXO"
    direction: int      # 1=多/買, -1=空/賣
    quantity: int
    entry_price: float  # 0 = 市價單尚未填入，等第一次查到部位後補
    stop_loss_pts: int
    take_profit_pts: int
    order_id: str = ""  # 對應的委託單 trade.status.id；用來判斷單子是否還掛著
    seen: bool = False  # 是否曾查到對應部位（＝已成交）
    waited: int = 0     # 「委託單已不在且無部位」後等待的輪數（每輪約 1s），兜底用
    # ── 選擇權專用 ──────────────────────────────────────────────
    is_option: bool = False
    match_code: str = ""        # 比對部位用：期貨=前綴(TMF)；選擇權=完整 code
    multiplier: float = 0.0     # 每點金額（顯示用，可選）
    exit_buffer_pts: float = 0.0  # 選擇權平倉讓價點數，提高成交率（買賣價差大）
    contract_obj: object = None   # 選擇權合約物件（平倉下單 / 重新訂閱報價用）


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
        contract_obj: object = None,
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
            match_code=match_code or contract,  # 期貨預設用前綴比對
            multiplier=multiplier,
            exit_buffer_pts=exit_buffer_pts,
            contract_obj=contract_obj,
        )
        # 選擇權：立刻訂閱該合約報價，停損停利才有即時權利金可比
        if is_option and contract_obj is not None:
            try:
                quote_hub.ensure_contract_subscribed(contract_obj)
            except Exception as e:
                logger.warning("ManualWatch 訂閱選擇權報價失敗 [%s]: %s", watch_id, e)
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
                positions = await broker.acall_to(
                    lambda: broker.api.list_positions(broker.api.futopt_account)
                )
            except asyncio.TimeoutError:
                logger.warning("ManualMonitor 查詢部位逾時")
                continue
            except Exception as e:
                logger.warning("ManualMonitor 查詢部位失敗: %s", e)
                continue

            # 只有「還有未成交監控」時才額外查委託單狀態，省 API 額度。
            order_status: dict[str, str] = {}
            if any(not w.seen for w in self._watches.values()):
                try:
                    def _fetch_orders():
                        broker.api.update_status(broker.api.futopt_account)
                        return broker.api.list_trades()
                    trades = await broker.acall_to(_fetch_orders)
                    for t in trades:
                        tid = str(getattr(t.status, "id", "") or "")
                        if tid:
                            order_status[tid] = str(
                                getattr(t.status.status, "value", t.status.status)
                            )
                except asyncio.TimeoutError:
                    logger.warning("ManualMonitor 查詢委託狀態逾時")
                except Exception as e:
                    logger.warning("ManualMonitor 查詢委託狀態失敗: %s", e)

            for watch in list(self._watches.values()):
                await self._check(watch, positions, order_status)

    # 委託單「還活著（掛著等成交）」的狀態
    _LIVE_ORDER_STATES = {
        "PendingSubmit", "PreSubmitted", "Submitted", "PartFilled",
    }
    # 委託單「已死且沒成交」的狀態
    _DEAD_ORDER_STATES = {
        "Cancelled", "Canceled", "Failed", "Inactive", "Expired",
    }

    async def _check(
        self, watch: ManualWatch, positions: list, order_status: dict[str, str]
    ) -> None:
        # 選擇權：每輪確保報價仍訂閱著（重連後 _subscribed_contracts 會被清空）
        if watch.is_option and watch.contract_obj is not None:
            try:
                quote_hub.ensure_contract_subscribed(watch.contract_obj)
            except Exception:
                pass

        target_dir = "Buy" if watch.direction == 1 else "Sell"

        def _code_match(code: str) -> bool:
            return code == watch.match_code if watch.is_option else code.startswith(watch.match_code)

        pos = next(
            (
                p for p in positions
                if _code_match(p.code)
                and getattr(p.direction, "value", str(p.direction)) == target_dir
            ),
            None,
        )

        if pos is None:
            if watch.seen:
                # 曾經有部位、現在不見了 → 已平倉（被停損停利或手動平掉），移除監控
                logger.info("手動監控 [%s] 部位已平倉，移除監控", watch.id)
                self.remove(watch.id)
                return

            # 尚未成交：以「委託單還在不在」判斷，而不是盲目計時。
            status = order_status.get(watch.order_id) if watch.order_id else None
            if status in self._LIVE_ORDER_STATES:
                # 限價單還掛著等成交 → 無限期等，不刪、不累積兜底計時
                watch.waited = 0
                return
            if status in self._DEAD_ORDER_STATES:
                logger.info(
                    "手動監控 [%s] 委託單已取消/失效（%s）且未成交，移除監控",
                    watch.id, status,
                )
                self.remove(watch.id)
                return
            # 查不到單號狀態（沒帶單號、或剛成交就平掉沒抓到部位）→ 用兜底計時
            watch.waited += 1
            if watch.waited >= 600:  # 約 10 分鐘都對不到單也無部位才放棄，避免幽靈監控
                logger.info("手動監控 [%s] 逾 10 分鐘無單無部位，移除監控", watch.id)
                self.remove(watch.id)
            return

        # 查到部位 → 標記已成交
        watch.seen = True
        watch.waited = 0

        # 市價單補入場價
        if watch.entry_price == 0:
            watch.entry_price = float(pos.price)
            return

        # 現價來源：選擇權用報價推播快取（權利金）；期貨用部位回報的 last_price
        if watch.is_option:
            cached = quote_hub.get_last_price(watch.match_code)
            if cached is None:
                return  # 還沒收到該選擇權的報價，這輪先略過，等下一筆 tick
            current_price = float(cached)
        else:
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
        quantity = watch.quantity

        if watch.is_option:
            # 選擇權平倉：用「讓價限價單」而非市價單（遠價外流動性差、市價會被坑）。
            # 平多(賣出)→掛比現價低 buffer；平空(買回)→掛比現價高 buffer，提高成交率。
            last = quote_hub.get_last_price(watch.match_code) or watch.entry_price
            buf = watch.exit_buffer_pts or 0
            raw = last - buf if watch.direction == 1 else last + buf
            limit_price = _txo_round_tick(max(0.1, raw))
            contract_obj = watch.contract_obj

            def _place():
                order = sj.FuturesOrder(
                    action=close_action,
                    price=limit_price,
                    quantity=quantity,
                    price_type=sj.constant.FuturesPriceType.LMT,
                    order_type=sj.constant.OrderType.IOC,
                    octype=sj.constant.FuturesOCType.Auto,
                    account=broker.api.futopt_account,
                )
                return broker.api.place_order(contract_obj, order)
        else:
            contract_key = watch.contract

            def _place():
                order = sj.FuturesOrder(
                    action=close_action,
                    price=0,
                    quantity=quantity,
                    price_type=sj.constant.FuturesPriceType.MKT,
                    order_type=sj.constant.OrderType.IOC,
                    octype=sj.constant.FuturesOCType.Auto,
                    account=broker.api.futopt_account,
                )
                return broker.api.place_order(_CONTRACT_FN[contract_key](), order)

        try:
            await broker.acall(_place)
            self.remove(watch.id)
            logger.info("手動停損停利平倉成功: %s", watch.id)
        except Exception as e:
            logger.error("手動停損停利平倉失敗 [%s]: %s", watch.id, e)


manual_monitor = ManualOrderMonitor()
