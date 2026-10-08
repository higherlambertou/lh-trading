import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Awaitable, Callable

from core.bar_builder import Bar, BarBuilder
from core.live_state import live_state
from core.tick_store import tick_recorder

logger = logging.getLogger(__name__)

# 開盤前試算時段（HHMM，含起點、不含終點）：日盤 08:30~08:45、夜盤 14:50~15:00（兩段都用實測驗證過：日盤價差平均 189 點、夜盤平均約 142 點、最大 329 點，15:00 一開盤就降到個位數）。
# 這段時間的行情是「試算價」不是真實成交：實測同一分鐘內 TMF/MXF/TXF 的價差平均 189 點、最大 270 點（正常盤最大約 17 點），
# 會污染策略的指標、K 棒（vwap_revert 會在 08:45 第一根就被誤導做空）與買賣力道——見 THRESHOLDS.md、update.md 發現 10。
# 處理方式：只讓畫面（WebSocket）看到，不餵給策略／K 棒／即時狀態／tick 落地。FILTER_PREOPEN_QUOTES=false 可恢復舊行為。
PREOPEN_WINDOWS = ((830, 845), (1450, 1500))
FILTER_PREOPEN = os.getenv("FILTER_PREOPEN_QUOTES", "true").lower() == "true"


def in_preopen(ts: float) -> bool:
    lt = time.localtime(ts)
    hhmm = lt.tm_hour * 100 + lt.tm_min
    return any(a <= hhmm < b for a, b in PREOPEN_WINDOWS)


QuoteCallback = Callable[[dict], Awaitable[None]]
BarCallback = Callable[[Bar], Awaitable[None]]


