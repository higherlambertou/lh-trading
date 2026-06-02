import logging
import random
from collections import deque
from typing import Any

import shioaji as sj

from core.broker import broker
from strategies.base import BaseStrategy, POINT_VALUE_TMF

logger = logging.getLogger(__name__)


class ScalpStrategy(BaseStrategy):
    """
    限價掃點策略

    訊號：外/內盤動量（tick_type 比例）或隨機
    開倉：ROD 限價單，可設偏移點控制積極/被動程度
    停利：成交後自動掛 ROD 限價停利單
    停損：持倉中價格不利達 sl_pts → 取消停利單 + 市價出場
    逾時：掛單超過 cancel_after_ticks 個 tick 未成交則取消
    冷卻：每次出場後等 cooldown_ticks 個 tick 再重新進場
    """

    name = "scalp"
    point_value = POINT_VALUE_TMF

    # ── 初始化 ─────────────────────────────────────────────────────

    def __init__(self) -> None:
        super().__init__()
        # ── 可調參數 ──────────────────────────────────────────────
        self.tp_pts: int = 20              # 停利點數
        self.sl_pts: int = 60              # 停損點數
        self.entry_offset: int = 0         # 掛單偏移：0=市價 正=被動 負=追價
        self.cancel_after_ticks: int = 15  # 幾個 tick 後取消未成交掛單
        self.momentum_window: int = 20     # 動量視窗 tick 數
        self.momentum_threshold: float = 0.65   # 觸發所需外/內盤比例
        self.signal_mode: str = "momentum" # "momentum" | "random"
        self.cooldown_ticks: int = 30      # 每次出場後冷卻 tick 數
        self.max_qty: int = 1              # 單次最多幾口

        # ── 內部狀態 ──────────────────────────────────────────────
        self._phase: str = "idle"     # idle / pending / holding / cooldown
        self._direction: int = 0      # 1=多  -1=空
        self._entry_trade = None
        self._tp_trade = None
        self._entry_qty: int = 1      # 本次實際入場口數
        self._entry_tick_count: int = 0
        self._cooldown_count: int = 0
        self._last_entry_price: float = 0.0
        self._tick_buf: deque[int] = deque(maxlen=100)

    # ── 參數介面 ────────────────────────────────────────────────

    @property
    def params(self) -> dict[str, Any]:
        return {
            "tp_pts": self.tp_pts,
            "sl_pts": self.sl_pts,
            "entry_offset": self.entry_offset,
            "cancel_after_ticks": self.cancel_after_ticks,
            "momentum_window": self.momentum_window,
            "momentum_threshold": round(self.momentum_threshold, 2),
            "signal_mode_int": 0 if self.signal_mode == "momentum" else 1,
            "cooldown_ticks": self.cooldown_ticks,
            "max_qty": self.max_qty,
        }

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "tp_pts",             "label": "停利點數",              "type": "number", "min": 5,    "max": 200},
            {"key": "sl_pts",             "label": "停損點數",              "type": "number", "min": 5,    "max": 500},
            {"key": "entry_offset",       "label": "掛單偏移（-追/+被動）",  "type": "number", "min": -10,  "max": 10},
            {"key": "cancel_after_ticks", "label": "掛單逾時 Ticks",        "type": "number", "min": 3,    "max": 100},
            {"key": "momentum_window",    "label": "動量視窗 Ticks",        "type": "number", "min": 5,    "max": 100},
            {"key": "momentum_threshold", "label": "動量門檻 0.5~1.0",      "type": "number", "min": 0.5,  "max": 1.0},
            {"key": "signal_mode_int",    "label": "訊號模式 0=動量/1=隨機", "type": "number", "min": 0,    "max": 1},
            {"key": "cooldown_ticks",     "label": "冷卻 Ticks",            "type": "number", "min": 0,    "max": 300},
            {"key": "max_qty",            "label": "最大口數",               "type": "number", "min": 1,    "max": 10},
        ]

    def _apply_params(self, params: dict[str, Any]) -> None:
        self.tp_pts             = int(params.get("tp_pts",             self.tp_pts))
        self.sl_pts             = int(params.get("sl_pts",             self.sl_pts))
        self.entry_offset       = int(params.get("entry_offset",       self.entry_offset))
        self.cancel_after_ticks = int(params.get("cancel_after_ticks", self.cancel_after_ticks))
        self.momentum_window    = int(params.get("momentum_window",    self.momentum_window))
        self.momentum_threshold = float(params.get("momentum_threshold", self.momentum_threshold))
        self.signal_mode        = "random" if int(params.get("signal_mode_int", 0)) else "momentum"
        self.cooldown_ticks     = int(params.get("cooldown_ticks",     self.cooldown_ticks))
        self.max_qty            = max(1, int(params.get("max_qty",     self.max_qty)))
        self._tick_buf = deque(maxlen=self.momentum_window)
        logger.info(
            "[scalp] 套用參數: TP=%d SL=%d offset=%d mode=%s window=%d threshold=%.2f cooldown=%d max_qty=%d",
            self.tp_pts, self.sl_pts, self.entry_offset, self.signal_mode,
            self.momentum_window, self.momentum_threshold, self.cooldown_ticks, self.max_qty,
        )

    # ── 訊號 ───────────────────────────────────────────────────────

    def _get_signal(self, quote: sj.QuoteFOPv1) -> int:
        """
        回傳方向：1=做多  -1=做空  0=無訊號

        動量模式：
          - 外盤（tick_type=1）= 主動買，佔比超過 threshold → 做多
          - 內盤（tick_type=2）= 主動賣，佔比超過 threshold → 做空

        entry_offset 控制積極/被動程度：
          0 = 限價掛在現價（容易成交）
         +N = 比現價更遠 N 點（被動等回調）
         -N = 比現價超前 N 點（主動追價）
        """
        if self.signal_mode == "random":
            return random.choice([1, -1])

        tt = int(quote.tick_type)
        if tt in (1, 2):
            self._tick_buf.append(tt)

        if len(self._tick_buf) < self.momentum_window:
            return 0

        buys  = sum(1 for t in self._tick_buf if t == 1)
        sells = sum(1 for t in self._tick_buf if t == 2)
        total = buys + sells
        if total == 0:
            return 0

        if buys / total >= self.momentum_threshold:
            return 1
        if sells / total >= self.momentum_threshold:
            return -1
        return 0

    # ── 主要 tick 邏輯 ──────────────────────────────────────────

    async def on_quote(self, quote: sj.QuoteFOPv1) -> None:
        price = float(quote.close)

        if self._phase == "idle":
            # 守衛：有未追蹤持倉時禁止新入場
            if self.state.position != 0:
                logger.warning("[scalp] idle 但 position=%d，暫停進場", self.state.position)
                return
            sig = self._get_signal(quote)
            if sig != 0:
                await self._do_enter(price, sig)

        elif self._phase == "pending":
            self._entry_tick_count += 1
            if self._entry_tick_count >= self.cancel_after_ticks:
                await self._cancel_entry()

        elif self._phase == "holding":
            pts = (price - self._last_entry_price) * self._direction
            if self.sl_pts > 0 and pts <= -self.sl_pts:
                logger.info("[scalp] 停損觸發 %.0f點 @ %.0f", pts, price)
                await self._do_sl()

        elif self._phase == "cooldown":
            self._cooldown_count += 1
            if self._cooldown_count >= self.cooldown_ticks:
                self._phase = "idle"
                self._tick_buf.clear()
                logger.info("[scalp] 冷卻結束，回到待機")

    # ── 下單輔助 ────────────────────────────────────────────────

    async def _lmt(self, action: sj.constant.Action, price: float, qty: int = 1):
        """掛 ROD 限價單"""
        order = sj.FuturesOrder(
            action=action,
            price=round(price),
            quantity=qty,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.Auto,
            account=broker.api.futopt_account,
        )
        return await broker.acall(
            lambda: broker.api.place_order(broker.tmf_contract(), order)
        )

    async def _cancel_safe(self, trade) -> None:
        if trade is None:
            return
        try:
            await broker.acall(lambda: broker.api.update_status(broker.api.futopt_account))
            await broker.acall(lambda: broker.api.cancel_order(trade))
        except Exception as e:
            logger.warning("[scalp] 取消委託失敗: %s", e)

    # ── 狀態切換動作 ────────────────────────────────────────────

    async def _do_enter(self, price: float, direction: int) -> None:
        """
        掛入場限價單

        entry_offset > 0：比現價更有利的方向（被動等候）
        entry_offset < 0：追過現價（積極追漲/殺跌）
        entry_offset = 0：直接掛在現價
        """
        # 多單：掛在 price - offset（正 offset = 低於現價，更被動）
        # 空單：掛在 price + offset（正 offset = 高於現價，更被動）
        entry_price = price - self.entry_offset * direction
        action = sj.constant.Action.Buy if direction == 1 else sj.constant.Action.Sell

        logger.info(
            "[scalp] %s 掛限價 @ %.0f  (現價=%.0f  offset=%+d  mode=%s  qty=%d)",
            "做多" if direction == 1 else "做空",
            entry_price, price, self.entry_offset, self.signal_mode, self.max_qty,
        )
        try:
            trade = await self._lmt(action, entry_price, qty=self.max_qty)
        except Exception as e:
            logger.error("[scalp] 掛單失敗: %s", e)
            self.state.errors.append(f"掛單失敗: {e}")
            return

        self._entry_trade       = trade
        self._entry_qty         = self.max_qty
        self._direction         = direction
        self._last_entry_price  = 0.0   # 清除舊值，成交後才更新
        self._entry_tick_count  = 0
        self._phase             = "pending"

    async def _cancel_entry(self) -> None:
        """
        逾時取消：送出取消請求，進入冷卻。
        _entry_trade 保留不清除，由 on_order_event 收到確認後才清除。
        這樣可以正確處理「取消請求送出後，成交回報才到」的 race condition。
        """
        logger.info("[scalp] 入場單逾時，送出取消請求 → 冷卻")
        if self._entry_trade is not None:
            try:
                await broker.acall(lambda: broker.api.update_status(broker.api.futopt_account))
            except Exception as e:
                logger.warning("[scalp] update_status 失敗: %s", e)
            try:
                t = self._entry_trade
                await broker.acall(lambda: broker.api.cancel_order(t))
            except Exception as e:
                logger.warning("[scalp] cancel_order 失敗（可能已成交，等 on_order_event）: %s", e)
        # _entry_trade 不在這裡清，由 on_order_event 確認 fill/cancel 後清
        self._phase          = "cooldown"
        self._cooldown_count = 0
        self._tick_buf.clear()

    async def _do_tp(self) -> None:
        """掛停利限價單"""
        if self._last_entry_price == 0:
            logger.error("[scalp] _last_entry_price=0，無法計算停利價，略過")
            self.state.errors.append("停利單未掛：entry_price 未知")
            return
        tp_price = self._last_entry_price + self.tp_pts * self._direction
        action = sj.constant.Action.Sell if self._direction == 1 else sj.constant.Action.Buy
        logger.info("[scalp] 掛停利單 @ %.0f  qty=%d", tp_price, self._entry_qty)
        try:
            self._tp_trade = await self._lmt(action, tp_price, qty=self._entry_qty)
        except Exception as e:
            logger.error("[scalp] 掛停利單失敗: %s", e)
            self.state.errors.append(f"掛停利單失敗: {e}")

    async def _do_sl(self) -> None:
        """停損：取消停利單 + 市價平倉"""
        await self._cancel_safe(self._tp_trade)
        self._tp_trade = None

        close_action = (
            sj.constant.Action.Sell if self._direction == 1
            else sj.constant.Action.Buy
        )
        try:
            await self.place_order(close_action, self._entry_qty)   # base class 市價單
        except Exception as e:
            logger.error("[scalp] 停損平倉失敗: %s", e)
            self.state.errors.append(f"停損平倉失敗: {e}")
            return

        self.state.realized_pnl += -self.sl_pts * self.point_value * self._entry_qty
        self.state.position = 0
        self.state.entry_price = 0.0
        self._phase = "cooldown"
        self._cooldown_count = 0

    # ── 成交回報 ────────────────────────────────────────────────

    async def on_order_event(self, stat: Any, msg: Any) -> None:
        try:
            oid    = stat.status.id
            status = str(stat.status.status)
        except Exception:
            return

        filled    = "Filled"    in status
        cancelled = "Cancelled" in status or "Cancel" in status

        # ── 入場單回報 ──────────────────────────────────────────
        # 只要 _entry_trade 還在就比對，不管目前 phase 是什麼。
        # _cancel_entry 不再提早清除 _entry_trade，所以這裡一定能收到。
        if self._entry_trade is not None and oid == self._entry_trade.status.id:
            if filled:
                fill_price = 0.0
                try:
                    deals = stat.status.deals or []
                    total_qty = sum(d.quantity for d in deals)
                    if total_qty > 0:
                        fill_price = (
                            sum(float(d.price) * d.quantity for d in deals)
                            / total_qty
                        )
                    else:
                        fill_price = float(self._entry_trade.order.price)
                except Exception:
                    fill_price = float(getattr(
                        getattr(self._entry_trade, "order", None), "price", 0
                    ) or 0)

                if fill_price == 0:
                    fill_price = self.state.last_price

                old_phase = self._phase
                self._last_entry_price = fill_price
                self.state.entry_price = fill_price
                self.state.position    = self._direction
                self._entry_trade      = None
                self._phase            = "holding"
                logger.info(
                    "[scalp] 入場成交 @ %.0f  方向=%s  (was phase=%s)",
                    fill_price, "多" if self._direction == 1 else "空", old_phase,
                )
                await self._do_tp()

            elif cancelled:
                logger.info("[scalp] 入場單取消確認 (phase=%s) → 冷卻", self._phase)
                self._entry_trade    = None
                self._direction      = 0
                self._phase          = "cooldown"
                self._cooldown_count = 0
                self._tick_buf.clear()

            return  # 入場單事件處理完畢，不往下走

        # ── 停利單回報 ─────────────────────────────────────────
        if self._phase == "holding" and self._tp_trade is not None:
            if oid != self._tp_trade.status.id:
                return

            if filled:
                self.state.realized_pnl += self.tp_pts * self.point_value * self._entry_qty
                self.state.position = 0
                self.state.entry_price = 0.0
                self._tp_trade = None
                self._phase = "cooldown"
                self._cooldown_count = 0
                logger.info("[scalp] 停利成交 +%d點 x%d口 → 冷卻", self.tp_pts, self._entry_qty)
