import asyncio
import os
import logging
import threading
import time
from typing import Any, Optional, Callable, TypeVar

import shioaji as sj
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SESSION_ERROR_KEYWORDS = ("SessionNotEstablished", "NotReady", "Session error")


def _is_session_error(e: Exception) -> bool:
    return any(k in str(e) for k in _SESSION_ERROR_KEYWORDS)


def _login_with_hard_timeout(
    api: sj.Shioaji,
    api_key: str,
    secret_key: str,
    contracts_timeout: int,
    hard_timeout: float,
) -> None:
    """在 daemon thread 執行 api.login()，超過 hard_timeout 秒未回來就丟 TimeoutError。

    Solace session 中途死亡時，SDK 會卡在死掉的 socket 上、既不回傳也不丟例外，
    而 contracts_timeout 只管「抓合約」那段、管不到前面的握手。這層硬逾時確保
    啟動不會無限懸著：逾時就放棄這條連線（卡死的 thread 是 daemon，隨 process 結束），
    由外層重試迴圈換一個全新的 sj.Shioaji 重連。
    """
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            api.login(
                api_key=api_key,
                secret_key=secret_key,
                contracts_timeout=contracts_timeout,
            )
            box["ok"] = True
        except Exception as e:  # noqa: BLE001 - 原樣帶回主執行緒處理
            box["err"] = e

    t = threading.Thread(target=_worker, name="shioaji-login", daemon=True)
    t.start()
    t.join(hard_timeout)
    if t.is_alive():
        raise TimeoutError(
            f"登入逾時：{hard_timeout:.0f}s 內未完成（疑似 Solace session 卡死）"
        )
    if "err" in box:
        raise box["err"]


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

        api_key    = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("缺少 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY 環境變數")

        # 登入會連永豐 Solace 行情主機，網路間歇不穩時會 timeout（ShioajiConnectionError），
        # 更糟的是 session 中途死亡時 SDK 會卡死在 socket 上不回不錯。
        # 重試數次避免一次連線失敗就讓整個 startup 掛掉。
        # contracts_timeout：最多等 15s 抓合約；LOGIN_TIMEOUT：整個 login 的硬逾時。
        max_attempts  = int(os.getenv("LOGIN_RETRIES", "3"))
        hard_timeout  = float(os.getenv("LOGIN_TIMEOUT", "25"))
        api = None
        last_err: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            api = sj.Shioaji(simulation=simulation)
            try:
                logger.info(
                    "Shioaji 登入中…(第 %d/%d 次, simulation=%s, 逾時 %.0fs)",
                    attempt, max_attempts, simulation, hard_timeout,
                )
                _login_with_hard_timeout(
                    api, api_key, secret_key,
                    contracts_timeout=15000,
                    hard_timeout=hard_timeout,
                )
                logger.info("Shioaji 登入成功")
                last_err = None
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    "Shioaji 登入失敗(第 %d/%d 次): %s", attempt, max_attempts, e
                )
                # 一般錯誤才嘗試登出回收；硬逾時時 session 已卡死，logout 也會跟著卡 → 直接丟棄
                if not isinstance(e, TimeoutError):
                    try:
                        api.logout()
                    except Exception:
                        pass
                api = None
                if attempt < max_attempts:
                    time.sleep(3)
        if api is None:
            raise RuntimeError(
                f"Shioaji 登入連續 {max_attempts} 次失敗（行情主機連線逾時？）: {last_err}"
            )

        ca_path    = os.getenv("CA_PATH")
        ca_password = os.getenv("CA_PASSWORD")
        person_id  = os.getenv("PERSON_ID")
        if ca_path and ca_password and person_id:
            try:
                logger.info("CA 憑證啟用中…")
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
