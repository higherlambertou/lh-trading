import asyncio
import logging
import random
from collections import deque
from typing import Any

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

    def __init__(self) -> None:
        super().__init__()
        self.tp_pts: int = 20
        self.sl_pts: int = 60
        self.entry_offset: int = 0
        self.cancel_after_ticks: int = 15
        self.momentum_window: int = 20
        self.momentum_threshold: float = 0.65
        self.signal_mode: str = "momentum"
        self.cooldown_ticks: int = 30
        self.max_qty: int = 1

        self._phase: str = "idle"
        self._direction: int = 0
        self._entry_trade: dict | None = None   # {"trade_id": ..., "status": ...}
        self._tp_trade: dict | None = None
        self._entry_qty: int = 1
        self._entry_tick_count: int = 0
        self._cooldown_count: int = 0
        self._last_entry_price: float = 0.0
        self._pending_entry_price: float = 0.0
        self._tick_buf: deque[int] = deque(maxlen=100)
        self._need_tp_resubmit: bool = False
        self._consec_failures: int = 0          # 連續入場失敗次數（退避用）

        self._entry_filled_qty: int = 0
        self._entry_filled_value: float = 0.0
        self._tp_filled_qty: int = 0

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
            **self._base_params,
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
            *self._base_param_schema,
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

    # ── 啟動帶倉接管 ──────────────────────────────────────────────

    def _on_position_synced(self, net: int, avg_price: float) -> None:
        if net == 0:
            self._phase = "idle"
            return
        self._direction          = 1 if net > 0 else -1
        self._entry_qty          = abs(net)
        self._last_entry_price   = avg_price
        self.state.entry_price   = avg_price
        self._entry_filled_qty   = self._entry_qty
        self._entry_filled_value = avg_price * self._entry_qty
        self._tp_filled_qty      = 0
        self._tp_trade           = None
        self._entry_trade        = None
        self._need_tp_resubmit   = True
        self._phase              = "holding"
        side = "多" if self._direction == 1 else "空"
        logger.info("[scalp] 啟動帶倉接管：%s %d口 @ %.0f → holding，待補掛停利",
                    side, self._entry_qty, avg_price)
        self._event(f"接管既有部位 {side} {self._entry_qty}口 @ {avg_price:.0f}")

    # ── 訊號 ───────────────────────────────────────────────────────

    def _get_signal(self, quote: dict) -> int:
        if self.signal_mode == "random":
            return random.choice([1, -1])

        tt = int(quote.get("tick_type", 0))
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

    async def on_quote(self, quote: dict) -> None:
        price = float(quote["close"])

        if self._phase == "idle":
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
            if self._need_tp_resubmit:
                self._need_tp_resubmit = False
                await self._do_tp()
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

    async def _lmt(self, action: str, price: float, qty: int = 1) -> dict:
        """掛 ROD 限價單，回傳 {"trade_id": ..., "status": ...}"""
        return await broker.place_order(
            contract_code="TMF",
            action=action,
            quantity=qty,
            price=round(price),
            price_type="LMT",
            order_type="ROD",
            octype="Auto",
        )

    async def _cancel_safe(self, trade: dict | None) -> None:
        if not trade:
            return
        trade_id = trade.get("trade_id", "")
        if not trade_id:
            return
        try:
            await asyncio.wait_for(broker.cancel_order(trade_id), timeout=5)
        except Exception as e:
            logger.warning("[scalp] 取消委託失敗: %s", e)

    # ── 狀態切換動作 ────────────────────────────────────────────

    async def _do_enter(self, price: float, direction: int) -> None:
        if self._phase != "idle":
            return
        ok, _reason = self._risk_ok()
        if not ok:
            return
        self._phase = "pending"

        entry_price = price - self.entry_offset * direction
        action = "Buy" if direction == 1 else "Sell"

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
            self._phase = "idle"
            return

        self._trades_today += 1
        self._entry_trade         = trade
        self._entry_qty           = self.max_qty
        self._direction           = direction
        self._last_entry_price    = 0.0
        self._pending_entry_price = entry_price
        self._entry_tick_count    = 0
        self._entry_filled_qty    = 0
        self._entry_filled_value  = 0.0
        self._tp_filled_qty       = 0
        self._phase               = "pending"
        self._event(f"掛{'多' if direction == 1 else '空'}限價 @ {entry_price:.0f} x{self.max_qty}口")

    async def _cancel_entry(self) -> None:
        if self._phase != "pending" or self._entry_trade is None:
            return

        trade_id = self._entry_trade.get("trade_id", "")
        logger.info("[scalp] 入場單逾時，查詢狀態 trade_id=%s", trade_id)

        if trade_id:
            try:
                trades = await asyncio.wait_for(broker.list_trades_with_status(), timeout=5)
                matching = next((t for t in trades if t.get("id") == trade_id), None)
                if matching and "Filled" in matching.get("status", ""):
                    fill_price = (
                        matching.get("avg_deal_price", 0)
                        or self._pending_entry_price
                        or self.state.last_price
                    )
                    logger.warning("[scalp] 逾時補抓成交 @ %.0f → holding", fill_price)
                    self._event(f"逾時補抓成交 @ {fill_price:.0f}（callback 未進）")
                    self._last_entry_price = fill_price
                    self.state.entry_price = fill_price
                    self.state.position    = self._direction
                    self._entry_trade      = None
                    self._phase            = "holding"
                    await self._do_tp()
                    return
            except Exception as e:
                logger.warning("[scalp] 查詢委託狀態失敗: %s", e)

            try:
                await asyncio.wait_for(broker.cancel_order(trade_id), timeout=5)
            except Exception as e:
                logger.warning("[scalp] cancel_order 失敗: %s", e)

        self._phase          = "cooldown"
        self._cooldown_count = 0
        self._tick_buf.clear()
        logger.info("[scalp] 入場單取消請求已送出 → 冷卻")

    async def _do_tp(self) -> None:
        if self._last_entry_price == 0:
            logger.error("[scalp] _last_entry_price=0，無法計算停利價，略過")
            self.state.errors.append("停利單未掛：entry_price 未知")
            return
        self._tp_filled_qty = 0
        tp_price = self._last_entry_price + self.tp_pts * self._direction
        action = "Sell" if self._direction == 1 else "Buy"
        logger.info("[scalp] 掛停利單 @ %.0f  qty=%d", tp_price, self._entry_qty)
        try:
            self._tp_trade = await self._lmt(action, tp_price, qty=self._entry_qty)
        except Exception as e:
            logger.error("[scalp] 掛停利單失敗: %s", e)
            self.state.errors.append(f"掛停利單失敗: {e}")

    async def _do_sl(self) -> None:
        if self._phase != "holding":
            return
        direction = self._direction
        qty       = self._entry_qty
        self._phase          = "cooldown"
        # SL 後多等 2 倍冷卻，讓券商端確認部位歸零、保證金釋放，再開新倉
        self._cooldown_count = -(self.cooldown_ticks)

        await self._cancel_safe(self._tp_trade)
        self._tp_trade = None

        close_action = "Sell" if direction == 1 else "Buy"
        try:
            await self.place_order(close_action, qty)
        except Exception as e:
            logger.error("[scalp] 停損平倉失敗: %s", e)
            self.state.errors.append(f"停損平倉失敗: {e}")
            return

        self.state.realized_pnl += -self.sl_pts * self.point_value * qty
        self.state.position = 0
        self.state.entry_price = 0.0
        self._event(f"停損出場 -{self.sl_pts}點 x{qty}口 → 冷卻")

    # ── 成交回報 ────────────────────────────────────────────────

    async def on_order_event(self, event: dict, _unused: Any = None) -> None:
        """
        接收 broker event dict（已萃取為純 Python dict）：
          state     = "FuturesOrder" | "FuturesDeal"
          trade_id  = 委託/成交識別碼
          price     = 成交價（Deal 才有）
          quantity  = 成交量（Deal 才有）
          op_type   = 委託操作類型（Order 才有）
          op_code   = "00"=正常；其他=失敗代碼
          op_msg    = 失敗說明
        """
        state_name = event.get("state", "")
        is_deal    = "Deal" in state_name
        is_order   = "Order" in state_name

        ev_trade_id    = event.get("trade_id", "")
        entry_trade_id = (self._entry_trade or {}).get("trade_id", "")
        tp_trade_id    = (self._tp_trade    or {}).get("trade_id", "")
        is_entry = bool(ev_trade_id and ev_trade_id == entry_trade_id)
        is_tp    = bool(ev_trade_id and ev_trade_id == tp_trade_id)

        logger.info(
            "[scalp] 回報 state=%s trade_id=%s entry=%s tp=%s",
            state_name, ev_trade_id, is_entry, is_tp,
        )

        if is_deal:
            price = float(event.get("price", 0) or 0)
            qty   = int(event.get("quantity", 0) or 0)
            if is_entry and self._entry_trade is not None:
                await self._on_entry_deal(price, qty)
            elif is_tp and self._tp_trade is not None:
                await self._on_tp_deal(qty)
            return

        if is_order:
            op_code = event.get("op_code", "")
            op_type = event.get("op_type", "")
            op_msg  = event.get("op_msg", "")

            if op_code not in ("", "00"):
                which = "入場單" if is_entry else ("停利單" if is_tp else "委託")
                logger.warning("[scalp] %s失敗 op_type=%s op_code=%s op_msg=%s",
                               which, op_type, op_code, op_msg)
                if is_entry and self._phase == "pending":
                    self._consec_failures += 1
                    # 連續失敗退避：1次=1x, 2次=2x, 3次=4x, 4次以上=8x cooldown
                    backoff = min(2 ** (self._consec_failures - 1), 8)
                    effective_cooldown = self.cooldown_ticks * backoff
                    logger.warning("[scalp] 入場連續失敗 %d 次，冷卻 %d ticks",
                                   self._consec_failures, effective_cooldown)
                    self._event(f"{which}失敗（{self._consec_failures}連敗）：{op_msg or op_code}")
                    self.state.errors.append(f"{which}失敗：{op_msg or op_code}")
                    self._entry_trade    = None
                    self._direction      = 0
                    self._phase          = "cooldown"
                    self._cooldown_count = -effective_cooldown + self.cooldown_ticks
                    self._tick_buf.clear()
                else:
                    self._event(f"{which}失敗：{op_msg or op_code}")
                    self.state.errors.append(f"{which}失敗：{op_msg or op_code}")
                return

            cancelled = op_type == "Cancel" and op_code in ("", "00")
            if is_entry and cancelled and self._phase != "holding":
                logger.info("[scalp] 入場單取消確認 → 冷卻")
                self._event("入場單取消（未成交）→ 冷卻")
                self._entry_trade    = None
                self._direction      = 0
                self._phase          = "cooldown"
                self._cooldown_count = 0
                self._tick_buf.clear()

    async def _on_entry_deal(self, price: float, qty: int) -> None:
        if qty <= 0:
            qty = self._entry_qty
        if price <= 0:
            price = self._pending_entry_price or self.state.last_price

        self._entry_filled_value += price * qty
        self._entry_filled_qty   += qty
        if self._entry_filled_qty < self._entry_qty:
            logger.info("[scalp] 入場部分成交 %d/%d @ %.0f",
                        self._entry_filled_qty, self._entry_qty, price)
            return

        avg = (
            self._entry_filled_value / self._entry_filled_qty
            if self._entry_filled_qty else price
        )
        self._last_entry_price  = avg
        self.state.entry_price  = avg
        self.state.position     = self._direction * self._entry_qty
        self._entry_trade       = None
        self._phase             = "holding"
        self._consec_failures   = 0  # 成功入場，重置退避計數
        side = "多" if self._direction == 1 else "空"
        logger.info("[scalp] 入場成交完成 均價 %.0f x%d口 方向=%s → 掛停利",
                    avg, self._entry_qty, side)
        self._event(f"入場成交 {side} {self._entry_qty}口 @ {avg:.0f}")
        await self._do_tp()

    async def _on_tp_deal(self, qty: int) -> None:
        if qty <= 0:
            qty = self._entry_qty
        self._tp_filled_qty += qty
        if self._tp_filled_qty < self._entry_qty:
            logger.info("[scalp] 停利部分成交 %d/%d", self._tp_filled_qty, self._entry_qty)
            return

        self.state.realized_pnl += self.tp_pts * self.point_value * self._entry_qty
        self.state.position    = 0
        self.state.entry_price = 0.0
        self._tp_trade         = None
        self._phase            = "cooldown"
        self._cooldown_count   = 0
        logger.info("[scalp] 停利成交完成 +%d點 x%d口 → 冷卻", self.tp_pts, self._entry_qty)
        self._event(f"停利成交 +{self.tp_pts}點 x{self._entry_qty}口 → 冷卻")
