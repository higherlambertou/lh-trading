import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from core.broker import broker
from core.manual_monitor import manual_monitor, _txo_round_tick

logger = logging.getLogger(__name__)
router = APIRouter()


class ManualOrderRequest(BaseModel):
    action: str
    quantity: int = 1
    price: Optional[float] = None
    price_type: str = "MKT"
    order_type: str = "IOC"
    octype: str = "Auto"
    contract: str = "TMF"
    stop_loss_pts: int = 0
    take_profit_pts: int = 0

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ("Buy", "Sell"):
            raise ValueError("action 必須是 'Buy' 或 'Sell'")
        return v

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity 必須大於 0")
        return v

    @field_validator("contract")
    @classmethod
    def validate_contract(cls, v: str) -> str:
        if v not in ("TMF", "MXF", "TXF"):
            raise ValueError("contract 必須是 'TMF', 'MXF' 或 'TXF'")
        return v


@router.post("/place")
async def place_order(req: ManualOrderRequest) -> dict[str, Any]:
    try:
        trade = await broker.place_order(
            contract_code=req.contract,
            action=req.action,
            quantity=req.quantity,
            price=req.price or 0,
            price_type=req.price_type,
            order_type=req.order_type,
            octype=req.octype,
        )
    except asyncio.TimeoutError:
        raise HTTPException(503, "下單逾時（券商連線忙碌）")
    except Exception as e:
        logger.error("手動下單失敗: %s", e)
        raise HTTPException(500, f"下單失敗: {e}")

    watch_id = None
    if req.stop_loss_pts > 0 or req.take_profit_pts > 0:
        direction = 1 if req.action == "Buy" else -1
        watch_id = manual_monitor.add(
            contract=req.contract,
            direction=direction,
            quantity=req.quantity,
            entry_price=req.price or 0.0,
            stop_loss_pts=req.stop_loss_pts,
            take_profit_pts=req.take_profit_pts,
            order_id=str(trade.get("trade_id", "") or ""),
        )

    return {
        "trade_id": trade["trade_id"],
        "status": trade["status"],
        **({"watch_id": watch_id} if watch_id else {}),
    }


@router.post("/cancel/{trade_id}")
async def cancel_order(trade_id: str) -> dict[str, str]:
    try:
        await broker.cancel_order(trade_id)
    except asyncio.TimeoutError:
        raise HTTPException(503, "取消委託逾時")
    except Exception as e:
        raise HTTPException(500, f"取消委託失敗: {e}")
    return {"status": "cancelled", "trade_id": trade_id}


@router.get("/watches")
def list_watches() -> list[dict[str, Any]]:
    return manual_monitor.list_watches()


class WatchUpdateRequest(BaseModel):
    stop_loss_pts: Optional[int] = None
    take_profit_pts: Optional[int] = None


@router.patch("/watches/{watch_id}")
def update_watch(watch_id: str, req: WatchUpdateRequest) -> dict[str, str]:
    ok = manual_monitor.update(watch_id, req.stop_loss_pts, req.take_profit_pts)
    if not ok:
        raise HTTPException(404, f"找不到 watch_id={watch_id}")
    return {"status": "updated", "watch_id": watch_id}


@router.delete("/watches/{watch_id}")
def cancel_watch(watch_id: str) -> dict[str, str]:
    if watch_id not in {w["id"] for w in manual_monitor.list_watches()}:
        raise HTTPException(404, f"找不到 watch_id={watch_id}")
    manual_monitor.remove(watch_id)
    return {"status": "removed", "watch_id": watch_id}


# ── 選擇權 ─────────────────────────────────────────────────────────

@router.get("/option/expiries")
async def option_expiries(category: str = "TXO") -> list[str]:
    try:
        return await broker.option_expiries(category)
    except asyncio.TimeoutError:
        raise HTTPException(503, "取得選擇權月份逾時（券商連線忙碌）")
    except Exception as e:
        raise HTTPException(500, f"取得選擇權月份失敗: {e}")


@router.get("/option/strikes")
async def option_strikes(delivery_month: str, right: str, category: str = "TXO") -> list[int]:
    try:
        return await broker.option_strikes(delivery_month, right, category)
    except asyncio.TimeoutError:
        raise HTTPException(503, "取得履約價逾時（券商連線忙碌）")
    except Exception as e:
        raise HTTPException(500, f"取得履約價失敗: {e}")


