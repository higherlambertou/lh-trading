import logging
from typing import Any, Optional

import shioaji as sj
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from core.broker import broker
from core.manual_monitor import manual_monitor

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
    stop_loss_pts: int = 0       # 0 = 停用
    take_profit_pts: int = 0     # 0 = 停用

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

    contract_name = req.contract
    req_price = req.price or 0

    def _build_and_place():
        order = sj.FuturesOrder(
            action=action,
            price=req_price,
            quantity=req.quantity,
            price_type=price_type,
            order_type=order_type,
            octype=octype,
            account=broker.api.futopt_account,
        )
        return broker.api.place_order(_CONTRACT_MAP[contract_name](), order)

    try:
        trade = broker.call(_build_and_place)
    except Exception as e:
        logger.error("手動下單失敗: %s", e)
        raise HTTPException(500, f"下單失敗: {e}")

    watch_id = None
    if req.stop_loss_pts > 0 or req.take_profit_pts > 0:
        direction = 1 if req.action == "Buy" else -1
        entry_price = req.price or 0.0  # MKT 單 entry_price=0，monitor 會補
        watch_id = manual_monitor.add(
            contract=req.contract,
            direction=direction,
            quantity=req.quantity,
            entry_price=entry_price,
            stop_loss_pts=req.stop_loss_pts,
            take_profit_pts=req.take_profit_pts,
        )

    return {
        "trade_id": trade.status.id,
        "status": trade.status.status.value,
        **({"watch_id": watch_id} if watch_id else {}),
    }


@router.post("/cancel/{trade_id}")
def cancel_order(trade_id: str) -> dict[str, str]:
    try:
        broker.call(lambda: broker.api.update_status(broker.api.futopt_account))
        trades = broker.api.list_trades()
    except Exception as e:
        raise HTTPException(500, f"更新委託狀態失敗: {e}")

    trade = next((t for t in trades if t.status.id == trade_id), None)
    if not trade:
        raise HTTPException(404, f"找不到 trade_id={trade_id}")

    try:
        broker.call(lambda: broker.api.cancel_order(trade))
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


@router.get("/trades")
def list_trades() -> list[dict[str, Any]]:
    try:
        broker.call(lambda: broker.api.update_status(broker.api.futopt_account))
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
        from datetime import datetime, timezone

        def _fmt_time(val) -> str:
            """把各種時間格式統一轉成 HH:MM:SS，無效值回傳空字串"""
            if not val:
                return ""
            if isinstance(val, (int, float)):
                # 奈秒 timestamp；年份 < 2020 視為模擬盤假值
                dt = datetime.fromtimestamp(val / 1e9, tz=timezone.utc).astimezone()
                return dt.strftime("%H:%M:%S") if dt.year >= 2020 else ""
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val).strftime("%H:%M:%S")
                except ValueError:
                    pass
                # 純 HH:MM:SS 字串
                if len(val) >= 8 and val[2] == ":" and val[5] == ":":
                    return val[:8]
                return ""
            if hasattr(val, "strftime"):
                return val.strftime("%H:%M:%S")
            return ""

        def _epoch(val) -> float:
            """把各種時間格式轉成 epoch 秒，用於排序；無效值回傳 0。"""
            if not val:
                return 0.0
            if isinstance(val, (int, float)):
                return val / 1e9 if val > 1e12 else float(val)
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val).timestamp()
                except ValueError:
                    return 0.0
            if hasattr(val, "timestamp"):
                try:
                    return val.timestamp()
                except Exception:
                    return 0.0
            return 0.0

        order_dt_raw = getattr(t.status, "order_datetime", None)
        deal_ts_raw  = getattr(deals[-1], "ts", None) if deals else None
        sort_ts = max(_epoch(order_dt_raw), _epoch(deal_ts_raw))

        deal_ts = _fmt_time(deal_ts_raw)
        order_time = _fmt_time(order_dt_raw)
        result.append((sort_ts, {
            "id": t.status.id,
            "action": t.order.action.value,
            "price": t.order.price,
            "deal_price": round(avg_deal_price, 2),
            "quantity": t.order.quantity,
            "status": t.status.status.value,
            "deal_quantity": getattr(t.status, "deal_quantity", 0),
            "order_time": order_time,
            "deal_time": deal_ts or "",
        }))

    # 依時間由新到舊排序（sort_ts 為 0 的無時間資料排最後）
    result.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in result]
