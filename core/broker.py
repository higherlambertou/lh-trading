import os
import logging
from typing import Optional, TypeVar, Callable
from functools import wraps

import shioaji as sj
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)

_SESSION_ERROR_KEYWORDS = ("SessionNotEstablished", "NotReady", "Session error")


def _is_session_error(e: Exception) -> bool:
    msg = str(e)
    return any(k in msg for k in _SESSION_ERROR_KEYWORDS)


class BrokerClient:
    """Shioaji 連線 singleton，整個 process 共用同一個 api 物件"""

    _instance: Optional["BrokerClient"] = None
    _api: Optional[sj.Shioaji] = None

    def __new__(cls) -> "BrokerClient":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def reconnect(self) -> sj.Shioaji:
        """強制重新登入（session 斷線時使用）"""
        logger.warning("Shioaji session 斷線，嘗試重新連線...")
        try:
            if self._api:
                self._api.logout()
        except Exception:
            pass
        self._api = None
        return self.login()

    def call(self, fn: Callable, *args, **kwargs):
        """呼叫 Shioaji API，遇到 session 斷線時自動重連後重試一次"""
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if _is_session_error(e):
                self.reconnect()
                return fn(*args, **kwargs)
            raise

    def login(self) -> sj.Shioaji:
        if self._api is not None:
            return self._api

        simulation = os.getenv("SIMULATION", "true").lower() == "true"
        api = sj.Shioaji(simulation=simulation)

        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("缺少 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY 環境變數")

        api.login(api_key=api_key, secret_key=secret_key)
        logger.info("Shioaji 登入成功 (simulation=%s)", simulation)

        ca_path = os.getenv("CA_PATH")
        ca_password = os.getenv("CA_PASSWORD")
        person_id = os.getenv("PERSON_ID")
        if ca_path and ca_password and person_id:
            try:
                api.activate_ca(
                    ca_path=ca_path,
                    ca_passwd=ca_password,
                    person_id=person_id,
                )
                logger.info("CA 憑證啟用成功")
            except Exception as e:
                logger.warning("CA 憑證啟用失敗（僅查報價可用）: %s", e)

        self._api = api
        return api

    @property
    def api(self) -> sj.Shioaji:
        if self._api is None:
            raise RuntimeError("尚未登入，請先呼叫 login()")
        return self._api

    @property
    def is_connected(self) -> bool:
        return self._api is not None

    def txf_contract(self) -> sj.contracts.Future:
        """台指近月連續合約 TXFR1"""
        return self._api.Contracts.Futures.TXF.TXFR1

    def mxf_contract(self) -> sj.contracts.Future:
        """小台近月連續合約 MXFR1（每點 50 元）"""
        return self._api.Contracts.Futures.MXF.MXFR1

    def tmf_contract(self) -> sj.contracts.Future:
        """微台近月連續合約 TMFR1（每點 10 元）"""
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
