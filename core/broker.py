import asyncio
import os
import logging
from typing import Optional, Callable, TypeVar

import shioaji as sj
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SESSION_ERROR_KEYWORDS = ("SessionNotEstablished", "NotReady", "Session error")


def _is_session_error(e: Exception) -> bool:
    return any(k in str(e) for k in _SESSION_ERROR_KEYWORDS)


class BrokerClient:
    """Shioaji 連線 singleton，整個 process 共用同一個 api 物件"""

    _instance: Optional["BrokerClient"] = None
    _api: Optional[sj.Shioaji] = None

    def __new__(cls) -> "BrokerClient":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # only initialise once
        if not hasattr(self, "_order_callback"):
            self._order_callback: Optional[Callable] = None

    # ── connection management ────────────────────────────────────────

    def login(self) -> sj.Shioaji:
        if self._api is not None:
            return self._api

        simulation = os.getenv("SIMULATION", "true").lower() == "true"
        api = sj.Shioaji(simulation=simulation)

        api_key    = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("缺少 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY 環境變數")

        api.login(api_key=api_key, secret_key=secret_key)
        logger.info("Shioaji 登入成功 (simulation=%s)", simulation)

        ca_path    = os.getenv("CA_PATH")
        ca_password = os.getenv("CA_PASSWORD")
        person_id  = os.getenv("PERSON_ID")
        if ca_path and ca_password and person_id:
            try:
                api.activate_ca(ca_path=ca_path, ca_passwd=ca_password, person_id=person_id)
                logger.info("CA 憑證啟用成功")
            except Exception as e:
                logger.warning("CA 憑證啟用失敗（僅查報價可用）: %s", e)

        self._api = api
        return api

    def reconnect(self) -> sj.Shioaji:
        """強制重新登入，reconnect 後重新掛上 callback 和報價訂閱。"""
        logger.warning("Shioaji session 斷線，嘗試重新連線...")
        try:
            if self._api:
                self._api.logout()
        except Exception:
            pass
        self._api = None
        api = self.login()

        # 重新掛 order callback
        if self._order_callback:
            try:
                api.set_order_callback(self._order_callback)
            except Exception as e:
                logger.warning("重連後設定 order callback 失敗: %s", e)

        # 通知 quote_hub 重新掛 quote callback 並重新訂閱合約
        try:
            from core.quote_hub import quote_hub
            quote_hub.reinstall_after_reconnect()
        except Exception as e:
            logger.warning("重連後重設 QuoteHub 失敗: %s", e)

        logger.info("Shioaji 重新連線成功")
        return api

    # ── safe call wrappers ────────────────────────────────────────────

    def call(self, thunk: Callable[[], T]) -> T:
        """
        執行 thunk()，遇到 session 斷線時自動重連後重試一次。
        thunk 必須是 no-arg callable（lambda），確保 retry 時使用最新的 api 物件。

        用法：broker.call(lambda: broker.api.list_positions(broker.api.futopt_account))
        """
        try:
            return thunk()
        except Exception as e:
            if _is_session_error(e):
                self.reconnect()
                return thunk()   # thunk 內的 broker.api 重新 evaluate → 新物件
            raise

    async def acall(self, thunk: Callable[[], T]) -> T:
        """async 版本：在 executor 執行 thunk()，同樣自動重連。"""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, thunk)
        except Exception as e:
            if _is_session_error(e):
                self.reconnect()
                return await loop.run_in_executor(None, thunk)
            raise

    # ── order callback ────────────────────────────────────────────────

    def set_order_callback(self, callback: Callable) -> None:
        """設定 order callback 並記住，reconnect 後自動重新設定。"""
        self._order_callback = callback
        self._api.set_order_callback(callback)

    # ── properties / helpers ──────────────────────────────────────────

    @property
    def api(self) -> sj.Shioaji:
        if self._api is None:
            raise RuntimeError("尚未登入，請先呼叫 login()")
        return self._api

    @property
    def is_connected(self) -> bool:
        return self._api is not None

    def txf_contract(self) -> sj.contracts.Future:
        return self._api.Contracts.Futures.TXF.TXFR1

    def mxf_contract(self) -> sj.contracts.Future:
        return self._api.Contracts.Futures.MXF.MXFR1

    def tmf_contract(self) -> sj.contracts.Future:
        return self._api.Contracts.Futures.TMF.TMFR1

    def logout(self) -> None:
        if self._api:
            try:
                self._api.logout()
            except Exception as e:
                logger.warning("登出時發生錯誤: %s", e)
            finally:
                self._api = None
            logger.info("Shioaji 已登出")


broker = BrokerClient()
