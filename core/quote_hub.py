import asyncio
import json
import logging
from typing import Awaitable, Callable

import shioaji as sj

logger = logging.getLogger(__name__)

QuoteCallback = Callable[[sj.QuoteFOPv1], Awaitable[None]]

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
        self._ws_queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._installed = False
        self._subscribed_contracts: set[str] = set()   # actual month codes, e.g. TMFF6
        self._known_contract_types: set[str] = set()   # type prefixes, e.g. TMF, TXF
        self._ws_last_close: dict[str, float] = {}

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── reconnect hook ────────────────────────────────────────────────

    def reinstall_after_reconnect(self) -> None:
        """Called by broker.reconnect() — re-register callback and re-subscribe contracts."""
        self._installed = False
        self._subscribed_contracts.clear()
        self._ws_last_close.clear()

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
                except Exception as e:
                    logger.warning("重連後重新訂閱 %s 失敗: %s", ctype, e)

        logger.info("QuoteHub 重連後重新安裝完成 (contracts: %s)", self._known_contract_types)

    # ── strategy subscriptions ────────────────────────────────────────

    def subscribe_strategy(self, name: str, callback: QuoteCallback) -> None:
        self._strategies[name] = callback
        self._ensure_installed()

    def unsubscribe_strategy(self, name: str) -> None:
        self._strategies.pop(name, None)

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

        from core.broker import broker
        broker.api.quote.subscribe(
            contract,
            quote_type=sj.constant.QuoteType.Quote,
            version=sj.constant.QuoteVersion.v1,
        )
        self._subscribed_contracts.add(code)
        logger.info("QuoteHub 訂閱合約: %s", code)

    # ── internal ──────────────────────────────────────────────────────

    def _ensure_installed(self) -> None:
        if self._installed:
            return
        from core.broker import broker
        broker.api.set_on_quote_fop_v1_callback(self._on_quote_sync)
        self._installed = True

    def _on_quote_sync(self, exchange: sj.Exchange, quote: sj.QuoteFOPv1) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._dispatch(quote), self._loop)

    async def _dispatch(self, quote: sj.QuoteFOPv1) -> None:
        for cb in list(self._strategies.values()):
            try:
                await cb(quote)
            except Exception as e:
                logger.error("QuoteHub strategy dispatch error: %s", e)

        if not self._ws_queues:
            return

        close = float(quote.close)
        if self._ws_last_close.get(quote.code) == close:
            return
        self._ws_last_close[quote.code] = close

        ts_ns = getattr(quote, "ts", None)
        msg = json.dumps({
            "code": quote.code,
            "close": close,
            "open": float(quote.open),
            "high": float(quote.high),
            "low": float(quote.low),
            "volume": int(quote.volume),
            "total_volume": int(quote.total_volume),
            "change_price": float(getattr(quote, "change_price", 0) or 0),
            "ts": ts_ns / 1e9 if ts_ns else 0.0,
        })
        for q in list(self._ws_queues):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


quote_hub = QuoteHub()
