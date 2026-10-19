"""〈資訊〉面板的資料（core/intel_reader.py）：唯讀讀 lh-intel 的 intel.db，只供顯示。沒有任何策略或風控模組讀它。"""
import asyncio
from typing import Any

from fastapi import APIRouter

from core import intel_reader

router = APIRouter()


@router.get("/summary")
async def get_summary() -> dict[str, Any]:
    """事件倒數、籌碼、全球市場、新聞、收集器狀態——一次回齊（10 秒快取）。**永遠回 200**：資料庫不在、打不開、版本不對時 `available: false` 並寫原因，
    畫面會顯示「資訊系統沒有資料」而不是整頁壞掉。只讀、不寫、不碰券商、不碰下單；同步的 SQLite 讀取丟 executor，不佔 event loop。"""
    return await asyncio.to_thread(intel_reader.summary)
