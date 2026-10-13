"""盤前跨市場資訊紀錄的查詢與匯出（core/premarket_log.py，update.md 發現 31）。只供查看；沒有任何策略或風控模組讀這些欄位。"""
import asyncio
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from core import premarket_log as pm

router = APIRouter()


@router.get("/status")
def get_status() -> dict[str, Any]:
    """是否啟用、取樣時間、來源順序、已記錄幾筆、今天記了沒、下一筆什麼時候記、錯過的日子、最近一筆缺哪些欄位、失敗警示。"""
    return pm.status()


@router.get("/preview")
async def get_preview(date: str = "") -> dict[str, Any]:
    """某個交易日（預設最近一個 T 已過的）的快照『會記成什麼樣子』——只算、不寫入。會去抓 Nasdaq／Cboe／證交所（60 秒快取），在 executor 裡跑。"""
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date() if date else None
    except ValueError:
        raise HTTPException(422, "date 要是 YYYY-MM-DD") from None
    try:
        return await asyncio.to_thread(pm.preview, d)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"預覽失敗：{type(e).__name__}: {e}") from e


@router.get("/snapshots")
def get_snapshots(days: float = 60, limit: int = 500) -> list[dict[str, Any]]:
    """每個台股交易日一筆（有更正就是更正後的內容；voided＝作廢），新 → 舊。缺值是 null、missing_reason 寫原因。"""
    rows = pm.default_store().effective(date.today() - timedelta(days=max(1.0, min(days, 3650.0))))
    return rows[: max(1, min(limit, 5000))]


@router.get("/snapshots.csv")
def get_snapshots_csv(days: float = 3650) -> Response:
    """匯出 CSV（UTF-8 含 BOM；時間一律 Asia/Taipei +08:00；缺值留空）。"""
    rows = pm.default_store().effective(date.today() - timedelta(days=max(1.0, min(days, 3650.0))))
    return Response("﻿" + pm.to_csv(list(reversed(rows))), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=premarket_snapshots.csv"})


@router.post("/run")
async def run_now() -> dict[str, Any]:
    """現在嘗試記今天的快照（冪等：已經有了就回 exists；T 之前回 too_early；13:45 之後回 too_late；休市回 skipped）。網路與資料庫都在 executor 裡跑，不佔 event loop。"""
    d = datetime.now(pm.TPE).date()
    return await asyncio.to_thread(pm.run_guarded, d)
