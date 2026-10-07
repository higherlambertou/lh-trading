import time
from typing import Any, Optional

from fastapi import APIRouter

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
