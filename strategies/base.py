import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from core.broker import broker
from core.quote_hub import quote_hub
from core.trade_log import trade_log

logger = logging.getLogger(__name__)

POINT_VALUE_TXF = 200
POINT_VALUE_MXF = 50
POINT_VALUE_TMF = 10

# 逐 tick 策略只吃 TMF 的行情（各策略的 quote_prefix 決定）。行情事件是 TMF/MXF/TXF 三個合約交錯進來的（約一半的相鄰兩筆是不同合約，
# 換合約時平均差 2.8 點、對照同合約 1.1 點），均線、突破、RSI、布林一直被誤觸，停損停利與未實現損益也在合約之間跳動——見 THRESHOLDS.md。
# TICK_STRATEGIES_TMF_ONLY=false 可恢復舊行為（所有合約都吃）。
QUOTE_PREFIX_FILTER = os.getenv("TICK_STRATEGIES_TMF_ONLY", "true").lower() == "true"


def _parse_hhmm(raw: str, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if 0 <= v // 100 < 24 and 0 <= v % 100 < 60 else default


# 風控的「當日」＝台指期的交易日：15:00 夜盤開盤到隔天 13:45 日盤收盤算同一天。
# 原本用日曆日（00:00 換日）會把夜盤切成兩半；而且「當日最大虧損」比的是後端啟動以來的累計損益，根本不會歸零（update.md 發現 20）。
# RISK_DAY_START=HHMM 可改換日時間，0 = 日曆日（00:00）。
RISK_DAY_START = _parse_hhmm(os.getenv("RISK_DAY_START", "1500"), 1500)
_DAY_START_TEXT = f"{RISK_DAY_START // 100:02d}:{RISK_DAY_START % 100:02d}"


def risk_day_key(now: datetime, start_hhmm: Optional[int] = None) -> str:
    """風控「當日」的鍵：時間往回推 start_hhmm 再取日期，所以每天剛好在 start_hhmm 換日。"""
    h, m = divmod(RISK_DAY_START if start_hhmm is None else start_hhmm, 100)
    return (now - timedelta(hours=h, minutes=m)).strftime("%Y-%m-%d")


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
    quote_prefix: Optional[str] = None      # 只處理這個前綴的合約行情（如 "TMF"）；None = 所有合約（舊行為）

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
        self._day_base: float = 0.0         # 換交易日當下的累計損益（已實現＋未實現）；當日損益 = 現在的累計 − 這個
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
            {"key": "daily_max_loss",     "label": f"當日最大虧損元（交易日 {_DAY_START_TEXT} 起算，0=停用）",   "type": "number", "min": 0, "max": 1000000},
            {"key": "max_trades_per_day", "label": f"當日最大進場次數（交易日 {_DAY_START_TEXT} 起算，0=停用）", "type": "number", "min": 0, "max": 500},
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
        self._risk_halted = False
        self._roll_risk_day()        # 同一交易日內停止再啟動會保留當日損益基準：重啟策略不能繞過當日虧損上限
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
        await self._cancel_all_pending()
        logger.info("策略 [%s] 已停止", self.name)

    async def _check_sl_tp(self, price: float) -> bool:
        if self.state.position == 0:
            return False
        pts = (price - self.state.entry_price) * (1 if self.state.position > 0 else -1)
        triggered = False
        kind = ""
        if self.take_profit_pts > 0 and pts >= self.take_profit_pts:
            logger.info("策略 [%s] 停利觸發: +%.0f點 @ %.0f", self.name, pts, price)
            triggered = True
            kind = "tp"
        elif self.stop_loss_pts > 0 and pts <= -self.stop_loss_pts:
            logger.info("策略 [%s] 停損觸發: %.0f點 @ %.0f", self.name, pts, price)
            triggered = True
            kind = "sl"
        if not triggered:
            return False

        prev_pos   = self.state.position
        prev_entry = self.state.entry_price
        action = "Sell" if prev_pos > 0 else "Buy"
        qty = abs(prev_pos)
        # 成交紀錄用：設定的停損/停利價位（與實際成交價的差 = 該單的滑價）
        ref = prev_entry + (1 if prev_pos > 0 else -1) * (
            self.take_profit_pts if kind == "tp" else -self.stop_loss_pts)
        self.state.position = 0
        self.state.entry_price = 0.0
        self.state.unrealized_pnl = 0.0
        try:
            await self.place_order(action, qty, kind=kind, ref_price=ref)
        except Exception as e:
            self.state.position = prev_pos
            self.state.entry_price = prev_entry
            logger.error("策略 [%s] 停損停利平倉失敗，還原部位: %s", self.name, e)
            self.state.errors.append(f"停損停利平倉失敗: {e}")
            return True
        self._add_realized(pts * qty * self.point_value)
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
                await self.place_order(action, close_qty, kind="reverse_close")
                pts = (price - prev_entry) * (1 if prev_pos > 0 else -1)
                self._add_realized(pts * close_qty * self.point_value)
            await self.place_order(action, 1, kind="entry")
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
        if QUOTE_PREFIX_FILTER and self.quote_prefix and not str(quote.get("code", "")).startswith(self.quote_prefix):
            return          # 其他合約的行情：不更新價格序列、不算未實現損益、也不拿來檢查停損停利
        price = float(quote["close"])
        self.state.last_price = price
        self._roll_risk_day()       # 要在更新未實現損益之前：跨交易日帶倉的跳空算新的一天

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

    def _now(self) -> datetime:
        return datetime.now()

    def _day_pnl(self) -> float:
        """當日（交易日）損益 = 累計損益（已實現＋未實現）− 換日當下的基準。"""
        return self.state.realized_pnl + self.state.unrealized_pnl - self._day_base

    def _roll_risk_day(self, now: Optional[datetime] = None) -> None:
        """換交易日就重設當日進場次數與停機旗標，並把當日損益基準設成「換日當下」的累計損益。
        必須在新的一天的第一筆損益變動之前呼叫：行情進來先於更新未實現損益、已實現損益一律走 _add_realized，
        基準才會是換日前的累計值（否則換日後的第一筆虧損會被算進基準而漏掉）。"""
        key = risk_day_key(now or self._now())
        if key == self._risk_day:
            return
        self._risk_day = key
        self._day_base = self.state.realized_pnl + self.state.unrealized_pnl
        self._trades_today = 0
        self._risk_halted = False
        logger.info("策略 [%s] 換交易日 %s：當日損益基準 %+.0f 元，進場次數歸零", self.name, key, self._day_base)

    def _add_realized(self, amount: float) -> None:
        """所有已實現損益都走這裡（策略裡不要直接累加 state.realized_pnl，tests/test_risk_day.py 會檢查）。"""
        self._roll_risk_day()
        self.state.realized_pnl += amount

    def _risk_ok(self) -> tuple[bool, str]:
        now = self._now()
        self._roll_risk_day(now)

        if self._risk_halted:
            return False, "已觸發當日虧損上限，今日停止開倉"

        if self.daily_max_loss > 0:
            day_pnl = self._day_pnl()
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
        kind: str = "",
        ref_price: Optional[float] = None,
    ):
        # action is "Buy" or "Sell"
        # kind / ref_price 只給成交紀錄用（原因 entry/tp/sl/trail…、停損停利設定價），不影響下單。
        # 參數不叫 reason：下面風控檢查的區域變數 reason 會把它蓋掉
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
            with trade_log.context(strategy=self.name, reason=kind or ("exit" if is_reducing else "entry"),
                                   signal_price=self.state.last_price or None, ref_price=ref_price):
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
