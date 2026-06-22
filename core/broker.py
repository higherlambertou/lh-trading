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


def _run_with_hard_timeout(fn: Callable[[], Any], timeout: float, what: str) -> bool:
    """在 daemon thread 跑 fn()，逾時(秒)就放棄並回 False；正常完成回 True。

    用於 logout 這種「session 可能已死、呼叫會無限卡住」的 Solace API：逾時就
    丟著不等（daemon thread 隨 process 結束），確保呼叫端不會被卡死。
    """
    done = threading.Event()

    def _worker() -> None:
        try:
            fn()
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_worker, name=f"shioaji-{what}", daemon=True).start()
    if not done.wait(timeout):
        logger.warning("Shioaji %s 逾時 %.0fs，放棄等待", what, timeout)
        return False
    return True


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
        # 對「已死的 session」呼叫 logout 會無限卡住，必須加硬逾時，
        # 否則整個 reconnect（連帶呼叫它的 event loop）會被凍結。
        if self._api is not None:
            old = self._api
            _run_with_hard_timeout(lambda: old.logout(), 5.0, "logout")
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
        """async 版本：在 executor 執行 thunk()，同樣自動重連。

        重連也丟到 executor 跑：reconnect() 內含 logout/login 等阻塞呼叫，
        若直接在這裡（event loop 上）執行會凍結整個服務。
        """
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, thunk)
        except Exception as e:
            if _is_session_error(e):
                await loop.run_in_executor(None, self.reconnect)
                return await loop.run_in_executor(None, thunk)
            raise

    async def acall_to(self, thunk: Callable[[], T], timeout: float = 8.0) -> T:
        """acall + 硬逾時。

        給「會被前端高頻輪詢」的查詢端點用：Solace 卡死時，同步呼叫會無限懸著
        並佔住執行緒池，前端每 2~3 秒一打就把池佔光，連 /health 都拿不到執行緒
        → 整個服務假性凍結。加硬逾時後，卡住的呼叫到點即放棄（端點回 503），
        端點本身永遠快速返回，threadpool 不會被佔光、health 不會被拖垮。
        逾時會丟 asyncio.TimeoutError（在 3.11+ 即內建 TimeoutError）。
        """
        return await asyncio.wait_for(self.acall(thunk), timeout)

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

    # ── 選擇權合約 ─────────────────────────────────────────────────
    # 期權合約是「月份 × 履約價 × 買賣權」上千檔，沒有像期貨那樣寫死一檔，
    # 故提供解析器（依條件挑出單一合約）與下拉清單 helper（給前端選）。

    def _option_category(self, category: str = "TXO"):
        """取得選擇權類別（TXO=月選；TX1~TX5=週選）。"""
        opts = getattr(self._api.Contracts, "Options", None)
        if opts is None:
            raise RuntimeError("此帳號/連線取不到選擇權合約（Contracts.Options），請確認權限")
        cat = getattr(opts, category, None)
        if cat is None:
            raise ValueError(f"找不到選擇權類別: {category}")
        return cat

    def list_option_contracts(self, category: str = "TXO") -> list:
        return list(self._option_category(category))

    @staticmethod
    def _opt_right_str(c) -> str:
        """回傳大寫的買賣權字串，可能是 'CALL'/'PUT' 或 'C'/'P'。"""
        r = getattr(c, "option_right", "")
        return str(getattr(r, "value", r)).upper()

    def option_expiries(self, category: str = "TXO") -> list[str]:
        """可選到期月份（delivery_month），由近到遠。"""
        return sorted({str(c.delivery_month) for c in self.list_option_contracts(category)})

    def option_strikes(
        self, delivery_month: str, right: str, category: str = "TXO"
    ) -> list[int]:
        """某到期月份 + 買/賣權 的所有履約價。"""
        r = right.upper()[:1]
        out = set()
        for c in self.list_option_contracts(category):
            if str(c.delivery_month) != str(delivery_month):
                continue
            if not self._opt_right_str(c).startswith(r):
                continue
            try:
                out.add(int(float(c.strike_price)))
            except (TypeError, ValueError):
                pass
        return sorted(out)

    def option_contract(
        self, delivery_month: str, strike, right: str, category: str = "TXO"
    ):
        """依 月份 / 履約價 / 買賣權(C或P) 取得單一選擇權合約物件。"""
        r = right.upper()[:1]
        sk = int(float(strike))
        for c in self.list_option_contracts(category):
            if str(c.delivery_month) != str(delivery_month):
                continue
            try:
                if int(float(c.strike_price)) != sk:
                    continue
            except (TypeError, ValueError):
                continue
            if self._opt_right_str(c).startswith(r):
                return c
        raise ValueError(
            f"找不到選擇權合約: {category} {delivery_month} {strike} {right}"
        )

    def option_categories_debug(self) -> dict:
        """結構傾印 Contracts.Options：看它怎麼存類別（月選/各週選），
        以便寫出正確的列舉方式。一次性探查用。"""
        opts = getattr(self._api.Contracts, "Options", None)
        if opts is None:
            return {"error": "此連線取不到 Contracts.Options"}
        info: dict = {
            "type": type(opts).__name__,
            "dir": [n for n in dir(opts) if not n.startswith("_")],
        }
        # 直接 iterate opts 看 yield 出什麼（類別物件 or 個別合約?）
        sample = []
        try:
            for i, c in enumerate(opts):
                sample.append(
                    {
                        "py_type": type(c).__name__,
                        "name": str(
                            getattr(c, "code", None)
                            or getattr(c, "symbol", None)
                            or getattr(c, "name", None)
                            or c
                        )[:40],
                        "has_strike": hasattr(c, "strike_price"),
                        "iterable": hasattr(c, "__iter__"),
                    }
                )
                if i >= 9:
                    break
        except Exception as e:
            sample = [f"iter err: {e}"]
        info["iter_sample"] = sample
        try:
            info["keys"] = list(opts.keys())[:40]
        except Exception as e:
            info["keys_err"] = str(e)[:120]
        return info

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
