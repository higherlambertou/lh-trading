from typing import Any

from fastapi import APIRouter, HTTPException

from core.broker import broker

router = APIRouter()


@router.get("/")
def get_positions() -> list[dict[str, Any]]:
    try:
        positions = broker.api.list_positions(broker.api.futopt_account)
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
        profit_loss = broker.api.list_profit_loss(broker.api.futopt_account)
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


@router.get("/margin")
def get_margin() -> dict[str, float]:
    try:
        margin = broker.api.margin(broker.api.futopt_account)
    except Exception as e:
        raise HTTPException(500, f"查詢保證金失敗: {e}")
    return {
        "equity": float(margin.equity),
        "equity_amount": float(margin.equity_amount),
        "margin_call": float(margin.margin_call),
        "initial_margin": float(margin.initial_margin),
        "maintenance_margin": float(margin.maintenance_margin),
    }
