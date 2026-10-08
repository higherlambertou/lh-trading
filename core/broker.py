"""WorkerBridge: routes all shioaji calls to shioaji_worker.py subprocess.
shioaji 的 C/Rust 層全部關在子進程；主進程的 asyncio event loop 永遠只碰純 Python dict。
"""
import asyncio
import itertools
import logging
import multiprocessing
import threading
import time
from typing import Any, Callable, Optional

from core.trade_log import trade_log

logger = logging.getLogger(__name__)


class BrokerClient:
    _instance: Optional["BrokerClient"] = None

    def __new__(cls) -> "BrokerClient":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._proc: multiprocessing.Process | None = None
        self._cmd_q: Any = None
        self._event_q: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._is_connected = False
        self._pending: dict[str, asyncio.Future] = {}
        self._req_counter = itertools.count(1)
        self._req_lock = threading.Lock()
        self._order_callback: Callable | None = None
        self._reader_thread: threading.Thread | None = None
        self._known_subs: list[str] = []       # futures prefixes，重連後重新訂閱
        self._known_opt_subs: list[dict] = []  # option params，重連後重新訂閱

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def login(self) -> None:
        """啟動 shioaji_worker 子進程。"""
        if self._proc and self._proc.is_alive():
            return
        from core.shioaji_worker import run_worker
        ctx = multiprocessing.get_context("spawn")
        self._cmd_q = ctx.Queue()
        self._event_q = ctx.Queue()
        self._proc = ctx.Process(
            target=run_worker,
            args=(self._cmd_q, self._event_q),
            daemon=True,
        )
        self._proc.start()
        logger.info("Shioaji worker 已啟動 (PID=%d)", self._proc.pid)
        self._reader_thread = threading.Thread(
            target=self._event_reader, daemon=True, name="broker-event-reader"
        )
        self._reader_thread.start()

    def _event_reader(self) -> None:
        """後台執行緒：從 event_q 讀事件，派給 event loop。"""
        while True:
            try:
                msg = self._event_q.get(timeout=0.5)
            except Exception:
                if self._proc and not self._proc.is_alive():
                    if self._is_connected:
                        self._is_connected = False
                        logger.error("Shioaji worker 意外退出，等待 watchdog 重啟")
                continue

            try:
                self._dispatch(msg)
            except Exception:
                logger.exception("_event_reader 處理事件時例外，繼續執行")

    def _dispatch(self, msg: dict) -> None:
        mtype = msg.get("type")

        if mtype == "connected":
            self._is_connected = True
            logger.info("Worker 已連線，重新訂閱 %d 個期貨 / %d 個選擇權",
                        len(self._known_subs), len(self._known_opt_subs))
            for code in self._known_subs:
                try:
                    self._cmd_q.put_nowait({"method": "subscribe", "contract_code": code})
                except Exception:
                    pass
            for opt in self._known_opt_subs:
                try:
                    self._cmd_q.put_nowait({"method": "subscribe_option", **opt})
                except Exception:
                    pass

        elif mtype == "error":
            logger.error("Worker 錯誤: %s", msg.get("msg"))

        elif mtype == "quote":
            if self._loop and self._loop.is_running():
                from core.quote_hub import quote_hub
                self._loop.call_soon_threadsafe(
                    quote_hub._inject_quote, msg["data"]
                )

        elif mtype == "order_event":
            try:
                trade_log.record_event(msg)  # 成交紀錄：不阻塞；任何問題都不能影響下面的回報派發給策略
            except Exception:
                logger.exception("成交紀錄失敗（已忽略，不影響成交回報派發）")
            if self._order_callback and self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    self._order_callback, msg
                )

        elif mtype == "response":
            req_id = msg.get("req_id")
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done() and self._loop:
                if "error" in msg:
                    self._loop.call_soon_threadsafe(
                        fut.set_exception, RuntimeError(msg["error"])
                    )
                else:
                    self._loop.call_soon_threadsafe(
                        fut.set_result, msg.get("result")
                    )

    # ── 內部 async 呼叫 ───────────────────────────────────────────────

    async def _acall(self, method: str, timeout: float = 8.0, **kwargs) -> Any:
        if not self._is_connected:
            raise RuntimeError("Worker 尚未連線，請稍後再試")
        loop = asyncio.get_running_loop()
        with self._req_lock:
            req_id = str(next(self._req_counter))
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            self._cmd_q.put_nowait({"method": method, "req_id": req_id, **kwargs})
        except Exception as e:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"無法傳送指令給 worker: {e}") from e
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    # ── 公開 API ──────────────────────────────────────────────────────

    def set_order_callback(self, cb: Callable) -> None:
        """策略登記 order callback（委託/成交回報）。"""
        self._order_callback = cb

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    # 期貨訂閱

    async def subscribe(self, contract_code: str) -> None:
        code = contract_code.upper()
        if code not in self._known_subs:
            self._known_subs.append(code)
        await self._acall("subscribe", contract_code=code)

    def subscribe_sync(self, contract_code: str) -> None:
        """Fire-and-forget 版（同步，不阻塞），供非 async context 呼叫。"""
        code = contract_code.upper()
        if code not in self._known_subs:
            self._known_subs.append(code)
        if self._is_connected and self._cmd_q:
            try:
                self._cmd_q.put_nowait({"method": "subscribe", "contract_code": code})
            except Exception:
                pass

    # 選擇權訂閱

    async def subscribe_option(
        self, delivery_month: str, strike: int, right: str, category: str = "TXO"
    ) -> None:
        params = {"delivery_month": delivery_month, "strike": strike,
                  "right": right, "category": category}
        if params not in self._known_opt_subs:
            self._known_opt_subs.append(params)
        await self._acall("subscribe_option", **params)

    def subscribe_option_sync(
        self, delivery_month: str, strike: int, right: str, category: str = "TXO"
    ) -> None:
        """Fire-and-forget 版，供非 async context 呼叫。"""
        params = {"delivery_month": delivery_month, "strike": strike,
                  "right": right, "category": category}
        if params not in self._known_opt_subs:
            self._known_opt_subs.append(params)
        if self._is_connected and self._cmd_q:
            try:
                self._cmd_q.put_nowait({"method": "subscribe_option", **params})
            except Exception:
                pass

    # 帳務查詢

    async def list_trades(self) -> list[dict]:
        return await self._acall("list_trades")

    async def list_trades_with_status(self) -> list[dict]:
        """update_status + list_trades 合一，原子執行於 worker。"""
        return await self._acall("list_trades_with_status")

    async def list_positions(self) -> list[dict]:
        return await self._acall("list_positions")

    async def list_profit_loss(self) -> list[dict]:
        """當日已實現損益。帳務查詢較慢，worker 逾時 4 秒；呼叫端請低頻使用。"""
        return await self._acall("list_profit_loss", timeout=8.0, timeout_ms=4000)

    async def cancel_order(self, trade_id: str) -> None:
        await self._acall("cancel_order", trade_id=trade_id)

    async def place_order(
        self,
        contract_code: str,
        action: str,
        quantity: int,
        price: float = 0,
        price_type: str = "MKT",
        order_type: str = "IOC",
        octype: str = "Auto",
    ) -> dict:
        t0 = time.monotonic()
        code = contract_code.upper()
        try:
            res = await self._acall(
                "place_order",
                contract_code=code,
                action=action,
                quantity=quantity,
                price=price,
                price_type=price_type,
                order_type=order_type,
                octype=octype,
            )
        except Exception as e:
            self._log_order(code, action, quantity, price_type, order_type, price, octype, None, t0, e)
            raise
        self._log_order(code, action, quantity, price_type, order_type, price, octype, res, t0, None)
        return res

    @staticmethod
    def _log_order(contract: str, action: str, qty: int, price_type: str, order_type: str,
                   price: float, octype: str, res: Any, t0: float, err: BaseException | None) -> None:
        """成交紀錄：委託送出結果（成功帶 trade_id；失敗/逾時也記，逾時的單可能仍已送達券商）。
        這裡在「下單已成功」之後才執行，所以無論如何都不能丟例外（否則呼叫端會以為下單失敗）。"""
        try:
            ok = isinstance(res, dict)
            trade_log.record_order(
                contract=contract, action=action, qty=qty, price_type=price_type, order_type=order_type,
                limit_price=price, octype=octype,
                trade_id=res.get("trade_id", "") if ok else "",
                status=str(res.get("status", "")) if ok else ("timeout" if isinstance(err, asyncio.TimeoutError) else "error"),
                error=repr(err) if err else "", latency_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception:
            logger.exception("成交紀錄失敗（已忽略，不影響下單）")

    async def place_option_order(
        self,
        delivery_month: str,
        strike: int,
        right: str,
        category: str,
        action: str,
        quantity: int,
        price: float,
        order_type: str = "ROD",
    ) -> dict:
        t0 = time.monotonic()
        code = f"{category}{delivery_month}{strike}{right}"
        try:
            res = await self._acall(
                "place_option_order",
                delivery_month=delivery_month, strike=strike,
                right=right, category=category,
                action=action, quantity=quantity,
                price=price, order_type=order_type,
            )
        except Exception as e:
            self._log_order(code, action, quantity, "LMT", order_type, price, "Auto", None, t0, e)
            raise
        self._log_order(str(res.get("code") or code) if isinstance(res, dict) else code,
                        action, quantity, "LMT", order_type, price, "Auto", res, t0, None)
        return res

    async def margin(self) -> dict:
        return await self._acall("margin", timeout=5.0)

    async def usage(self) -> dict:
        return await self._acall("usage", timeout=5.0)

    async def snapshots(self, contract_codes: list[str]) -> list[dict]:
        return await self._acall("snapshots", contracts=contract_codes)

    async def kbars(self, contract_code: str, start: str, end: str) -> dict:
        """1 分 K（ts/open/high/low/close/volume 平行陣列），start/end 為 YYYY-MM-DD。
        worker 單執行緒：查詢期間下單指令會排隊，只在盤前/盤後呼叫，且單次區間別太長。"""
        return await self._acall(
            "kbars", timeout=60.0,
            contract_code=contract_code.upper(), start=start, end=end, timeout_ms=45000,
        )

    # 選擇權查詢

    async def option_snapshot(
        self, delivery_month: str, strike: int, right: str, category: str = "TXO"
    ) -> dict:
        return await self._acall(
            "option_snapshot",
            delivery_month=delivery_month, strike=strike,
            right=right, category=category,
        )

    async def option_expiries(self, category: str = "TXO") -> list[str]:
        return await self._acall("option_expiries", category=category)

    async def option_strikes(
        self, delivery_month: str, right: str, category: str = "TXO"
    ) -> list[int]:
        return await self._acall(
            "option_strikes",
            delivery_month=delivery_month, right=right, category=category,
        )

    def logout(self) -> None:
        """關閉 worker 子進程。"""
        if self._proc:
            try:
                self._cmd_q.put_nowait({"type": "shutdown"})
                self._proc.join(timeout=5)
            except Exception:
                pass
            if self._proc and self._proc.is_alive():
                self._proc.kill()
            self._proc = None
        self._is_connected = False
        logger.info("WorkerBridge 已關閉")


broker = BrokerClient()
