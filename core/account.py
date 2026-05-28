import logging
from typing import Any

from core.broker import broker

logger = logging.getLogger(__name__)


def get_positions() -> list[dict[str, Any]]:
    positions = broker.api.list_positions(broker.api.futopt_account)
    return [
        {
            "code": p.code,
            "direction": p.direction.value,
            "quantity": p.quantity,
            "price": float(p.price),
            "last_price": float(p.last_price),
            "pnl": float(p.pnl),
            "margin_original": float(p.margin_original),
        }
        for p in positions
    ]


def get_profit_loss() -> list[dict[str, Any]]:
    profit_loss = broker.api.list_profit_loss(broker.api.futopt_account)
    return [
        {
            "code": pl.code,
            "quantity": pl.quantity,
            "price": float(pl.price),
            "pnl": float(pl.pnl),
            "dseq": pl.dseq,
        }
        for pl in profit_loss
    ]


def get_margin() -> dict[str, float]:
    margin = broker.api.margin(broker.api.futopt_account)
    return {
        "equity": float(margin.equity),
        "equity_amount": float(margin.equity_amount),
        "margin_call": float(margin.margin_call),
        "initial_margin": float(margin.initial_margin),
        "maintenance_margin": float(margin.maintenance_margin),
    }
