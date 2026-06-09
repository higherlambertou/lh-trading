import logging
from typing import Any, Optional

import shioaji as sj
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from core.broker import broker
from core.manual_monitor import manual_monitor, _txo_round_tick

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
            order_id=str(getattr(trade.status, "id", "") or ""),
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


# ── 選擇權 ─────────────────────────────────────────────────────────

@router.get("/option/expiries")
def option_expiries(category: str = "TXO") -> list[str]:
    try:
        return broker.call(lambda: broker.option_expiries(category))
    except Exception as e:
        raise HTTPException(500, f"取得選擇權月份失敗: {e}")


@router.get("/option/categories")
def option_categories() -> dict[str, Any]:
    """列出永豐實際提供的選擇權類別（月選/各週選）與各自月份，供探查用。"""
    try:
        return broker.call(lambda: broker.option_categories_debug())
    except Exception as e:
        raise HTTPException(500, f"取得選擇權類別失敗: {e}")


@router.get("/option/strikes")
def option_strikes(delivery_month: str, right: str, category: str = "TXO") -> list[int]:
    try:
        return broker.call(
            lambda: broker.option_strikes(delivery_month, right, category)
        )
    except Exception as e:
        raise HTTPException(500, f"取得履約價失敗: {e}")


@router.get("/option/quote")
def option_quote(
    delivery_month: str, strike: int, right: str, category: str = "TXO"
) -> dict[str, Any]:
    """單一選擇權的參考現價（snapshot）：成交價 + 買/賣價。
    給下單面板顯示用，前端低頻刷新（避免吃行情查詢額度）。"""
    try:
        contract = broker.call(
            lambda: broker.option_contract(delivery_month, strike, right, category)
        )
    except Exception as e:
        raise HTTPException(400, f"找不到選擇權合約: {e}")
    try:
        snaps = broker.call(lambda: broker.api.snapshots([contract]))
    except Exception as e:
        raise HTTPException(500, f"查詢報價失敗: {e}")
    if not snaps:
        raise HTTPException(404, "查無報價")
    s = snaps[0]
    return {
        "code": str(getattr(contract, "code", "")),
        "close": float(getattr(s, "close", 0) or 0),
        "bid": float(getattr(s, "buy_price", 0) or 0),
        "ask": float(getattr(s, "sell_price", 0) or 0),
        "total_volume": int(getattr(s, "total_volume", 0) or 0),
    }


class OptionOrderRequest(BaseModel):
    delivery_month: str           # 到期月份，如 "202606"
    strike: int                   # 履約價
    option_right: str             # "C"(買權) 或 "P"(賣權)
    category: str = "TXO"         # TXO=月選；TX1~TX5=週選
    action: str                   # "Buy"(買進) 或 "Sell"(賣出/賣方)
    quantity: int = 1
    price: float                  # 權利金限價（選擇權一律限價，必填）
    order_type: str = "ROD"       # "ROD" / "IOC" / "FOK"
    stop_loss_pts: int = 0        # 權利金停損點數（0=停用）
    take_profit_pts: int = 0      # 權利金停利點數（0=停用）
    exit_buffer_pts: int = 3      # 觸發停損停利平倉時的讓價點數，提高成交率

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
def place_option(req: OptionOrderRequest) -> dict[str, Any]:
    # 1) 解析合約
    try:
        contract = broker.call(
            lambda: broker.option_contract(
                req.delivery_month, req.strike, req.option_right, req.category
            )
        )
    except Exception as e:
        raise HTTPException(400, f"找不到選擇權合約: {e}")

    try:
        action = getattr(sj.constant.Action, req.action)
        order_type = getattr(sj.constant.OrderType, req.order_type)
    except AttributeError as e:
        raise HTTPException(400, f"無效的參數值: {e}")

    limit_price = _txo_round_tick(req.price)  # 對齊合法跳動點，避免被退單

    # 2) 下單（選擇權一律限價）
    def _place():
        order = sj.FuturesOrder(
            action=action,
            price=limit_price,
            quantity=req.quantity,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=order_type,
            octype=sj.constant.FuturesOCType.Auto,
            account=broker.api.futopt_account,
        )
        return broker.api.place_order(contract, order)

    try:
        trade = broker.call(_place)
    except Exception as e:
        logger.error("選擇權下單失敗: %s", e)
        raise HTTPException(500, f"下單失敗: {e}")

    # 3) 登記停損停利監控（買=+1 / 賣=-1，權利金點數）
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
            order_id=str(getattr(trade.status, "id", "") or ""),
            is_option=True,
            match_code=str(getattr(contract, "code", "")),
            multiplier=float(getattr(contract, "multiplier", 0) or 0),
            exit_buffer_pts=req.exit_buffer_pts,
            contract_obj=contract,
        )

    return {
        "trade_id": trade.status.id,
        "status": trade.status.status.value,
        "code": str(getattr(contract, "code", "")),
        "limit_price": limit_price,
        **({"watch_id": watch_id} if watch_id else {}),
    }


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
