from typing import Any

from fastapi import APIRouter, HTTPException

from core.broker import broker

router = APIRouter()


@router.get("/")
def get_positions() -> list[dict[str, Any]]:
    try:
        positions = broker.call(lambda: broker.api.list_positions(broker.api.futopt_account))
    except Exception as e:
        raise HTTPException(500, f"查詢部位失敗: {e}")
    return [
        {
            "code": p.code,
            "direction": p.direction.value,
            "quantity": p.quantity,
            "price": float(p.price),
            "last_price": float(getattr(p, "last_price", p.price)),
            "pnl": float(getattr(p, "pnl", 0)),
            "margin_original": float(getattr(p, "margin_original", 0)),
        }
        for p in positions
    ]


@router.get("/pnl")
def get_pnl() -> list[dict[str, Any]]:
    try:
        profit_loss = broker.call(lambda: broker.api.list_profit_loss(broker.api.futopt_account))
    except Exception as e:
        raise HTTPException(500, f"查詢損益失敗: {e}")
    return [
        {
            "code": getattr(pl, "code", ""),
            "quantity": int(getattr(pl, "quantity", 0) or 0),
            "price": float(getattr(pl, "price", 0) or 0),
            "pnl": float(getattr(pl, "pnl", 0) or 0),
            "dseq": str(getattr(pl, "dseq", "")),
            "date": str(getattr(pl, "date", "") or ""),
        }
        for pl in profit_loss
    ]


@router.get("/usage")
def get_usage() -> dict[str, Any]:
    """查詢 Shioaji 當日流量用量與連線數（額度開盤日 08:00 重置）。"""
    try:
        u = broker.call(lambda: broker.api.usage())
    except Exception as e:
        raise HTTPException(500, f"查詢用量失敗: {e}")
    used = int(getattr(u, "bytes", 0) or 0)
    limit = int(getattr(u, "limit_bytes", 0) or 0)
    remaining = int(getattr(u, "remaining_bytes", max(limit - used, 0)) or 0)
    return {
        "connections": int(getattr(u, "connections", 0) or 0),
        "used_bytes": used,
        "limit_bytes": limit,
        "remaining_bytes": remaining,
        "percent": round(used / limit * 100, 1) if limit > 0 else 0.0,
    }


@router.get("/margin")
def get_margin() -> dict[str, float]:
    try:
        margin = broker.call(lambda: broker.api.margin(broker.api.futopt_account))
    except Exception as e:
        raise HTTPException(500, f"查詢保證金失敗: {e}")
    return {
        "equity": float(margin.equity),
        "equity_amount": float(margin.equity_amount),
        "margin_call": float(margin.margin_call),
        "initial_margin": float(margin.initial_margin),
        "maintenance_margin": float(margin.maintenance_margin),
    }
