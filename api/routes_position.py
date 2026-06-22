import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from core.broker import broker

logger = logging.getLogger(__name__)
router = APIRouter()

# ── 快取層 ────────────────────────────────────────────────────────────
# HTTP 端點只讀記憶體，完全不打 shioaji。
# 後端背景 task (_cache_refresh_loop) 每 CACHE_TTL 秒刷一次，有 timeout 保護。
# 這樣前端高頻輪詢不會觸發額外的 shioaji 呼叫，大幅降低 GIL 凍結機率。

CACHE_TTL = 10  # 秒

_cache: dict[str, Any] = {
    "positions": [],
    "pnl": [],
    "margin": None,
    "usage": None,
    "updated_at": 0.0,
}


async def cache_refresh_loop() -> None:
    """背景快取刷新。
    list_positions / list_profit_loss 已移除——這兩個呼叫在永豐 Solace 不穩時
    會持 GIL 卡死整個進程，且每 10 秒一輪，是凍結的主因。
    部位資料改由策略自身的 state 追蹤（/api/strategy/{name}）。
    只保留 margin（每 60s）和 usage（每 120s），頻率低、影響小。"""
    tick = 0
    await asyncio.sleep(30)  # 啟動後 30s 才開始，讓連線穩定
    while True:
        await asyncio.sleep(60)
        tick += 1
        if not broker.is_connected:
            continue
        account = broker.api.futopt_account if broker.is_connected else None
        if account is None:
            continue

        # 每 60 秒查一次保證金
        try:
            m = await broker.acall_to(lambda: broker.api.margin(account), timeout=3)
            _cache["margin"] = {
                "equity": float(m.equity),
                "equity_amount": float(m.equity_amount),
                "margin_call": float(m.margin_call),
                "initial_margin": float(m.initial_margin),
                "maintenance_margin": float(m.maintenance_margin),
            }
            _cache["updated_at"] = time.time()
        except Exception as e:
            logger.debug("margin cache 刷新失敗（保留舊值）: %s", e)

        # 每 120 秒查一次流量
        if tick % 2 == 0:
            try:
                u = await broker.acall_to(lambda: broker.api.usage(), timeout=3)
                used = int(getattr(u, "bytes", 0) or 0)
                limit = int(getattr(u, "limit_bytes", 0) or 0)
                remaining = int(getattr(u, "remaining_bytes", max(limit - used, 0)) or 0)
                _cache["usage"] = {
                    "connections": int(getattr(u, "connections", 0) or 0),
                    "used_bytes": used,
                    "limit_bytes": limit,
                    "remaining_bytes": remaining,
                    "percent": round(used / limit * 100, 1) if limit > 0 else 0.0,
                }
            except Exception as e:
                logger.debug("usage cache 刷新失敗（保留舊值）: %s", e)


# ── 端點：讀快取，不打 shioaji ────────────────────────────────────────

@router.get("/")
def get_positions() -> list[dict[str, Any]]:
    return _cache["positions"]


@router.get("/meta")
def get_meta() -> dict[str, float]:
    """快取最後成功刷新的時間（epoch 秒）。前端用來顯示資料新鮮度，
    後端凍結/重啟期間 stale 資料才不會默默誤導使用者。"""
    return {"updated_at": _cache["updated_at"], "age_sec": round(time.time() - _cache["updated_at"], 1) if _cache["updated_at"] else -1}


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
    """流量查詢：讀快取（每 CACHE_TTL 秒背景刷新），不直接打 shioaji。
    避免 broker.api.usage() 在 Solace 不穩時持 GIL 凍住整個進程。"""
    if _cache["usage"] is None:
        raise HTTPException(503, "流量資料尚未就緒（首次查詢中，請稍後再試）")
    return _cache["usage"]
