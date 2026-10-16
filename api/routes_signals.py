"""影子訊號的結果（update.md 發現 43）：只讀 data/signals.db，不查券商、不碰下單。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from core import signal_rules as sr
from core import signal_shadow as sh
from core.daytrade import TAIPEI

router = APIRouter()


@router.get("/shadow")
async def shadow(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    """累積的影子訊號成績（各變體、日盤／夜盤、持有 5／15／30 分鐘，淨報酬已扣成本）與最近的訊號。"""
    rows = await asyncio.to_thread(sh.default_store().rows)
    recent = [{"id": r["id"], "rule": r["rule"], "time": datetime.fromtimestamp(r["ts"], TAIPEI).isoformat(timespec="minutes"), "session": r["session"],
               "direction": r["direction"], "move_pts": round(r["move"] * sr.REF_PRICE, 1), "vol_ratio": round(r["vol_ratio"], 2),
               "close": r["signal_close"], "entry_price": r["entry_price"], "done": bool(r["done"])} for r in rows[:limit]]
    return {"enabled": sh.enabled(), "cost_pts": sr.cost_pts(), "ref_price": sr.REF_PRICE, "summary": sh.summary(rows), "recent": recent}
