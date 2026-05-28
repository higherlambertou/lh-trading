import logging
from typing import Any, Optional

import shioaji as sj
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from core.broker import broker

logger = logging.getLogger(__name__)
router = APIRouter()


_CONTRACT_MAP = {
    "TMF": lambda: broker.tmf_contract(),
    "MXF": lambda: broker.mxf_contract(),
    "TXF": lambda: broker.txf_contract(),
}


class ManualOrderRequest(BaseModel):
    action: str                  # "Buy" or "Sell"
    quantity: int = 1
    price: Optional[float] = None
    price_type: str = "MKT"      # "LMT" or "MKT"
    order_type: str = "IOC"      # "ROD", "IOC", "FOK"
    octype: str = "Auto"
    contract: str = "TMF"        # "TMF", "MXF", "TXF"

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
        if v not in _CONTRACT_MAP:
            raise ValueError("contract 必須是 'TMF', 'MXF' 或 'TXF'")
        return v


@router.post("/place")
def place_order(req: ManualOrderRequest) -> dict[str, str]:
    try:
        action = getattr(sj.constant.Action, req.action)
        price_type = getattr(sj.constant.FuturesPriceType, req.price_type)
        order_type = getattr(sj.constant.OrderType, req.order_type)
        octype = getattr(sj.constant.FuturesOCType, req.octype)
    except AttributeError as e:
        raise HTTPException(400, f"無效的參數值: {e}")

    order = sj.FuturesOrder(
        action=action,
        price=req.price or 0,
        quantity=req.quantity,
        price_type=price_type,
        order_type=order_type,
        octype=octype,
        account=broker.api.futopt_account,
    )

    try:
        trade = broker.api.place_order(_CONTRACT_MAP[req.contract](), order)
    except Exception as e:
        logger.error("手動下單失敗: %s", e)
        raise HTTPException(500, f"下單失敗: {e}")

    return {
        "trade_id": trade.status.id,
        "status": trade.status.status.value,
    }


@router.post("/cancel/{trade_id}")
def cancel_order(trade_id: str) -> dict[str, str]:
    try:
        broker.api.update_status(broker.api.futopt_account)
        trades = broker.api.list_trades()
    except Exception as e:
        raise HTTPException(500, f"更新委託狀態失敗: {e}")

    trade = next((t for t in trades if t.status.id == trade_id), None)
    if not trade:
        raise HTTPException(404, f"找不到 trade_id={trade_id}")

    try:
        broker.api.cancel_order(trade)
    except Exception as e:
        raise HTTPException(500, f"取消委託失敗: {e}")

    return {"status": "cancelled", "trade_id": trade_id}


@router.get("/trades")
def list_trades() -> list[dict[str, Any]]:
    try:
        broker.api.update_status(broker.api.futopt_account)
        trades = broker.api.list_trades()
    except Exception as e:
        raise HTTPException(500, f"查詢委託失敗: {e}")

    result = []
    for t in trades:
        deals = getattr(t.status, "deals", []) or []
        total_qty = sum(d.quantity for d in deals)
        avg_deal_price = (
            sum(d.price * d.quantity for d in deals) / total_qty
            if total_qty > 0 else 0.0
        )
        # 成交時間：取最後一筆 deal 的 ts（奈秒），fallback 到委託時間
        deal_ts = None
        if deals:
            raw_ts = getattr(deals[-1], "ts", None)
            if raw_ts:
                from datetime import datetime, timezone
                deal_ts = datetime.fromtimestamp(raw_ts / 1e9, tz=timezone.utc).astimezone().strftime("%H:%M:%S")
        order_time = str(getattr(t.status, "order_datetime", "") or "")
        result.append({
            "id": t.status.id,
            "action": t.order.action.value,
            "price": t.order.price,
            "deal_price": round(avg_deal_price, 2),
            "quantity": t.order.quantity,
            "status": t.status.status.value,
            "deal_quantity": getattr(t.status, "deal_quantity", 0),
            "order_time": order_time,
            "deal_time": deal_ts or "",
        })
    return result
