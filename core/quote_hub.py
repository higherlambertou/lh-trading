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
        self._quote_seen = False
        self._subscribed_contracts: set[str] = set()   # actual month codes, e.g. TMFF6
        self._known_contract_types: set[str] = set()   # type prefixes, e.g. TMF, TXF
        self._ws_last_close: dict[str, float] = {}
        self._last_price: dict[str, float] = {}        # code → 最新成交價（含選擇權權利金）

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

        # 盤後/剛啟動沒有即時 tick，推播快取會是空的 → 前端看不到台指現價。
        # 用一次 snapshot 種「最後成交價」當底價（跟選擇權同機制）；盤中會被推播覆蓋。
        # best-effort：snapshot 失敗（Solace 卡/超限）不影響訂閱本身。
        try:
            snaps = broker.api.snapshots([contract])
            if snaps:
                px = float(getattr(snaps[0], "close", 0) or 0)
                if px > 0:
                    self._last_price[code] = px
                    logger.info("QuoteHub 種底價 %s = %s（snapshot）", code, px)
        except Exception as e:
            logger.warning("QuoteHub 種底價 snapshot 失敗 %s: %s", code, e)

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
        if not getattr(self, "_quote_seen", False):
            self._quote_seen = True
            logger.info(
                "QuoteHub 收到首筆報價 code=%s close=%s → 開始派發給 %d 個策略",
                getattr(quote, "code", "?"), getattr(quote, "close", "?"),
                len(self._strategies),
            )
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._dispatch(quote), self._loop)

    async def _dispatch(self, quote: sj.QuoteFOPv1) -> None:
        # 更新最新價快取（含選擇權權利金），供手動監控讀取
        try:
            self._last_price[quote.code] = float(quote.close)
        except (TypeError, ValueError):
            pass

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
