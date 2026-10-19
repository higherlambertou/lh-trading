"""CPI 提醒（update.md 發現 52）：美國 CPI 公布當天，如果還有持倉（不是當沖、收盤前會被沖掉的那種），推播提醒「隔天日盤開盤的跳空平常的約 1.6 倍」。**只通知、不動任何委託、不是進場訊號。**

依據：事件日分析（發現 50，判定標準事先登記、只跑一次）——CPI 公布後的第一個日盤，開盤離前一日收盤的距離（跳空）是平常的 1.59 倍（95% 區間 1.22～2.03，兩關都過）；
**日盤振幅沒有放大**（1.02 倍，沒過）。所以這則提醒只說跳空、不說「那天會大波動」。FOMC（探索期 2.05 倍、保留期 1.13 倍沒確認）與非農（沒過）沒有可以說的結論，不提醒。
時間：CPI 在美東 08:30 公布＝台北 20:30（夏令）或 21:30（冬令），與台北同一天。公布當天台北時間 12:30 起到公布前，只要有持倉就提醒一次（每個事件一次，冷卻 key 跨重啟）；
12:30 之後才開的倉也會在下一輪（最多 60 秒）提醒到。日盤收盤 13:45，12:30 給你還有時間決定要不要平倉。
訊息不放權益。事件時間讀 lh-intel 的 `intel.db`（`core/intel_reader.upcoming_events`，唯讀、永遠不丟例外）；讀不到就這次不提醒。
`EVENT_REMINDER=false` 關閉。不接進任何策略、風控或下單模組（`tests/test_event_reminder.py` 靜態檢查）。"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Callable, Optional

from core import daytrade, intel_reader
from core.notify import notify
from core.reminders import open_daytrade_positions

logger = logging.getLogger(__name__)

POLL_SEC = 60.0
START_HHMM = (12, 30)
SOURCE, TITLE_EN = "bls", "Consumer Price Index"
# 發現 50 的結果（update.md；data/event_day_result.json）：CPI 反應日的開盤跳空 vs 一般日
STAT = {"n": 61, "amp": 1.59, "lo": 1.22, "hi": 2.03, "median_event": 154, "median_ctrl": 100, "range_amp": 1.02, "years": "2021～2026"}
TAIPEI = intel_reader.TAIPEI


def enabled() -> bool:
    return os.getenv("EVENT_REMINDER", "true").lower() != "false"


def is_cpi(ev: dict[str, Any]) -> bool:
    return ev.get("source") == SOURCE and ev.get("title_en") == TITLE_EN and ev.get("scheduled_time") is not None


def due_event(now_ts: float, upcoming: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """現在落在『CPI 公布當天（台北日期）12:30 ～ 公布時間』之內的那個事件；沒有就 None。"""
    now = datetime.fromtimestamp(now_ts, TAIPEI)
    start = now.replace(hour=START_HHMM[0], minute=START_HHMM[1], second=0, microsecond=0)
    for ev in upcoming or []:
        if not is_cpi(ev):
            continue
        t = float(ev["scheduled_time"])
        if datetime.fromtimestamp(t, TAIPEI).date() == now.date() and start <= now and now_ts < t:
            return ev
    return None


def carried(positions: list[dict[str, Any]], watches: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """會留到晚上的持倉：有口數、而且不是當沖倉位（當沖倉位收盤前就會被沖掉）。"""
    day = {(i["code"], i["direction"]) for i in open_daytrade_positions(positions, watches, daytrade.marks(now))}
    out = []
    for p in positions or []:
        qty = int(p.get("quantity", 0) or 0)
        code = str(p.get("code", ""))
        if qty > 0 and code and (code, p.get("direction")) not in day:
            out.append({"code": code, "direction": p.get("direction"), "quantity": qty})
    return out


def message(ev: dict[str, Any], items: list[dict[str, Any]]) -> tuple[str, str]:
    hhmm = datetime.fromtimestamp(float(ev["scheduled_time"]), TAIPEI).strftime("%H:%M")
    held = "、".join(f"{i['code']} {'多' if i['direction'] == 'Buy' else '空'} {i['quantity']} 口" for i in items)
    s = STAT
    return (f"📅 今晚 {hhmm} 美國 CPI，你還有持倉",
            f"{held}。{s['years']} 年 {s['n']} 次 CPI 公布後的第一個日盤，開盤離前一日收盤的距離，中位數 {s['median_event']} 點，平常 {s['median_ctrl']} 點（約 {s['amp']:.1f} 倍）；"
            f"日盤的振幅沒有明顯變大。這是歷史統計、不是方向預測；要不要留倉自行決定。")


def check(now_ts: float, positions: list[dict[str, Any]], watches: list[dict[str, Any]], get_events: Callable[[float], Optional[list[dict[str, Any]]]]) -> Optional[dict[str, Any]]:
    """該提醒就推播並回傳事件；否則 None。沒有持倉或還沒到 12:30 時不去讀資料庫。"""
    now = datetime.fromtimestamp(now_ts, TAIPEI)
    if (now.hour, now.minute) < START_HHMM or not positions:
        return None
    items = carried(positions, watches, now)
    if not items:
        return None
    ev = due_event(now_ts, get_events(now_ts) or [])
    if ev is None:
        return None
    title, msg = message(ev, items)
    notify(title, msg, key=f"cpi:{ev.get('uid') or ev['scheduled_time']}", cooldown=2 * 86400.0, priority="high")
    return ev


async def event_reminder_loop(get_positions: Callable[[], list[dict[str, Any]]], get_watches: Callable[[], list[dict[str, Any]]]) -> None:
    """positions／watches 由呼叫端提供（讀快取與記憶體，不查券商）。EVENT_REMINDER=false 關閉。"""
    if not enabled():
        logger.info("CPI 提醒已關閉（EVENT_REMINDER=false）")
        return
    import time
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            positions = get_positions()
            if positions:
                await asyncio.to_thread(check, time.time(), positions, get_watches(), intel_reader.upcoming_events)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("CPI 提醒迴圈例外（已忽略）")
