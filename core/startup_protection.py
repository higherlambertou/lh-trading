"""後端（重新）啟動時的持倉保護（update.md 發現 18、32）：恢復手動停損監控、報告哪些持倉沒有保護（推播）。

watchdog 在凍結後重啟後端是常態；重啟那一刻持倉的停損停利本來全部消失。現在：
  1. 落地的手動停損監控和券商「現在」的部位對帳、恢復（core/manual_monitor.py 的 restore／reconcile）；
  2. 有持倉就推播一則報告——恢復了什麼（含停損價），或哪些持倉沒有任何保護（策略不會自動重啟，請到儀表板處理）；
  3. 券商一直連不上就推播「停損監控還沒恢復」。
沒有持倉就不打擾。整個過程不下任何單，也不丟例外。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Optional

from core import protection
from core.manual_monitor import manual_monitor
from core.notify import notify

logger = logging.getLogger(__name__)

REPEAT_COOLDOWN = 600.0     # 同樣內容的報告 10 分鐘內只推一次（連續重啟時不洗版；內容變了就會再推）


async def protect_after_start(positions: Optional[list[dict[str, Any]]] = None, *, wait_sec: float = 600.0) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "error"}
    try:
        report = await manual_monitor.restore(positions, wait_sec=wait_sec)
        held = report["positions"]
        unprot = protection.unprotected(held, manual_monitor.list_watches(), strategy_running=False)   # 啟動當下沒有策略在跑
        report["unprotected"] = unprot
        if report["status"] == "broker_unavailable":
            msg: Optional[tuple[str, str]] = ("⚠️ 後端已重啟，但連不上券商：停損監控還沒恢復",
                                              f"落地的 {report.get('saved', 0)} 個手動停損監控還沒有恢復（等不到券商的持倉資料）。"
                                              "如果你有持倉，請先到券商 App 確認，必要時手動平倉。")
        else:
            msg = protection.startup_message(held, report["restored"], unprot, report["status"])
        if msg:
            digest = hashlib.sha1((msg[0] + msg[1]).encode("utf-8")).hexdigest()[:10]
            notify(msg[0], msg[1], key=f"startup:{digest}", cooldown=REPEAT_COOLDOWN, priority="urgent" if unprot or report["status"] == "broker_unavailable" else "high")
        logger.info("啟動時的持倉保護：%s，恢復 %d 個停損監控、丟掉 %d 個、沒有保護的持倉 %d 筆",
                    report["status"], len(report["restored"]), len(report["dropped"]), len(unprot))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("啟動時的持倉保護檢查失敗（持倉可能沒有停損保護，請檢查儀表板）")
    return report
