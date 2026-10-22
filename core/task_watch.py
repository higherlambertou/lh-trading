"""背景工作的健康（update.md 發現 69）：main.py 建的背景工作登記在這裡，系統健康面板看得到有沒有哪個停了。

一個背景工作結束只有三種：被取消（關機）、正常結束（一次性的開機工作做完了，或設定關閉、模擬盤不跑而直接 return）、
丟例外（意外停止——之後就再也不會動，直到後端重啟）。以前第三種完全沒人知道；現在記一行 ERROR、推播一次（冷卻 1 小時），面板顯示紅燈。
只看、不重啟任何工作，也不影響交易路徑（done callback 永遠不丟例外）。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}
ONE_SHOT = {"開機訂閱", "重啟後的持倉保護"}          # 做完就結束的工作：正常結束不是問題


def register(name: str, task: Optional[asyncio.Task]) -> Optional[asyncio.Task]:
    if task is None:
        return None
    _tasks[name] = task
    task.add_done_callback(lambda t, n=name: _on_done(n, t))
    return task


def _on_done(name: str, task: asyncio.Task) -> None:
    try:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error("背景工作「%s」意外停止（重啟後端前不會再動）：%r", name, exc, exc_info=exc)
        from core.notify import notify
        notify(f"⚠️ 後端的背景工作停了：{name}", f"錯誤：{type(exc).__name__}: {str(exc)[:200]}。這個工作在後端重啟前都不會再動；"
               "細節在 app log，〈分析〉頁的〈系統健康〉也看得到。", key=f"taskdead:{name}", cooldown=3600.0)
    except Exception:  # noqa: BLE001
        pass


def status() -> list[dict[str, Any]]:
    out = []
    for name, t in _tasks.items():
        if not t.done():
            state, err = "running", None
        elif t.cancelled():
            state, err = "cancelled", None
        elif t.exception() is not None:
            state, err = "crashed", f"{type(t.exception()).__name__}: {str(t.exception())[:120]}"
        else:
            state, err = ("done" if name in ONE_SHOT else "off"), None
        out.append({"name": name, "state": state, "error": err})
    return out


def clear() -> None:
    """測試用。"""
    _tasks.clear()
