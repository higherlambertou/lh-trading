"""重複錯誤的 log 節流（update.md 發現 65）。

同一個 key（例如 worker 的查詢名稱）、同一種錯誤：第一次記完整內容（traceback），之後每 WINDOW 秒只記一行「又發生 n 次」，
恢復（同一個 key 成功一次）時記一行總結。錯誤換了一種（訊息不同）就當成新的、再記一次完整內容。
2026-10-15～22 的 /tmp/lh_live.log 有 6,000 多段完整 traceback（週末券商維護時每幾秒一段），log 長到 80 MB，真正的問題反而被淹沒。"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

WINDOW = 300.0


def signature(e: Any) -> str:
    """錯誤的『種類』：例外型別＋訊息（數字一律換成 #，請求編號、連線編號不同不算不同的錯誤），最多 160 字。"""
    name = type(e).__name__ if isinstance(e, BaseException) else ""
    return f"{name}: {re.sub(r'[0-9]+', '#', str(e))[:160]}"


class ErrorThrottle:
    def __init__(self, window: float = WINDOW) -> None:
        self.window = window
        self._s: dict[str, dict[str, Any]] = {}

    def error(self, key: str, sig: str, now: float) -> tuple[str, int]:
        """("full", 0)＝記完整內容；("summary", n)＝記一行彙總（上次記錄之後又發生 n 次）；("quiet", 0)＝這次不記。"""
        st = self._s.get(key)
        if st is None or st["sig"] != sig:
            self._s[key] = {"sig": sig, "first": now, "count": 1, "pending": 0, "last_log": now}
            return "full", 0
        st["count"] += 1
        st["pending"] += 1
        if now - st["last_log"] >= self.window:
            n, st["pending"], st["last_log"] = st["pending"], 0, now
            return "summary", n
        return "quiet", 0

    def success(self, key: str, now: float) -> Optional[tuple[int, float]]:
        """這個 key 之前在失敗：回傳（總共失敗幾次, 從第一次失敗到現在幾秒）並清掉；沒有在失敗回 None。"""
        st = self._s.pop(key, None)
        return None if st is None else (int(st["count"]), now - st["first"])


# ── uvicorn 的存取紀錄（update.md 發現 66）─────────────────────────

class QuietAccessFilter(logging.Filter):
    """成功的 GET／HEAD／OPTIONS（2xx、3xx）不記：畫面每 2～5 秒輪詢，2026-10-15～22 有 107 萬行、占 app log 的 96%，真正的錯誤被淹沒。
    錯誤回應（4xx、5xx）與 POST／DELETE／PUT（下單、改設定）照記。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:              # 不是 uvicorn 存取紀錄的格式：照記
            return True
        try:
            method, status = str(args[1]).upper(), int(args[4])
        except (TypeError, ValueError):
            return True
        return not (method in ("GET", "HEAD", "OPTIONS") and status < 400)


def install_access_filter(logger_name: str = "uvicorn.access") -> bool:
    """在 uvicorn 設定完 logging 之後呼叫（lifespan 開頭）；重複呼叫不會加兩次。ACCESS_LOG=all 全記。回傳有沒有裝上。"""
    if os.getenv("ACCESS_LOG", "").lower() == "all":
        return False
    lg = logging.getLogger(logger_name)
    if not any(isinstance(f, QuietAccessFilter) for f in lg.filters):
        lg.addFilter(QuietAccessFilter())
    return True