class QuoteHub:
    """
    報價派發中樞。
    Worker 子進程把每個 tick 萃取為純 Python dict，由 broker 的 event reader thread
    透過 _inject_quote() → call_soon_threadsafe → _dispatch_on_loop 送進 event loop。
    完全不碰 shioaji C/Rust 物件。
    """

    def __init__(self) -> None:
        self._strategies: dict[str, QuoteCallback] = {}
        self._bar_subs: dict[str, BarCallback] = {}
        self._ws_queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._quote_seen = False
        self._ws_last_close: dict[str, float] = {}
        self._last_price: dict[str, float] = {}
        self._daily_high: dict[str, float] = {}
        self._daily_low: dict[str, float] = {}
        self._daily_date: dict[str, str] = {}
        self.bars = BarBuilder(interval_sec=60)
        self._in_preopen = False                  # 目前是否在試算時段（用來記進入／離開的 log）
        self._pre_skipped = 0                     # 這一段試算時段略過了幾筆
        self._pre_flagged = 0                     # 其中帶 simtrade 旗標的幾筆（核對旗標與時段是否一致）
        self._simtrade_outside = 0                # 試算時段以外收到 simtrade 旗標的次數（最多記 5 次 warning）

    def get_last_price(self, code: str) -> float | None:
        return self._last_price.get(code)

    def all_last_prices(self) -> dict[str, float]:
        return dict(self._last_price)

    def last_price_by_prefix(self, prefix: str) -> float | None:
        """依合約前綴（TMF/MXF/TXF）取最新價；報價快取的 key 是實際合約代碼（如 TMFJ6）。"""
        for code, px in self._last_price.items():
            if code.startswith(prefix):
                return px
        return None

    def daily_ohlc(self) -> dict[str, dict[str, float]]:
        result = {}
        for code in self._last_price:
            result[code] = {
                "last":  self._last_price.get(code, 0.0),
                "high":  self._daily_high.get(code, 0.0),
                "low":   self._daily_low.get(code, 0.0),
            }
        return result

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── strategy subscriptions ────────────────────────────────────────

    def subscribe_strategy(self, name: str, callback: QuoteCallback) -> None:
        self._strategies[name] = callback

    def unsubscribe_strategy(self, name: str) -> None:
        self._strategies.pop(name, None)

    def subscribe_strategy_bars(self, name: str, callback: BarCallback) -> None:
        self._bar_subs[name] = callback

    def unsubscribe_strategy_bars(self, name: str) -> None:
        self._bar_subs.pop(name, None)

    # ── websocket client management ───────────────────────────────────

    def add_ws_client(self, q: asyncio.Queue) -> None:
        self._ws_queues.add(q)

    def remove_ws_client(self, q: asyncio.Queue) -> None:
        self._ws_queues.discard(q)

    # ── quote injection（由 broker event reader thread 呼叫）───────────

    def _inject_quote(self, snapshot: dict) -> None:
        """在 event loop 執行緒上被 call_soon_threadsafe 呼叫，snapshot 已是純 Python dict。"""
        code  = snapshot.get("code", "")
        price = snapshot.get("close", 0.0)
        if not code or not price:
            return

        if not self._quote_seen:
            self._quote_seen = True
            logger.info(
                "QuoteHub 收到首筆報價 code=%s close=%s → 開始派發給 %d 個策略",
                code, price, len(self._strategies),
            )

        ts = snapshot.get("ts", time.time())
        preopen = self._check_preopen(code, ts, snapshot)

        self._last_price[code] = price
        if preopen:
            # 試算行情只讓畫面看到（維持原本的顯示），不更新日高日低、不餵策略／K 棒／即時狀態／落地
            if self._ws_queues:
                self._dispatch_on_loop(snapshot, strategies=False)
            return

        # 日高日低（日期變換自動重置）
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_date.get(code) != today:
            self._daily_date[code] = today
            self._daily_high[code] = price
            self._daily_low[code]  = price
        else:
            if price > self._daily_high.get(code, price):
                self._daily_high[code] = price
            if price < self._daily_low.get(code, price):
                self._daily_low[code] = price

        # tick 落地 + 1 分 K 聚合
        vol = snapshot.get("volume", 0)
        # 盤中即時狀態（只給儀表板顯示）：任何例外都不可影響下面的報價派發
        try:
            live_state.feed(code, price, vol, snapshot.get("total_volume", 0),
                            snapshot.get("tick_type", 0), ts)
        except Exception:
            logger.debug("live_state.feed 失敗（已忽略）", exc_info=True)
        tick_recorder.record(code, ts, price, vol, snapshot.get("tick_type", 0), snapshot.get("total_volume", 0))
        done_bar = self.bars.feed(code, price, vol, ts)
        if done_bar and self._bar_subs and self._loop and self._loop.is_running():
            self._dispatch_bar_on_loop(done_bar)

        if not (self._strategies or self._ws_queues):
            return

        self._dispatch_on_loop(snapshot)

    # ── internal dispatch ─────────────────────────────────────────────

    def _check_preopen(self, code: str, ts: float, snapshot: dict) -> bool:
        """這筆行情是不是開盤前的試算行情（PREOPEN_WINDOWS 內）；順便記下進入／離開時段與略過的筆數。"""
        if not FILTER_PREOPEN:
            return False
        if in_preopen(ts):
            if not self._in_preopen:
                self._in_preopen = True
                self._pre_skipped = self._pre_flagged = 0
                logger.info("進入開盤前試算時段（%s）：試算行情只顯示在畫面，不餵給策略／K 棒／即時狀態／落地",
                            time.strftime("%H:%M", time.localtime(ts)))
            self._pre_skipped += 1
            if snapshot.get("simtrade"):
                self._pre_flagged += 1
            return True
        if self._in_preopen:
            self._in_preopen = False
            logger.info("開盤前試算時段結束：略過 %d 筆試算行情（其中帶 simtrade 旗標 %d 筆）", self._pre_skipped, self._pre_flagged)
        elif snapshot.get("simtrade") and self._simtrade_outside < 5:
            self._simtrade_outside += 1
            logger.warning("試算時段以外收到 simtrade 旗標的行情（%s %s）：目前不會被擋；若是新的試算時段請回報",
                           code, time.strftime("%H:%M:%S", time.localtime(ts)))
        return False

    def _dispatch_on_loop(self, snapshot: dict, strategies: bool = True) -> None:
        if strategies and self._strategies:
            for cb in list(self._strategies.values()):
                self._loop.create_task(self._run_cb(cb, snapshot))

        if not self._ws_queues:
            return
        close = snapshot["close"]
        code  = snapshot["code"]
        if self._ws_last_close.get(code) == close:
            return
        self._ws_last_close[code] = close
        msg = json.dumps(snapshot)
        for q in list(self._ws_queues):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def _run_cb(self, cb: QuoteCallback, snapshot: dict) -> None:
        try:
            await cb(snapshot)
        except Exception as e:
            logger.error("QuoteHub strategy dispatch error: %s", e)

    def _dispatch_bar_on_loop(self, bar: Bar) -> None:
        for cb in list(self._bar_subs.values()):
            self._loop.create_task(self._run_bar_cb(cb, bar))

    async def _run_bar_cb(self, cb: BarCallback, bar: Bar) -> None:
        try:
            await cb(bar)
        except Exception as e:
            logger.error("QuoteHub bar dispatch error: %s", e)


quote_hub = QuoteHub()
