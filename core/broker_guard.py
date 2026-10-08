"""券商連線數保護：登入「之前」先看正式盤後端目前有幾條連線，太多就不登入。

同一個身分（person_id）最多 5 條連線，超過的登入會被拒絕。正式盤、模擬盤各佔 1 條；每次重啟被 kill 的進程
還會留下一條「殘留連線」，要等券商端逾時（好幾分鐘）才釋放。

會另外登入的唯讀工具（hurst_study fetch、indicator_history flow-fetch）每次再佔 1 條——即使有 logout，券商端回收也有延遲，
連續跑幾次就疊起來（2026-10-08 實際發生：3 → 最高 5 → 幾分鐘後回到 3）。高峰時如果正式盤剛好重啟，第 6 條就登不進去。
所以：登入前用 GET /api/position/usage（後端背景每 120 秒刷新，不用登入）問目前幾條，登入後最多 MAX_AFTER_LOGIN 條，
留一條給正式盤重啟。後端沒開（連不上）就放行——沒有別的連線在搶。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable

LIMIT = 5                 # 券商規定：同一個身分最多 5 條
MAX_AFTER_LOGIN = 4       # 我們這條登入之後最多 4 條，留 1 條給正式盤重啟


class BackendNotReady(Exception):
    """後端有開，但連線數還沒就緒（剛啟動、背景還沒查過）。"""


def usage_url(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if env.get("USAGE_URL"):
        return env["USAGE_URL"]
    host = env.get("BIND_HOST") or "localhost"
    if host == "0.0.0.0":
        host = "localhost"
    return f"http://{host}:{env.get('PORT') or '8002'}/api/position/usage"


def peek_connections(url: str | None = None, timeout: float = 3.0) -> int | None:
    """後端回報的券商連線數；後端連不上回 None；後端有開但還沒有資料（HTTP 503）丟 BackendNotReady。"""
    if url is None:
        if not os.getenv("USAGE_URL"):
            from dotenv import load_dotenv
            load_dotenv()                  # BIND_HOST／PORT 在 .env（只進程式的環境變數，不會印出來）
        url = usage_url()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return int(json.load(r)["connections"])
    except urllib.error.HTTPError as e:
        raise BackendNotReady(f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def room_for_login(max_after: int = MAX_AFTER_LOGIN, peek: Callable[[], int | None] = peek_connections) -> tuple[bool, str]:
    """(可以登入嗎, 說明)。說明直接給人看。"""
    try:
        n = peek()
    except BackendNotReady:
        return False, "後端剛啟動、券商連線數還沒就緒（重啟後連線最多），等一兩分鐘再試"
    if n is None:
        return True, "正式盤後端沒有回應，無法預先檢查連線數（沒開就沒有別的連線在搶）"
    if n + 1 > max_after:
        return False, (f"正式盤後端回報目前有 {n} 條券商連線，再登入一條會到 {n + 1} 條"
                       f"（上限 {LIMIT}，保留 {LIMIT - max_after} 條給正式盤重啟）。殘留連線要幾分鐘才會釋放，稍後再試")
    return True, f"目前 {n} 條券商連線，登入後 {n + 1} 條"
