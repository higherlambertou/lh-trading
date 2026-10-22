"""重複錯誤的 log 節流（update.md 發現 65）。

同一個 key（例如 worker 的查詢名稱）、同一種錯誤：第一次記完整內容（traceback），之後每 WINDOW 秒只記一行「又發生 n 次」，
恢復（同一個 key 成功一次）時記一行總結。錯誤換了一種（訊息不同）就當成新的、再記一次完整內容。
2026-10-15～22 的 /tmp/lh_live.log 有 6,000 多段完整 traceback（週末券商維護時每幾秒一段），log 長到 80 MB，真正的問題反而被淹沒。"""
from __future__ import annotations

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
