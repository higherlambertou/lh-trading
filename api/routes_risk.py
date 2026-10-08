import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

import api.routes_position as routes_position
from core.ruin import build_report, history_stats
from core.trade_log import current_mode, trade_log

router = APIRouter()
MIN_HISTORY_TRADES = 30       # 成交紀錄至少這麼多筆，才允許「用真實損益分布」模擬


@router.get("/ruin")
async def ruin(
    capital: Optional[float] = Query(None, gt=0, description="本金（元）；不給 = 目前帳戶權益數"),
    win_rate: float = Query(0.65, gt=0, lt=1, description="勝率 0~1"),
    tp_pts: float = Query(20, gt=0, description="停利點數"),
    sl_pts: float = Query(60, gt=0, description="停損點數"),
    qty: int = Query(1, ge=1, le=100),
    point_value: float = Query(10, gt=0, description="每點元數：TMF=10 / MXF=50 / TXF=200"),
    cost_pts: float = Query(0, ge=0, description="每筆成本（手續費+稅+滑價，點）"),
    trades: int = Query(1000, ge=10, le=5000, description="模擬的交易筆數"),
    paths: int = Query(4000, ge=500, le=20000, description="模擬路徑數"),
    ruin_level: float = Query(0, ge=0, description="破產線（元）：本金跌到這裡就視為出局"),
    use_history: bool = Query(False, description="用成交紀錄的真實損益分布做 bootstrap（需 >=30 筆）"),
) -> dict[str, Any]:
    """破產機率驗證：本金 ×0.5／×1／×2 的破產機率、損益兩平勝率、勝率×本金敏感度、歷史最長連虧對照。"""
    margin = routes_position._cache.get("margin") or {}
    equity = margin.get("equity")
    cap = capital if capital is not None else equity
    if not cap:
        raise HTTPException(422, "沒有本金：保證金資料尚未就緒，請帶 capital 參數")
    k = point_value * qty
    trips = trade_log.round_trips(mode=current_mode())["trips"]
    pnls = [t["pnl"] for t in trips]
    use = use_history and len(pnls) >= MIN_HISTORY_TRADES
    report = await asyncio.to_thread(
        build_report, float(cap), win_rate, tp_pts * k, sl_pts * k, cost=cost_pts * k, n_trades=trades,
        n_paths=paths, ruin_level=ruin_level, pnls=pnls if use else None)
    report["history"] = history_stats(pnls)
    report["history_used"] = use
    report["history_note"] = (
        None if use or not use_history
        else f"成交紀錄只有 {len(pnls)} 筆（至少 {MIN_HISTORY_TRADES} 筆才用真實損益分布），改用參數試算")
    report["defaults"] = {"capital_from_equity": capital is None, "equity": equity}
    return report


@router.get("/trips")
def trips(limit: int = Query(50, ge=1, le=1000)) -> dict[str, Any]:
    """成交紀錄還原的完整交易（FIFO 配對；實際損益，不含手續費與稅）。新→舊。"""
    res = trade_log.round_trips(mode=current_mode())
    return {"n": len(res["trips"]), "trips": res["trips"][::-1][:limit], "open": res["open"],
            "stats": history_stats([t["pnl"] for t in res["trips"]])}
