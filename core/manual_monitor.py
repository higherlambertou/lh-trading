import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from core.broker import broker
from core.quote_hub import quote_hub
from core.trade_log import trade_log

logger = logging.getLogger(__name__)

# 平倉單的確認與重送：送出後不立刻移除監看，確認結果才決定移除或重送
MAX_CLOSE_ATTEMPTS = 8        # 平倉單最多送幾次（每次重送前都先確認前一張的結果）
CLOSE_RETRY_MIN_GAP = 2.0     # 前一張確定沒成交（取消/失敗）後，至少隔幾秒才重送
CLOSE_FILLED_GRACE = 5.0      # 前一張有成交但這個監看還有口數沒平：等持倉刷新再補平剩餘
CLOSE_LIVE_TIMEOUT = 15.0     # IOC 不該掛這麼久：狀態一直是「處理中」超過這麼久，視為沒成交
CLOSE_UNKNOWN_TIMEOUT = 10.0  # 送單逾時/查不到委託狀態（結果不明）：等這麼久再決定


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
    # 平倉追蹤：送出平倉單後不立刻移除監看，確認結果才決定移除或重送
    remaining: int = 0             # 這個監看還要平掉幾口（0 = 尚未設定，視為 quantity）
    close_order_id: str = ""       # 最近一張尚未結算的平倉單 id（空 = 沒有）
    close_qty: int = 0             # 那張平倉單的口數
    close_sent_at: float = 0.0
    close_attempts: int = 0        # 已送出幾次平倉單（重送時選擇權限價會逐次放寬）
    retry_after: float = 0.0       # 在此（monotonic）時間之前不可再送平倉單
    close_gave_up: bool = False    # 超過 MAX_CLOSE_ATTEMPTS 仍未平倉：不再自動重送，需人工處理


