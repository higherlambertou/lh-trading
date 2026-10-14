"""成交推播（update.md 發現 34）：手動單成交了就通知（預設只通知手動下的單），只通知、不動任何委託。

券商的成交回報只有 trade_id、價格、口數，沒有合約與買賣方向：用 TradeLog 記住的委託脈絡（record_order 時存的）補上。
NOTIFY_FILLS：manual（預設）＝只通知手動單；all＝策略的成交也通知（scalp 之類的會很吵）；off＝不通知。
停損停利監控送的平倉單（strategy=manual_monitor）不在這裡通知——它有自己的「觸發／平倉完成」訊息，避免同一件事通知兩次。
同一筆成交券商偶爾會重複送達：用 (trade_id, 成交序號或時間) 去重。部分成交每一筆各通知一次。"""
from __future__ import annotations

import logging
import os
from typing import Any

from core.notify import notify

logger = logging.getLogger(__name__)

ACTION_ZH = {"Buy": "買進", "Sell": "賣出"}


def mode() -> str:
    v = os.getenv("NOTIFY_FILLS", "manual").strip().lower()
    return v if v in ("off", "manual", "all") else "manual"


def describe(ev: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str] | None:
    """一筆成交回報＋委託脈絡 → (標題, 內容)；不該通知就回 None。"""
    m = mode()
    strat = str(ctx.get("strategy", ""))
    if m == "off" or ev.get("quantity", 0) <= 0 or strat == "manual_monitor":
        return None
    if m == "manual" and strat != "manual":
        return None
    qty, price = int(ev.get("quantity") or 0), float(ev.get("price") or 0)
    who = "手動" if strat == "manual" else (f"策略 {strat}" if strat else "來源不明")
    what = f"{ctx.get('contract', '?')} {ACTION_ZH.get(str(ctx.get('action')), '?')} {qty} 口 @ {price:,.0f}"
    return "✅ 成交", f"{what}（{who}）"


def on_deal(ev: dict[str, Any], ctx: dict[str, Any]) -> None:
    """TradeLog.deal_listeners 的監聽函式。永不丟例外。"""
    try:
        d = describe(ev, ctx)
        if d is None:
            return
        uid = f"{ev.get('trade_id', '')}:{ev.get('deal_seq') or ev.get('deal_ts') or ev.get('price')}:{ev.get('quantity')}"
        notify(d[0], d[1], key=f"fill:{uid}", cooldown=3600, priority="default")
    except Exception:  # noqa: BLE001
        logger.exception("成交推播失敗（已忽略）")


def install(trade_log: Any) -> None:
    """掛到 TradeLog（可重複呼叫）。"""
    if on_deal not in trade_log.deal_listeners:
        trade_log.deal_listeners.append(on_deal)
