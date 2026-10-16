import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core import entry_log
from core.trade_log import current_mode, trade_log

router = APIRouter()


@router.get("/orders")
def get_orders(limit: int = 50, strategy: Optional[str] = None) -> list[dict[str, Any]]:
    """最近的委託（新→舊）+ 成交彙總。slip_signal / slip_ref 為滑價（點），正 = 對我方不利。
    outcome：filled / partial / rejected / cancelled / error / timeout / unfilled（IOC 久無回報）/ open。"""
    return trade_log.orders(mode=current_mode(), limit=max(1, min(limit, 1000)), strategy=strategy)


@router.get("/summary")
def get_summary(days: float = 30) -> dict[str, Any]:
    """依 (策略, 原因) 彙總近 N 天：成交結果、滑價分布（訊號價／停損停利設定價）、送單延遲。"""
    return trade_log.summary(current_mode(), time.time() - max(0.1, min(days, 3650.0)) * 86400)


@router.get("/entries")
def get_entries(days: float = 30, limit: int = 1000) -> list[dict[str, Any]]:
    """進場市場狀態紀錄：一筆交易一列（進場快照 + 出場彙總），新 → 舊。市場狀態欄位取不到就是 null、missing_reason 寫原因。
    只供查看與匯出；沒有任何策略或風控讀取這些欄位。"""
    return entry_log.fetch_entries(trade_log.path, mode=current_mode(), since_ts=time.time() - max(0.1, min(days, 3650.0)) * 86400,
                                   limit=max(1, min(limit, 20000)))


@router.get("/entries.csv")
def get_entries_csv(days: float = 3650, limit: int = 20000) -> Response:
    """同上，匯出 CSV（UTF-8 含 BOM，Excel 直接開；時間一律 Asia/Taipei +08:00）。"""
    rows = entry_log.fetch_entries(trade_log.path, mode=current_mode(), since_ts=time.time() - max(0.1, min(days, 3650.0)) * 86400,
                                   limit=max(1, min(limit, 20000)))
    body = "\ufeff" + entry_log.to_csv(rows)
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="entries_{current_mode()}.csv"'})


class LabelRequest(BaseModel):
    trend: str = Field(description="multi（多）｜short（空）｜range（盤整）｜none（無明確判斷）")
    note: Optional[str] = Field(default=None, max_length=entry_log.NOTE_MAX)


@router.post("/entries/{trade_id}/label")
def post_label(trade_id: str, body: LabelRequest) -> dict[str, Any]:
    """手動單的主觀趨勢判斷標籤（update.md 發現 47）：進場後任何時候補填，**不阻擋、不延遲任何下單**；只新增一列（改標籤＝再新增，舊的留著）。
    輸入時間晚於第一次出場會標 label_after_exit。策略單回 422（主觀欄位固定為空）。只供事後分析，沒有任何策略或風控讀取。"""
    try:
        return entry_log.add_label(trade_log.path, current_mode(), trade_id, body.trend, body.note)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/entries/status")
def get_entries_status() -> dict[str, Any]:
    """進場紀錄的健康狀態：筆數、尚未處理的成交回報、寫入失敗的通報次數與最近一筆。alerts>0 代表有失敗（不影響下單，但要看）。"""
    return trade_log.entry_status()