@router.get("/option/quote")
async def option_quote(
    delivery_month: str, strike: int, right: str, category: str = "TXO"
) -> dict[str, Any]:
    try:
        return await broker.option_snapshot(delivery_month, strike, right, category)
    except asyncio.TimeoutError:
        raise HTTPException(503, "查詢選擇權報價逾時（券商連線忙碌）")
    except Exception as e:
        raise HTTPException(500, f"查詢報價失敗: {e}")


class OptionOrderRequest(BaseModel):
    delivery_month: str
    strike: int
    option_right: str
    category: str = "TXO"
    action: str
    quantity: int = 1
    price: float
    order_type: str = "ROD"
    stop_loss_pts: int = 0
    take_profit_pts: int = 0
    exit_buffer_pts: int = 3

    @field_validator("action")
    @classmethod
    def _v_action(cls, v: str) -> str:
        if v not in ("Buy", "Sell"):
            raise ValueError("action 必須是 'Buy' 或 'Sell'")
        return v

    @field_validator("option_right")
    @classmethod
    def _v_right(cls, v: str) -> str:
        if v.upper()[:1] not in ("C", "P"):
            raise ValueError("option_right 必須是 'C' 或 'P'")
        return v.upper()[:1]

    @field_validator("quantity")
    @classmethod
    def _v_qty(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity 必須大於 0")
        return v

    @field_validator("price")
    @classmethod
    def _v_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price（權利金限價）必須大於 0")
        return v


@router.post("/place_option")
async def place_option(req: OptionOrderRequest) -> dict[str, Any]:
    limit_price = _txo_round_tick(req.price)
    try:
        trade = await broker.place_option_order(
            delivery_month=req.delivery_month,
            strike=req.strike,
            right=req.option_right,
            category=req.category,
            action=req.action,
            quantity=req.quantity,
            price=limit_price,
            order_type=req.order_type,
        )
    except asyncio.TimeoutError:
        raise HTTPException(503, "選擇權下單逾時")
    except Exception as e:
        logger.error("選擇權下單失敗: %s", e)
        raise HTTPException(500, f"下單失敗: {e}")

    watch_id = None
    if req.stop_loss_pts > 0 or req.take_profit_pts > 0:
        direction = 1 if req.action == "Buy" else -1
        watch_id = manual_monitor.add(
            contract=req.category,
            direction=direction,
            quantity=req.quantity,
            entry_price=limit_price,
            stop_loss_pts=req.stop_loss_pts,
            take_profit_pts=req.take_profit_pts,
            order_id=str(trade.get("trade_id", "") or ""),
            is_option=True,
            match_code=str(trade.get("code", "")),
            exit_buffer_pts=req.exit_buffer_pts,
            delivery_month=req.delivery_month,
            strike_price=req.strike,
            option_right=req.option_right,
            option_category=req.category,
        )

    return {
        "trade_id": trade["trade_id"],
        "status": trade["status"],
        "code": trade.get("code", ""),
        "limit_price": limit_price,
        **({"watch_id": watch_id} if watch_id else {}),
    }


@router.get("/trades")
async def list_trades() -> list[dict[str, Any]]:
    try:
        trades = await broker.list_trades()
    except asyncio.TimeoutError:
        raise HTTPException(503, "查詢委託逾時（券商連線忙碌，稍後自動重試）")
    except Exception as e:
        raise HTTPException(500, f"查詢委託失敗: {e}")

    from datetime import datetime, timezone

    def _fmt_time(val) -> str:
        if not val:
            return ""
        if isinstance(val, (int, float)):
            dt = datetime.fromtimestamp(val / 1e9 if val > 1e12 else val, tz=timezone.utc).astimezone()
            return dt.strftime("%H:%M:%S") if dt.year >= 2020 else ""
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val).strftime("%H:%M:%S")
            except ValueError:
                pass
            if len(val) >= 8 and val[2] == ":" and val[5] == ":":
                return val[:8]
            return ""
        if hasattr(val, "strftime"):
            return val.strftime("%H:%M:%S")
        return ""

    result = []
    for t in trades:
        order_time = _fmt_time(t.get("order_datetime"))
        deal_time  = _fmt_time(t.get("deal_ts"))
        result.append({
            "id": t["id"],
            "action": t["action"],
            "price": t["price"],
            "deal_price": t.get("avg_deal_price", 0),
            "quantity": t["quantity"],
            "status": t["status"],
            "deal_quantity": t.get("deal_quantity", 0),
            "order_time": order_time,
            "deal_time": deal_time,
        })

    # 已成交的排前面（有 deal_ts），無時間資料排最後
    result.sort(key=lambda x: x.get("deal_time", "") or x.get("order_time", ""), reverse=True)
    return result
