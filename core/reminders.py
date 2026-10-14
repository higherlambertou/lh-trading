"""收盤前提醒（update.md 發現 34）：當沖倉位還沒平，在收盤前推播提醒。只通知、不動任何委託。

為什麼：期貨商通常在收盤前約 15 分鐘（日盤約 13:30）強制沖銷還沒沖掉的當沖倉位（見 core/daytrade.py；細節以期貨商規定為準）。
怎麼知道哪些是當沖倉位：手動下單勾了當沖（倉別 DayTrade）時登記（daytrade.note_open）；停損停利監控的 octype＝DayTrade 也算（有落地，重啟後還在）。
券商的持倉資料不帶倉別，所以「勾了當沖但後端重啟過又沒掛停損停利」的部位認不出來——這是已知的限制。
提醒時間：13:15、13:25（週一～五，台北時間）；每個時間點只推一次。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from core import daytrade
from core.notify import notify

logger = logging.getLogger(__name__)

REMIND_AT = ((13, 15), (13, 25))
POLL_SEC = 20.0


def due(now: datetime) -> Optional[str]:
    """現在是不是提醒時間點（那一分鐘內）；回傳 "13:15" 之類，不是就 None。週末不提醒。"""
    t = now.astimezone(daytrade.TAIPEI)
    if t.weekday() >= 5:
        return None
    for h, m in REMIND_AT:
        if (t.hour, t.minute) == (h, m):
            return f"{h:02d}:{m:02d}"
    return None


def open_daytrade_positions(positions: list[dict[str, Any]], watches: list[dict[str, Any]], marks: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """還開著的當沖倉位：持倉裡和登記（marks＝(合約, 方向 1／-1)）或監看（octype＝DayTrade、已看到部位）對得上的。"""
    out = []
    for p in positions or []:
        code, direction = str(p.get("code", "")), 1 if p.get("direction") == "Buy" else -1
        qty = int(p.get("quantity", 0) or 0)
        if qty <= 0 or not code:
            continue
        hit = any(code.startswith(c) and d == direction for c, d in marks) or \
            any(w.get("octype") == "DayTrade" and w.get("seen") and (1 if w.get("direction") == "Buy" else -1) == direction
                and (code == w.get("match_code") if w.get("is_option") else code.startswith(str(w.get("match_code") or "\0"))) for w in watches or [])
        if hit:
            out.append({"code": code, "direction": p.get("direction"), "quantity": qty})
    return out


def message(hhmm: str, items: list[dict[str, Any]]) -> tuple[str, str]:
    held = "、".join(f"{i['code']} {'多' if i['direction'] == 'Buy' else '空'} {i['quantity']} 口" for i in items)
    return f"⏰ 當沖倉位還沒平（{hhmm}）", (f"{held}。日盤 13:45 收盤；期貨商通常在收盤前約 15 分鐘（約 13:30）強制沖銷還沒平的當沖倉位，"
                                       "請自行處理（以期貨商規定為準）。")


def check(now: datetime, positions: list[dict[str, Any]], watches: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    """到了提醒時間而且還有當沖倉位就推播，回傳推播的倉位；否則 None。"""
    hhmm = due(now)
    if hhmm is None:
        return None
    items = open_daytrade_positions(positions, watches, daytrade.marks(now))
    if not items:
        return None
    title, msg = message(hhmm, items)
    notify(title, msg, key=f"daytrade:{now.astimezone(daytrade.TAIPEI).date()}:{hhmm}", cooldown=3600, priority="urgent")
    return items


async def reminder_loop(get_positions: Callable[[], list[dict[str, Any]]], get_watches: Callable[[], list[dict[str, Any]]]) -> None:
    """背景：每 20 秒看一次時間。positions／watches 由呼叫端提供（讀快取與記憶體，不查券商）。"""
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            check(daytrade.taipei_now(), get_positions(), get_watches())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("收盤前提醒迴圈例外（已忽略）")
