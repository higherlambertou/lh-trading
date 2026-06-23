import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import shioaji as sj

from core.bar_builder import Bar, BarBuilder
from core.tick_store import tick_recorder

logger = logging.getLogger(__name__)

QuoteCallback = Callable[[dict], Awaitable[None]]
BarCallback = Callable[[Bar], Awaitable[None]]

# contract code prefix → broker getter name
_PREFIX_TO_GETTER = {
    "TMF": "tmf_contract",
    "TXF": "txf_contract",
    "MXF": "mxf_contract",
}


class QuoteHub:
    """
    Single Shioaji quote callback dispatcher.
    Fans out to registered strategy coroutines and WebSocket queues.
    Survives broker reconnect via reinstall_after_reconnect().
    """

    def __init__(self) -> None:
        self._strategies: dict[str, QuoteCallback] = {}
        self._bar_subs: dict[str, BarCallback] = {}    # 策略名 → 1分K callback
        self._ws_queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._installed = False
        self._quote_seen = False
        self._subscribed_contracts: set[str] = set()   # actual month codes, e.g. TMFF6
        self._known_contract_types: set[str] = set()   # type prefixes, e.g. TMF, TXF
        self._ws_last_close: dict[str, float] = {}
        self._last_price: dict[str, float] = {}        # code → 最新成交價（含選擇權權利金）
        self.bars = BarBuilder(interval_sec=60)        # tick → 1 分 K 聚合

    def get_last_price(self, code: str) -> float | None:
        """任意已訂閱合約的最新成交價（推播快取）。手動選擇權停損停利用此取權利金。"""
        return self._last_price.get(code)

    def all_last_prices(self) -> dict[str, float]:
        """整份最新價快取（code→價）。前端面板讀此顯示台指現價，
        純讀我方記憶體、不對永豐發查詢。"""
        return dict(self._last_price)

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── reconnect hook ────────────────────────────────────────────────

    def reinstall_after_reconnect(self) -> None:
        """Called by broker.reconnect() — re-register callback and re-subscribe contracts."""
        self._installed = False
        self._subscribed_contracts.clear()
        self._ws_last_close.clear()
        self._last_price.clear()

        if not (self._strategies or self._ws_queues):
            return  # 沒人在聽，不需要安裝

        self._ensure_installed()

        from core.broker import broker
        for ctype in list(self._known_contract_types):
            getter = _PREFIX_TO_GETTER.get(ctype)
            if getter:
                try:
                    contract = getattr(broker, getter)()
                    self.ensure_contract_subscribed(contract)
                    # reconnect 跑在 executor（非 event loop）→ 同步種底價安全
                    self.seed_price_sync(contract)
                except Exception as e:
                    logger.warning("重連後重新訂閱 %s 失敗: %s", ctype, e)

        logger.info("QuoteHub 重連後重新安裝完成 (contracts: %s)", self._known_contract_types)

    # ── strategy subscriptions ────────────────────────────────────────

    def subscribe_strategy(self, name: str, callback: QuoteCallback) -> None:
        self._strategies[name] = callback
        self._ensure_installed()

    def unsubscribe_strategy(self, name: str) -> None:
        self._strategies.pop(name, None)

    def subscribe_strategy_bars(self, name: str, callback: BarCallback) -> None:
        """訂閱 1 分 K 完成事件（bar 收完才派發，不是每 tick）。"""
        self._bar_subs[name] = callback
        self._ensure_installed()

    def unsubscribe_strategy_bars(self, name: str) -> None:
        self._bar_subs.pop(name, None)

    # ── websocket client management ───────────────────────────────────

    def add_ws_client(self, q: asyncio.Queue) -> None:
        self._ws_queues.add(q)

    def remove_ws_client(self, q: asyncio.Queue) -> None:
        self._ws_queues.discard(q)

    # ── contract subscription ─────────────────────────────────────────

    def ensure_contract_subscribed(self, contract) -> None:
        code = getattr(contract, "code", str(contract))
        if code in self._subscribed_contracts:
            return
        # remember type prefix for reconnect
        prefix = code[:3]
        if prefix in _PREFIX_TO_GETTER:
            self._known_contract_types.add(prefix)

        # 一定要先掛上 v1 callback，再訂閱；否則 shioaji 會把報價綁到
        # 預設的 print handler，事後再 set callback 也蓋不掉 → 策略收不到報價
        self._ensure_installed()

        from core.broker import broker
        broker.api.quote.subscribe(
            contract,
            quote_type=sj.constant.QuoteType.Quote,
            version=sj.constant.QuoteVersion.v1,
        )
        self._subscribed_contracts.add(code)
        logger.info("QuoteHub 訂閱合約: %s", code)
        # ⚠️ 不要在這裡呼叫 snapshots() 種底價！
        # snapshots() 是 Solace C 層呼叫：不穩定時持 GIL 不放，asyncio.wait_for 的
        # timeout 需要 event loop 執行才能觸發，但 GIL 被佔後 loop 也跑不了 → 假超時。
        # 底價直接從報價訂閱推播的第一個 tick 取得（幾乎立即）；
        # reconnect 路徑（在 executor、非 loop）可用 seed_price_sync()。

    def seed_price_sync(self, contract) -> None:
        """用 snapshot 種「最後成交價」當底價（盤後/剛連線沒 tick 時的現價來源）。
        ⚠️ 同步阻塞——只能在『非 event loop 的執行緒』呼叫（如 reconnect 在 executor 跑）。
        啟動路徑請改用 seed_prices_async()。"""
        from core.broker import broker
        code = getattr(contract, "code", str(contract))
        try:
            snaps = broker.api.snapshots([contract])
            if snaps:
                px = float(getattr(snaps[0], "close", 0) or 0)
                if px > 0:
                    self._last_price[code] = px
                    logger.info("QuoteHub 種底價 %s = %s（snapshot）", code, px)
        except Exception as e:
            logger.warning("QuoteHub 種底價 snapshot 失敗 %s: %s", code, e)

    async def seed_prices_async(self, contracts: list) -> None:
        """非阻塞種底價：每檔用 broker.acall_to（丟 executor + 8s 硬逾時），
        不會卡住 event loop。給 startup 後的背景 task 用。"""
        from core.broker import broker
        for contract in contracts:
            code = getattr(contract, "code", str(contract))
            try:
                snaps = await broker.acall_to(lambda c=contract: broker.api.snapshots([c]))
                if snaps:
                    px = float(getattr(snaps[0], "close", 0) or 0)
                    if px > 0:
                        self._last_price[code] = px
                        logger.info("QuoteHub 種底價 %s = %s（snapshot）", code, px)
            except Exception as e:
                logger.warning("QuoteHub 種底價(async) 失敗 %s: %s", code, e)

    # ── internal ──────────────────────────────────────────────────────

    def _ensure_installed(self) -> None:
        if self._installed:
            return
        from core.broker import broker
        broker.api.quote.set_on_quote_fop_v1_callback(self._on_quote_sync)
        self._installed = True
        self._quote_seen = False
        logger.info("QuoteHub 已安裝 on_quote_fop_v1 callback")

    def _on_quote_sync(self, exchange: sj.Exchange, quote: sj.QuoteFOPv1) -> None:
        # ⚠️ 此函式由 shioaji 的 C 報價執行緒呼叫，每個 tick 都會進來。
        # 重要：shioaji 的 quote 物件是 Rust/PyO3 包裝，C callback 執行緒可能在下一個
        # tick 到來時覆寫同一個物件。若把 quote 物件跨執行緒傳給 event loop（call_soon_threadsafe），
        # event loop 再存取 quote.open/high/low 時可能觸發 PyBorrowMutError / 內部鎖死。
        # 解法：在 C callback 執行緒就把所有欄位萃取成純 Python dict，只傳 dict。
        if not getattr(self, "_quote_seen", False):
            self._quote_seen = True
            logger.info(
                "QuoteHub 收到首筆報價 code=%s close=%s → 開始派發給 %d 個策略",
                getattr(quote, "code", "?"), getattr(quote, "close", "?"),
                len(self._strategies),
            )
        try:
            price = float(quote.close)
            code  = str(quote.code)
            self._last_price[code] = price
        except (TypeError, ValueError):
            return

        # ── tick 落地 + 1分K 聚合（純記憶體/put_nowait，C 報價執行緒安全）──
        ts_ns = getattr(quote, "ts", None)
        ts = ts_ns / 1e9 if ts_ns else time.time()
        vol = int(getattr(quote, "volume", 0) or 0)
        tick_recorder.record(
            code, ts, price, vol, int(getattr(quote, "tick_type", 0) or 0)
        )
        done_bar = self.bars.feed(code, price, vol, ts)
        if done_bar and self._bar_subs and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._dispatch_bar_on_loop, done_bar)

        if not (self._strategies or self._ws_queues):
            return
        if self._loop and self._loop.is_running():
            # 在 C callback 執行緒萃取所有欄位 → 純 Python dict，不跨執行緒傳 Rust 物件
            snapshot = {
                "code": code,
                "close": price,
                "open": float(getattr(quote, "open", 0) or 0),
                "high": float(getattr(quote, "high", 0) or 0),
                "low": float(getattr(quote, "low", 0) or 0),
                "volume": vol,
                "total_volume": int(getattr(quote, "total_volume", 0) or 0),
                "change_price": float(getattr(quote, "change_price", 0) or 0),
                "tick_type": int(getattr(quote, "tick_type", 0) or 0),
                "ts": ts_ns / 1e9 if ts_ns else ts,
            }
            self._loop.call_soon_threadsafe(self._dispatch_on_loop, snapshot)

    def _dispatch_on_loop(self, snapshot: dict) -> None:
        """在 event loop 執行緒上同步執行：ws 廣播 + 排策略 task。
        接收純 Python dict（由 C callback 執行緒萃取），不含任何 Rust 物件。"""
        # 策略（async）：傳 snapshot dict，策略用 snap["close"] / snap["code"]
        if self._strategies:
            for cb in list(self._strategies.values()):
                self._loop.create_task(self._run_cb(cb, snapshot))

        # websocket 廣播
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
        """1分K 完成 → 派發給 bar 訂閱者（在 event loop 執行緒上）。"""
        for cb in list(self._bar_subs.values()):
            self._loop.create_task(self._run_bar_cb(cb, bar))

    async def _run_bar_cb(self, cb: BarCallback, bar: Bar) -> None:
        try:
            await cb(bar)
        except Exception as e:
            logger.error("QuoteHub bar dispatch error: %s", e)


quote_hub = QuoteHub()
