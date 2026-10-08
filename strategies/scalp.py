import asyncio
import logging
import os
import random
import time
from collections import deque
from typing import Any

from core.broker import broker
from core.daily_summary import market_state
from core.live_state import TradeDetector
from core.trade_log import trade_log
from strategies.base import BaseStrategy, POINT_VALUE_TMF, is_working_status

logger = logging.getLogger(__name__)

# 進場把關：max_qty 是「持倉上限」，每次進場前向券商確認實際持倉與未成交委託（2026-10-08 成交回報解析壞掉，
# 策略認不出自己的成交，一分鐘內送了 15 張單；只信策略自己記的部位不夠）。ENTRY_POSITION_CHECK=false 恢復舊行為（不查）。
ENTRY_POSITION_CHECK = os.getenv("ENTRY_POSITION_CHECK", "true").lower() == "true"
GATE_TIMEOUT_SEC = 3.0         # 查券商的逾時；查不到就不進場
GATE_BACKOFF_SEC = 3.0         # 被擋下後幾秒內不再查（訊號每個 tick 都可能再觸發）
GATE_LOG_EVERY_SEC = 30.0      # 同一個理由最多每 30 秒記一次


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
        self.market_bias: int = 0               # 0=不限 1=順勢 -1=逆勢 2=依今日市場狀態自動
        self._last_bias_reason: str = ""        # 偏向擋單的理由（同一理由只記一次）
        self.flow_source: int = 0               # 外/內盤統計來源：0=所有行情事件（現行）1=只算 TMF 真實成交
        self._trade_detector = TradeDetector()  # flow_source=1 時判定「真實成交」用

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
        self._cancelling: bool = False          # 入場單逾時處理進行中（擋重入）
        self._gate_hold_until: float = 0.0      # 進場靜默期（time.monotonic）：被把關擋下、或下單丟例外之後幾秒內不再嘗試
        self._gate_last_msg: str = ""
        self._gate_last_at: float = 0.0

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
            "market_bias": self.market_bias,
            "flow_source": self.flow_source,
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
            {"key": "max_qty",            "label": "最大口數（持倉上限）",    "type": "number", "min": 1,    "max": 10},
            {"key": "market_bias",        "label": "市場偏向 0=不限/1=順勢/-1=逆勢/2=自動", "type": "number", "min": -1, "max": 2},
            {"key": "flow_source",        "label": "外/內盤來源 0=所有事件(現行)/1=只算TMF真實成交", "type": "number", "min": 0, "max": 1},
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
        bias = int(params.get("market_bias", self.market_bias))
        self.market_bias        = bias if bias in (-1, 0, 1, 2) else 0
        self._last_bias_reason  = ""
        flow = int(params.get("flow_source", self.flow_source))
        self.flow_source        = flow if flow in (0, 1) else 0
        self._trade_detector    = TradeDetector()
        self._tick_buf = deque(maxlen=self.momentum_window)
        logger.info(
            "[scalp] 套用參數: TP=%d SL=%d offset=%d mode=%s window=%d threshold=%.2f cooldown=%d max_qty=%d bias=%d flow_source=%d",
            self.tp_pts, self.sl_pts, self.entry_offset, self.signal_mode,
            self.momentum_window, self.momentum_threshold, self.cooldown_ticks, self.max_qty,
            self.market_bias, self.flow_source,
        )

    # ── 啟動帶倉接管 ──────────────────────────────────────────────

    def _on_position_synced(self, net: int, avg_price: float) -> None:
        self._gate_hold_until = 0.0
        self._cancelling = False
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
        if self.flow_source == 1:
            # 只算 TMF 真實成交：報價更新（volume=0）、重複回報、MXF/TXF 的事件都不進視窗；
            # 也只在「新成交」那一刻判斷訊號（沒有新資訊就不重複觸發）。預設 0 = 維持舊算法
            code = str(quote.get("code", ""))
            if not code.startswith("TMF") or not self._trade_detector.is_trade(
                    code, int(quote.get("volume", 0) or 0), int(quote.get("total_volume", 0) or 0)):
                return 0
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

    def _apply_market_bias(self, sig: int) -> int:
        """market_bias != 0 時，只放行與「日K方向 × 偏向」同向的訊號，其餘擋下（回 0）。
        只讀 market_state 的記憶體快取，沒有 I/O，可放心跑在 tick 路徑上。"""
        if self.market_bias == 0:
            return sig
        want, reason = market_state.bias_direction(self.market_bias)
        if want == 0 or sig != want:
            self._note_bias_block(reason or f"訊號{'多' if sig > 0 else '空'}與偏向{'多' if want > 0 else '空'}不符")
            return 0
        self._last_bias_reason = ""
        return sig

    def _note_bias_block(self, reason: str) -> None:
        if reason == self._last_bias_reason:        # 同一理由只記一次，避免每個 tick 洗版
            return
        self._last_bias_reason = reason
        logger.info("[scalp] market_bias 擋下進場：%s", reason)
        self._event(f"偏向擋單：{reason}")

    # ── 主要 tick 邏輯 ──────────────────────────────────────────

    async def on_quote(self, quote: dict) -> None:
        price = float(quote["close"])

        if self._phase == "idle":
            if self.state.position != 0:
                logger.warning("[scalp] idle 但 position=%d，暫停進場", self.state.position)
                return
            sig = self._apply_market_bias(self._get_signal(quote))
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

    async def _lmt(self, action: str, price: float, qty: int = 1, kind: str = "",
                   signal_price: float | None = None, ref_price: float | None = None) -> dict:
        """掛 ROD 限價單，回傳 {"trade_id": ..., "status": ...}
        kind / signal_price / ref_price 只給成交紀錄用，不影響下單。"""
        with trade_log.context(strategy=self.name, reason=kind, signal_price=signal_price, ref_price=ref_price):
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
        if time.monotonic() < self._gate_hold_until:    # 剛被進場把關擋下：幾秒內不再查券商，也不讓每個訊號都重複查詢
            return
        ok, _reason = self._risk_ok()
        if not ok:
            return
        # 先改 state 再 await（擋掉報價重入）。上一張入場單的殘留也要先清掉：await 期間每個 tick 都會進 on_quote 的 pending 分支，
        # 殘留的舊 _entry_trade／_entry_tick_count 會讓逾時檢查立刻對「舊單」動手、把狀態切到冷卻（2026-10-08 實際發生）
        self._phase = "pending"
        self._entry_trade = None
        self._entry_tick_count = 0

        if ENTRY_POSITION_CHECK:
            ok, why = await self._entry_gate()
            if not ok:
                self._phase = "idle"
                self._gate_hold_until = time.monotonic() + GATE_BACKOFF_SEC
                self._note_gate_block(why)
                return

        entry_price = price - self.entry_offset * direction
        action = "Buy" if direction == 1 else "Sell"

        logger.info(
            "[scalp] %s 掛限價 @ %.0f  (現價=%.0f  offset=%+d  mode=%s  qty=%d)",
            "做多" if direction == 1 else "做空",
            entry_price, price, self.entry_offset, self.signal_mode, self.max_qty,
        )
        try:
            trade = await self._lmt(action, entry_price, qty=self.max_qty, kind="entry", signal_price=price)
        except Exception as e:
            logger.error("[scalp] 掛單失敗: %s", e)
            self.state.errors.append(f"掛單失敗: {e}")
            self._phase = "idle"
            self._gate_hold_until = time.monotonic() + GATE_BACKOFF_SEC   # 下單丟例外時別在下一個 tick 立刻重試（連帶每次 2 次券商查詢）
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

    # ── 進場把關：max_qty 是持倉上限，以券商實際狀態為準 ────────────────

    async def _entry_gate(self) -> tuple[bool, str]:
        """進場前向券商確認：TMF 部位加上未成交委託的總口數，再加這次要下的 max_qty 口，不能超過 max_qty。
        策略自己記的部位（state.position）可能是錯的；查不到就不進場（fail-closed）。"""
        try:
            net, gross, _avg = await self._fetch_broker_tmf(timeout=GATE_TIMEOUT_SEC)
            trades = await asyncio.wait_for(broker.list_trades_with_status(), timeout=GATE_TIMEOUT_SEC)
        except Exception as e:
            return False, f"無法確認券商部位與委託（{type(e).__name__}: {e}），為安全起見不進場"
        working = [t for t in trades
                   if is_working_status(t.get("status", "")) and str(t.get("code") or "TMF").startswith("TMF")]
        working_qty = sum(max(int(t.get("quantity", 0) or 0) - int(t.get("deal_quantity", 0) or 0), 1) for t in working)
        exposure = gross + working_qty
        if exposure + self.max_qty > self.max_qty:
            return False, (f"券商已有 TMF 部位 {net:+d} 口、未成交委託 {len(working)} 筆（合計 {exposure} 口），"
                           f"再進 {self.max_qty} 口會超過最大口數 {self.max_qty}")
        return True, ""

    def _note_gate_block(self, why: str) -> None:
        now = time.monotonic()
        if why == self._gate_last_msg and now - self._gate_last_at < GATE_LOG_EVERY_SEC:
            return                                  # 同一個理由 30 秒內只記一次，避免訊號每幾秒就洗一次版
        self._gate_last_msg, self._gate_last_at = why, now
        logger.warning("[scalp] 進場把關：%s", why)
        self._event(f"進場把關：{why}")
        self.state.errors.append(f"進場把關：{why}")
        del self.state.errors[:-50]

    # ── 入場單逾時 ──────────────────────────────────────────────

    def _entry_pending(self, trade: dict) -> bool:
        """這張入場單是不是還在等待處理。每個 await 之後都要再確認一次：期間成交／取消／失敗的回報可能已經改了狀態。"""
        return self._phase == "pending" and self._entry_trade is trade

    async def _recover_fill(self, trade: dict) -> bool:
        """用券商的委託狀態確認入場單有沒有成交（callback 漏接時的備援）。
        回 True＝這張單已經有結論，呼叫端不要再動狀態：已成交（含部分成交後被取消）並接成 holding，或查詢期間回報已經處理掉了。"""
        trade_id = trade.get("trade_id", "")
        try:
            trades = await asyncio.wait_for(broker.list_trades_with_status(), timeout=5)
        except Exception as e:
            logger.warning("[scalp] 查詢委託狀態失敗: %s", e)
            return False
        if not self._entry_pending(trade):
            return True
        matching = next((t for t in trades if t.get("id") == trade_id), None)
        if not matching or is_working_status(matching.get("status", "")):
            return False                            # 查不到或還在場上 → 繼續取消流程
        filled = int(matching.get("deal_quantity", 0) or 0)
        if filled <= 0 and matching.get("status") == "Filled":
            filled = self._entry_qty
        if filled <= 0:
            return False                            # 已結束、沒成交（取消／被拒）→ 照原流程進冷卻
        fill_price = matching.get("avg_deal_price", 0) or self._pending_entry_price or self.state.last_price
        logger.warning("[scalp] 逾時補抓成交 %d口 @ %.0f → holding", filled, fill_price)
        self._event(f"逾時補抓成交 {filled}口 @ {fill_price:.0f}（callback 未進）")
        self._entry_qty          = filled
        self._entry_filled_qty   = filled
        self._entry_filled_value = fill_price * filled
        self._tp_filled_qty      = 0
        self._last_entry_price   = fill_price
        self.state.entry_price   = fill_price
        self.state.position      = self._direction * filled
        self._entry_trade        = None
        self._consec_failures    = 0
        self._phase              = "holding"
        await self._do_tp()
        return True

    async def _cancel_entry(self) -> None:
        if self._phase != "pending" or self._entry_trade is None or self._cancelling:
            return
        # 先改 state 再 await：掛單逾時之後每個 tick 都會走到這裡，不擋的話同一張單會被同時查詢、取消十幾次（2026-10-08 實際發生）
        self._cancelling = True
        trade = self._entry_trade
        trade_id = trade.get("trade_id", "")
        try:
            logger.info("[scalp] 入場單逾時，查詢狀態 trade_id=%s", trade_id)
            if trade_id:
                if await self._recover_fill(trade):
                    return
                try:
                    await asyncio.wait_for(broker.cancel_order(trade_id), timeout=5)
                except Exception as e:
                    logger.warning("[scalp] cancel_order 失敗: %s", e)
                    # 取消失敗最常見的原因是單子已經成交或被拒絕（「無原委託內容」）——不能直接當成已取消，再查一次
                    if await self._recover_fill(trade):
                        return
            if not self._entry_pending(trade):      # 等待期間成交／取消／失敗的回報已經處理過，不要覆蓋它的結果
                return
            self._phase          = "cooldown"
            self._cooldown_count = 0
            self._tick_buf.clear()
            logger.info("[scalp] 入場單取消請求已送出 → 冷卻")
        finally:
            self._cancelling = False

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
            self._tp_trade = await self._lmt(action, tp_price, qty=self._entry_qty, kind="tp",
                                             signal_price=self.state.last_price or None, ref_price=tp_price)
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
            await self.place_order(close_action, qty, kind="sl",
                                   ref_price=self._last_entry_price - direction * self.sl_pts)
        except Exception as e:
            logger.error("[scalp] 停損平倉失敗: %s", e)
            self.state.errors.append(f"停損平倉失敗: {e}")
            return

        self._add_realized(-self.sl_pts * self.point_value * qty)
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

        self._add_realized(self.tp_pts * self.point_value * self._entry_qty)
        self.state.position    = 0
        self.state.entry_price = 0.0
        self._tp_trade         = None
        self._phase            = "cooldown"
        self._cooldown_count   = 0
        logger.info("[scalp] 停利成交完成 +%d點 x%d口 → 冷卻", self.tp_pts, self._entry_qty)
        self._event(f"停利成交 +{self.tp_pts}點 x{self._entry_qty}口 → 冷卻")
