import asyncio
import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.routes_strategy import strategy_engine
from core.broker import broker
from core.daily_summary import TICK_SEC, market_state

logger = logging.getLogger(__name__)
router = APIRouter()


class IVRequest(BaseModel):
    iv: float = Field(gt=0, lt=300, description="ATM 隱含波動率，單位 %（例如 18.5）")
    date: Optional[str] = Field(default=None, description="YYYY-MM-DD；不給 = 今日（會重算今日狀態）")


class NoteRequest(BaseModel):
    basis: Optional[str] = Field(default=None, max_length=2000)   # 判斷依據（你看到了什麼）
    notes: Optional[str] = Field(default=None, max_length=2000)   # 備註


async def market_state_loop() -> None:
    """背景：等券商連線 → 載入/計算今日狀態 → 每 30s 取樣策略績效 + 盤前/盤後排程。"""
    for _ in range(180):
        if broker.is_connected:
            break
        await asyncio.sleep(1)
    if broker.is_connected:
        # 讓啟動訂閱（_startup_bg）與快取刷新先佔 worker：日K查詢會讓 worker 排隊指令
        await asyncio.sleep(10)
    try:
        await market_state.startup()
    except Exception as e:
        logger.warning("市場狀態啟動計算失敗（之後由排程重試/手動 refresh）: %r", e)
    while True:
        await asyncio.sleep(TICK_SEC)
        try:
            await market_state.tick(strategy_engine.strategies)
        except Exception as e:
            logger.warning("市場狀態排程例外: %r", e)


@router.get("/state")
def get_state() -> dict[str, Any]:
    """今日市場狀態（純讀記憶體快取，不對券商發查詢）。"""
    return market_state.snapshot()


@router.post("/refresh")
async def refresh(force: bool = False) -> dict[str, Any]:
    """立刻重算今日狀態（向券商抓日K + ATM 選擇權報價）。
    worker 是單執行緒，查詢期間下單指令會排隊，所以有策略執行中時預設拒絕。"""
    running = [n for n, s in strategy_engine.strategies.items() if s.state.is_running]
    if running and not force:
        raise HTTPException(
            409, f"策略 {running[0]} 執行中：查詢期間 worker 會暫時排隊下單指令。"
                 "確定要重算請加 ?force=true"
        )
    return await market_state.refresh("manual")


@router.post("/iv")
async def set_iv(req: IVRequest) -> dict[str, Any]:
    """手動輸入 ATM IV（%）。今日 → 重算今日狀態；過去日期 → 只回填歷史。"""
    if req.date:
        try:
            date.fromisoformat(req.date)
        except ValueError:
            raise HTTPException(422, "date 格式需為 YYYY-MM-DD")
    return await market_state.set_manual_iv(req.iv, req.date)


@router.get("/journal")
def get_journal(limit: int = 30) -> list[dict[str, Any]]:
    """每日日誌（新→舊）：市場判斷 + 當日策略結果 + 你填的判斷依據/備註。"""
    return market_state.store.list_journal(market_state.mode, max(1, min(limit, 365)))


@router.patch("/journal/{day}")
def patch_journal(day: str, req: NoteRequest) -> dict[str, str]:
    if not market_state.store.set_note(day, market_state.mode, req.basis, req.notes):
        raise HTTPException(404, f"找不到 {day} 的日誌")
    return {"status": "updated", "date": day}


@router.get("/stats")
def get_stats(strategy: str = "scalp") -> dict[str, Any]:
    """驗證統計：各市場狀態下該策略的勝率/賺賠比；各 IV 狀態下的大波動比例。"""
    return market_state.store.stats(market_state.mode, strategy)
