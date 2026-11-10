"""每天換證重啟（update.md 發現 80）：讓 shioaji 登入 token 永遠在沒有交易的空檔到期。

為什麼：永豐 shioaji 的登入 token 只有 24 小時。到期時 SDK 主動斷線（shioaji.log「Received 401, proactively disconnecting Solace session」）、
不會自己重登，之後所有查詢回 Session error、報價停止，要等連線假死偵測（core/session_watch.py，發現 65）在交易時段發現才重啟——
每個交易日開盤後 4～6 分鐘沒有報價、停損監控失效，而且因為每次都是「上一次重啟 + 24 小時」，到期時間會一直黏在開盤附近。

做法：在空手的空檔主動要求 watchdog 重啟（重啟＝重新登入＝新 token），走換月那條路徑（/api/health 帶 restart_wanted=relogin，
run_live.sh／run_sim.sh 正常關閉後重啟；有計畫的重啟不算短命、不退避）。兩個空檔：
  - 下午 14:00～14:50（日盤 13:45 收、夜盤 15:00 開）：只要 token 會在「明天 13:50 前」到期就重啟。正常情況每天一次（含週末與假日），
    token 永遠在 14:00 左右換新，到期時間落在隔天的同一個空檔裡。
  - 早上 08:00～08:30（夜盤 05:00 收、日盤 08:45 開）：只有 token 會在「今天日盤收盤（13:45）前」到期才重啟。補救用——
    前一天下午那次因為有持倉被跳過、或手動重啟落在上午之類。
空手＝券商沒有部位、沒有未完成的委託、沒有手動停損停利監控、沒有策略在跑；部位快取不是最新的也算不是空手（寧可讓假死偵測處理，
也不帶倉重啟）。只讀記憶體裡的快取，不向券商發查詢。RELOGIN_RESTART=false 關閉。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Optional

from core.daytrade import TAIPEI

logger = logging.getLogger(__name__)

POLL_SEC = 30.0
TOKEN_TTL = 24 * 3600.0                                    # 登入 token 的壽命（實測 24 小時 ± 35 秒）
AFTERNOON = (dtime(14, 0), dtime(14, 50))                  # 日夜盤之間的空檔（目標時間, 最晚）
MORNING = (dtime(8, 0), dtime(8, 30))                      # 夜盤收盤後、日盤開盤前
NEXT_AFTERNOON_START = dtime(13, 50)                       # 下午那個空檔的開始：token 撐不到明天這個時間就要換
DAY_CLOSE = dtime(13, 45)
MAX_POSITIONS_AGE = 900.0                                  # 部位快取超過這麼舊就不敢說是空手（週末的刷新間隔是 120 秒）

_wanted: Optional[str] = None


def enabled() -> bool:
    return os.getenv("RELOGIN_RESTART", "true").lower() != "false"


def wanted() -> Optional[str]:
    """health 用：要 watchdog 重啟時回 "relogin"，否則 None。只讀記憶體。"""
    return _wanted


def expiry(login_at: float) -> datetime:
    return datetime.fromtimestamp(login_at + TOKEN_TTL, TAIPEI)


def _at(d: date, t: dtime) -> datetime:
    return datetime.combine(d, t, tzinfo=TAIPEI)


def decide(now: datetime, login_at: Optional[float]) -> Optional[str]:
    """現在這個時間點該不該換證（還沒看空手）：回原因字串，不該就 None。now 要帶台北時區；login_at 是這個 worker 登入的 epoch 秒。"""
    if login_at is None:
        return None
    exp = expiry(login_at)
    t = now.timetz().replace(tzinfo=None)
    if AFTERNOON[0] <= t <= AFTERNOON[1]:
        limit = _at(now.date() + timedelta(days=1), NEXT_AFTERNOON_START)
        if exp < limit:
            return f"登入 token 會在 {exp:%m/%d %H:%M} 到期（撐不到明天的日夜盤空檔），趁現在空檔換證"
    elif MORNING[0] <= t <= MORNING[1]:
        limit = _at(now.date(), DAY_CLOSE)
        if exp < limit:
            return f"登入 token 會在 {exp:%m/%d %H:%M} 到期（今天日盤裡），趁開盤前換證"
    return None


def busy_reason(positions: Optional[list[dict[str, Any]]], positions_age: Optional[float], unfinished: bool,
                watches: Optional[list[Any]], strategy_running: bool) -> Optional[str]:
    """為什麼現在不是空手（不能重啟）；None＝空手。部位資料不是最新的也算不是空手。"""
    if positions is None or positions_age is None or positions_age < 0 or positions_age > MAX_POSITIONS_AGE:
        return "部位快取不是最新的" + (f"（{positions_age:.0f} 秒前）" if positions_age is not None and positions_age >= 0 else "（還沒拿到過）")
    held = [p for p in positions if int(p.get("quantity", 0) or 0) != 0]
    if held:
        return f"有部位（{len(held)} 筆）"
    if unfinished:
        return "有未完成的委託"
    if watches:
        return f"有手動停損停利監控（{len(watches)} 個）"
    if strategy_running:
        return "有策略在跑"
    return None


async def relogin_loop(get_login_at: Callable[[], Optional[float]], get_busy: Callable[[], Optional[str]]) -> None:
    """背景：每 30 秒看一次。get_login_at＝這個 worker 的登入時間（session_watch.connected_at）；get_busy＝空手檢查（讀快取）。例外全部吞掉。"""
    global _wanted
    if not enabled():
        logger.info("換證重啟已關閉（RELOGIN_RESTART=false）")
        return
    last_busy: Optional[str] = None
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            reason = decide(datetime.now(TAIPEI), get_login_at())
            if reason is None:
                if _wanted:                                             # 空檔過了還在跑：watchdog 沒接手（沒用 watchdog 啟動？），收回要求，免得之後在盤中被重啟
                    logger.warning("換證：空檔已過、後端還沒被重啟（沒有 watchdog？），收回重啟要求")
                    _wanted = None
                last_busy = None
                continue
            if _wanted:
                continue
            busy = get_busy()
            if busy:
                if busy != last_busy:
                    logger.info("換證重啟延後：%s（%s）", busy, reason)
                    last_busy = busy
                continue
            _wanted = "relogin"
            logger.warning("換證：%s，要求 watchdog 重啟", reason)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("換證檢查例外（已忽略）")
