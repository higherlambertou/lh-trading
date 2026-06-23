import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from core.bar_builder import Bar, BarBuilder
from core.tick_store import tick_recorder

logger = logging.getLogger(__name__)

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
        self.bars = BarBuilder(interval_sec=60)

    def get_last_price(self, code: str) -> float | None:
        return self._last_price.get(code)

    def all_last_prices(self) -> dict[str, float]:
        return dict(self._last_price)

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

        self._last_price[code] = price

        # tick 落地 + 1 分 K 聚合
        ts  = snapshot.get("ts", time.time())
        vol = snapshot.get("volume", 0)
        tick_recorder.record(code, ts, price, vol, snapshot.get("tick_type", 0))
        done_bar = self.bars.feed(code, price, vol, ts)
        if done_bar and self._bar_subs and self._loop and self._loop.is_running():
            self._dispatch_bar_on_loop(done_bar)

        if not (self._strategies or self._ws_queues):
            return

        self._dispatch_on_loop(snapshot)

    # ── internal dispatch ─────────────────────────────────────────────

    def _dispatch_on_loop(self, snapshot: dict) -> None:
        if self._strategies:
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