class ManualOrderMonitor:
    def __init__(self) -> None:
        self._watches: dict[str, ManualWatch] = {}
        self._task: asyncio.Task | None = None
        self._clock = time.monotonic          # 測試可替換

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
                "close_attempts": w.close_attempts,
                "close_gave_up": w.close_gave_up,
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
            order_fills: dict[str, int] = {}
            if self._needs_order_status():
                try:
                    trades = await asyncio.wait_for(broker.list_trades_with_status(), timeout=5)
                    order_status, order_fills = self._index_trades(trades)
                except asyncio.TimeoutError:
                    logger.warning("ManualMonitor 查詢委託狀態逾時")
                except Exception as e:
                    logger.warning("ManualMonitor 查詢委託狀態失敗: %s", e)

            for watch in list(self._watches.values()):
                await self._check(watch, positions, order_status, order_fills)

    _LIVE_ORDER_STATES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}
    _DEAD_ORDER_STATES = {"Cancelled", "Canceled", "Failed", "Inactive", "Expired"}

    def _needs_order_status(self) -> bool:
        """有監看還在等委託單的結果（開倉單尚未成交，或平倉單送出後要確認）才需要查委託狀態。"""
        return any((not w.seen) or w.close_attempts > 0 for w in self._watches.values())

    @staticmethod
    def _index_trades(trades: list[dict]) -> tuple[dict[str, str], dict[str, int]]:
        """委託清單 → ({id: 狀態}, {id: 已成交口數})。"""
        status: dict[str, str] = {}
        fills: dict[str, int] = {}
        for t in trades:
            tid = str(t.get("id", "") or "")
            if tid:
                status[tid] = str(t.get("status", ""))
                fills[tid] = int(t.get("deal_quantity", 0) or 0)
        return status, fills

    async def _check(
        self, watch: ManualWatch, positions: list[dict], order_status: dict[str, str],
        order_fills: dict[str, int] | None = None,
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

        # 先結算上一張平倉單：全數成交 → 這個監看的任務完成（持倉裡剩下的口數屬於別的單，不能動）
        if watch.close_order_id and self._settle(watch, order_status, order_fills or {}):
            logger.info("手動監控 [%s] 平倉單已全數成交，移除監控", watch.id)
            self.remove(watch.id)
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
        kind = ""
        if watch.take_profit_pts > 0 and pts >= watch.take_profit_pts:
            triggered = True
            reason = f"停利 +{pts:.0f}點"
            kind = "tp"
        elif watch.stop_loss_pts > 0 and pts <= -watch.stop_loss_pts:
            triggered = True
            reason = f"停損 {pts:.0f}點"
            kind = "sl"

        if triggered:
            await self._try_close(watch, pos, kind, reason, current_price)

    def _settle(self, watch: ManualWatch, order_status: dict[str, str], order_fills: dict[str, int]) -> bool:
        """結算最近一張平倉單：有結果就記帳（扣掉已成交口數）並決定何時能重送。
        回傳 True = 這個監看要平的口數已全數成交。"""
        oid, now = watch.close_order_id, self._clock()
        status = order_status.get(oid)
        if status == "Filled" or status in self._DEAD_ORDER_STATES:
            # Filled = 全部成交；取消/失敗之前可能已部分成交（IOC 常見）
            filled = order_fills.get(oid, watch.close_qty if status == "Filled" else 0)
            watch.remaining = max(0, (watch.remaining or watch.quantity) - filled)
            watch.close_order_id, watch.close_qty = "", 0
            if watch.remaining == 0:
                return True
            # 有成交（持倉可能還沒刷新）等久一點再補平剩餘；完全沒成交就很快重送
            watch.retry_after = now + (CLOSE_FILLED_GRACE if filled else CLOSE_RETRY_MIN_GAP)
        elif status in self._LIVE_ORDER_STATES:
            if now - watch.close_sent_at >= CLOSE_LIVE_TIMEOUT:      # IOC 不該處理這麼久
                watch.close_order_id, watch.close_qty = "", 0
                watch.retry_after = now
        elif now - watch.close_sent_at >= CLOSE_UNKNOWN_TIMEOUT:     # 查不到這張單的狀態
            watch.close_order_id, watch.close_qty = "", 0
            watch.retry_after = now
        return False

    async def _try_close(self, watch: ManualWatch, pos: dict, kind: str, reason: str,
                         current_price: float) -> None:
        """停損/停利觸發：送平倉單。前一張還在等結果、冷卻中、或已放棄時不送，避免重複平倉變成反向開倉。"""
        if watch.close_order_id or watch.close_gave_up:
            return
        if self._clock() < watch.retry_after:
            return
        if watch.close_attempts >= MAX_CLOSE_ATTEMPTS:
            watch.close_gave_up = True
            logger.error("手動監控 [%s] 平倉單已送 %d 次仍未平倉，不再自動重送，請立刻手動處理！",
                         watch.id, watch.close_attempts)
            return
        remaining = watch.remaining or watch.quantity
        pos_qty = int(pos.get("quantity", 0) or 0)
        qty = min(remaining, pos_qty) if pos_qty > 0 else remaining      # 不超過這個監看剩下的口數與當下部位
        if watch.close_attempts:
            logger.warning("手動下單 [%s] %s @ %.0f，前一張平倉單未成交，第 %d 次重送（%d 口）",
                           watch.id, reason, current_price, watch.close_attempts + 1, qty)
        else:
            logger.info("手動下單 [%s] %s @ %.0f，執行平倉", watch.id, reason, current_price)
        # 成交紀錄用：設定的停損/停利價位與觸發當下的價格（滑價 = 實際成交價 vs 這些）
        ref = watch.entry_price + watch.direction * (
            watch.take_profit_pts if kind == "tp" else -watch.stop_loss_pts)
        await self._close(watch, kind, ref, current_price, qty)

    async def _close(self, watch: ManualWatch, kind: str = "exit", ref_price: float | None = None,
                     signal_price: float | None = None, qty: int | None = None) -> None:
        """送出一張平倉單。送出後**不移除監看**：由 _check 確認結果（全數成交才移除、沒成交再重送）。"""
        close_action = "Sell" if watch.direction == 1 else "Buy"
        qty = int(qty or watch.quantity)
        attempt = watch.close_attempts              # 0 = 第一次
        now = self._clock()
        watch.close_attempts += 1
        # 先設好「結果不明時」的保護：就算送單丟例外/逾時（單可能已送達券商），也要等一下才重送，避免重複平倉
        watch.close_order_id, watch.close_qty = "", 0
        watch.retry_after = now + CLOSE_UNKNOWN_TIMEOUT

        try:
            # kind / ref_price / signal_price 只給成交紀錄用，不影響下單
            with trade_log.context(strategy="manual_monitor", reason=kind,
                                   signal_price=signal_price, ref_price=ref_price):
                if watch.is_option and watch.delivery_month:
                    last = quote_hub.get_last_price(watch.match_code) or watch.entry_price
                    # 選擇權平倉是限價 IOC：沒成交時重送會逐次放寬緩衝（每次再讓 ~1% 權利金），提高成交機會
                    buf = (watch.exit_buffer_pts or 0) + attempt * max(1.0, round(last * 0.01, 1))
                    raw = last - buf if watch.direction == 1 else last + buf
                    limit_price = _txo_round_tick(max(0.1, raw))
                    res = await broker.place_option_order(
                        delivery_month=watch.delivery_month,
                        strike=watch.strike_price,
                        right=watch.option_right,
                        category=watch.option_category,
                        action=close_action,
                        quantity=qty,
                        price=limit_price,
                        order_type="IOC",
                    )
                else:
                    res = await broker.place_order(
                        contract_code=watch.contract,
                        action=close_action,
                        quantity=qty,
                        price=0,
                        price_type="MKT",
                        order_type="IOC",
                        octype="Auto",
                    )
            trade_id = str(res.get("trade_id", "") or "") if isinstance(res, dict) else ""
            watch.close_order_id, watch.close_qty, watch.close_sent_at = trade_id, qty, now
            logger.info("手動停損停利平倉單已送出 [%s]（第 %d 次，%d 口）trade_id=%s，等待成交確認",
                        watch.id, watch.close_attempts, qty, trade_id or "?")
        except Exception as e:
            logger.error("手動停損停利平倉失敗 [%s]（第 %d 次，結果不明，%.0f 秒後確認部位再決定是否重送）: %s",
                         watch.id, watch.close_attempts, CLOSE_UNKNOWN_TIMEOUT, e)


manual_monitor = ManualOrderMonitor()
