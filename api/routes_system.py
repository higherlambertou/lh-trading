"""系統健康（update.md 發現 66）：GET /api/system/health。只讀檔案與記憶體（在執行緒裡讀），不查券商、不碰下單。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter

from core import intel_reader, system_health, task_watch
from core.backup import backup_root
from core.broker import broker
from core.session_watch import session_watch
from core.trade_log import current_mode

router = APIRouter()
STARTED_AT = time.time()


def sdk_version() -> str | None:
    """這個後端用的 shioaji 版本（讀套件資訊，不 import；發現 68 起正式盤用專案的 .venv）。"""
    try:
        from importlib.metadata import version
        return version("shioaji")
    except Exception:  # noqa: BLE001
        return None


@router.get("/health")
async def get_health() -> dict[str, Any]:
    from api.routes_position import _cache
    now = time.time()
    return await asyncio.to_thread(system_health.collect, now, current_mode(), bool(broker.is_connected), session_watch.stale_reason,
                                   session_watch.snapshot(now), _cache.get("usage"), now - STARTED_AT, intel_reader.collector_status, backup_root(),
                                   sdk=sdk_version(), tasks=task_watch.status())
