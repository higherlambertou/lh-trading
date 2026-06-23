import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from core.broker import broker

logger = logging.getLogger(__name__)
router = APIRouter()

CACHE_TTL = 10

_cache: dict[str, Any] = {
    "positions": [],
    "pnl": [],
    "margin": None,
    "usage": None,
    "updated_at": 0.0,
}


async def cache_refresh_loop() -> None:
    """背景快取刷新：每 60s 查保證金，每 120s 查流量。
    直接呼叫 broker.margin() / broker.usage()（async，透過 worker IPC，不阻塞 loop）。"""
    tick = 0
    await asyncio.sleep(30)
    while True:
        await asyncio.sleep(60)
        tick += 1
        if not broker.is_connected:
            continue

        try:
            _cache["margin"] = await asyncio.wait_for(broker.margin(), timeout=5)
            _cache["updated_at"] = time.time()
        except Exception as e:
            logger.debug("margin cache 刷新失敗（保留舊值）: %s", e)

        if tick % 2 == 0:
            try:
                _cache["usage"] = await asyncio.wait_for(broker.usage(), timeout=5)
            except Exception as e:
                logger.debug("usage cache 刷新失敗（保留舊值）: %s", e)


@router.get("/")
def get_positions() -> list[dict[str, Any]]:
    return _cache["positions"]


@router.get("/meta")
def get_meta() -> dict[str, float]:
    return {
        "updated_at": _cache["updated_at"],
        "age_sec": round(time.time() - _cache["updated_at"], 1) if _cache["updated_at"] else -1,
    }


@router.get("/pnl")
def get_pnl() -> list[dict[str, Any]]:
    return _cache["pnl"]


@router.get("/margin")
def get_margin() -> dict[str, float]:
    if _cache["margin"] is None:
        raise HTTPException(503, "保證金資料尚未就緒（首次查詢中）")
    return _cache["margin"]


@router.get("/usage")
def get_usage() -> dict[str, Any]:
    if _cache["usage"] is None:
        raise HTTPException(503, "流量資料尚未就緒（首次查詢中，請稍後再試）")
    return _cache["usage"]
