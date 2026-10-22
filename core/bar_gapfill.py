"""走勢圖缺口的回補（update.md 發現 66）：後端沒在記錄的那幾分鐘（重啟、斷線），收盤後用券商的歷史 1 分 K 補畫。只給走勢圖用。

為什麼：走勢圖的 K 棒來自後端自己每分鐘記的 flow_1m（真實成交＋外內盤）；後端沒開著或報價斷了的分鐘就是空的
（10/21 結算日 10:55～14:46 整段空白）。券商的 1 分 K 只有開高低收量、沒有外內盤，所以**存在另一張表 bars_fill_1m**：
flow_1m 不動、研究與指標都不讀它，走勢圖只在 flow_1m 沒有那一分鐘時才拿來補（畫成灰色）。
什麼時候：只有正式盤跑（模擬盤不重複查）；平日 13:50～14:50 與 08:00～08:40（券商的歷史 K 棒查詢只在盤前／盤後用）檢查最近 LOOKBACK 個平日的
日盤與夜盤（已經結束的）：flow_1m＋已回補的加起來幾乎是滿的就不查，否則一段查一次券商 1 分 K、只補缺的分鐘。每段在同一個行程裡最多試 MAX_TRIES 次。
GAPFILL=false 關閉。
時間對齊：券商 K 棒的時間是「台灣時間當成 UTC」、而且標在那一分鐘的**結束**（日盤第一根標 08:46），flow_1m 標分鐘的**起點**——所以起點＝結束 − 60 秒。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Sequence

from core.daytrade import TAIPEI
from core.hurst_analyzer import ts_to_local

logger = logging.getLogger(__name__)

CODE = "TMF"
POLL_SEC = 300.0
MAX_TRIES = 3
LOOKBACK = 3                           # 往回看幾個平日（10/21 的洞，隔天收盤後就會補上）
FULL_SLACK = 2                         # 少於這麼多分鐘的缺口就不查（可能只是那一分鐘真的沒成交）
DAY_RUN = (13 * 60 + 50, 14 * 60 + 50)
NIGHT_RUN = (8 * 60, 8 * 60 + 40)


def enabled() -> bool:
    return os.getenv("GAPFILL", "true").lower() != "false" and os.getenv("SIMULATION", "false").lower() != "true"


def _epoch(d: date, hh: int, mm: int) -> float:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TAIPEI).timestamp()


def day_window(d: date) -> dict[str, Any]:
    return {"key": f"day:{d}", "label": f"{d} 日盤", "start": _epoch(d, 8, 45), "end": _epoch(d, 13, 45),
            "kb_start": d.isoformat(), "kb_end": d.isoformat(), "expected": 300}


def night_window(d: date) -> dict[str, Any]:
    """d 那天 15:00 到隔天 05:00 的夜盤。"""
    nxt = d + timedelta(days=1)
    return {"key": f"night:{d}", "label": f"{d} 夜盤", "start": _epoch(d, 15, 0), "end": _epoch(nxt, 5, 0),
            "kb_start": d.isoformat(), "kb_end": nxt.isoformat(), "expected": 840}


def windows_due(now: datetime) -> list[dict[str, Any]]:
    """現在該檢查的段落（最近 LOOKBACK 個平日、已經結束的日盤與夜盤，新的在前）；不在盤前／盤後的執行時段就是空的。
    每段：{"key", "label", "start", "end"（分鐘起點的 epoch，左閉右開）, "kb_start", "kb_end"（kbars 的日期）, "expected"}。"""
    now = now.astimezone(TAIPEI)
    m, today = now.hour * 60 + now.minute, now.date()
    if today.weekday() >= 5 or not (DAY_RUN[0] <= m < DAY_RUN[1] or NIGHT_RUN[0] <= m < NIGHT_RUN[1]):
        return []
    days, d = [], today
    while len(days) < LOOKBACK:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    ts_now = now.timestamp()
    out = []
    for d in days:
        for w in (day_window(d), night_window(d)):
            if w["end"] <= ts_now:
                out.append(w)
    return sorted(out, key=lambda w: -w["start"])


def kbar_minutes(raw: dict[str, Sequence[float]]) -> list[tuple[int, float, float, float, float, int]]:
    """券商 1 分 K（平行陣列）→ [(分鐘起點 epoch, 開, 高, 低, 收, 量)]；價格不完整的丟掉。"""
    out = []
    for ts, o, h, l, c, v in zip(raw.get("ts", []), raw.get("open", []), raw.get("high", []), raw.get("low", []), raw.get("close", []), raw.get("volume", [])):
        if not (o and h and l and c):
            continue
        end_local = ts_to_local(ts)                                                     # naive 的台灣當地時間（K 棒結束）
        start = end_local.replace(tzinfo=TAIPEI).timestamp() - 60
        out.append((int(start), float(o), float(h), float(l), float(c), int(v or 0)))
    return out


def missing(have: set[int], bars: list[tuple[int, float, float, float, float, int]], start: float, end: float) -> list[tuple[int, float, float, float, float, int]]:
    """券商有、flow_1m 沒有、而且在這一段裡的分鐘。"""
    return [b for b in bars if start <= b[0] < end and b[0] not in have]


class GapFiller:
    def __init__(self) -> None:
        self.done: set[str] = set()
        self.tries: dict[str, int] = {}

    async def run_once(self, now: datetime, store: Any, kbars: Callable[[str, str, str], Awaitable[dict]], connected: bool) -> Optional[int]:
        """到時間就檢查最近幾段、需要就補；回傳這次補了幾根（都不需要＝0），不在執行時段或沒連上回 None。"""
        ws = windows_due(now)
        if not ws or not connected:
            return None
        total = 0
        for w in ws:
            if w["key"] in self.done or self.tries.get(w["key"], 0) >= MAX_TRIES:
                continue
            flow = await asyncio.to_thread(store.flow_minutes, CODE, w["start"], w["end"])
            filled = await asyncio.to_thread(store.fill_minutes, CODE, w["start"], w["end"])
            if len(flow | filled) >= w["expected"] - FULL_SLACK:
                self.done.add(w["key"])
                continue
            self.tries[w["key"]] = self.tries.get(w["key"], 0) + 1
            try:
                raw = await kbars(CODE, w["kb_start"], w["kb_end"])
            except Exception as e:  # noqa: BLE001
                logger.warning("走勢圖回補：%s 查券商 1 分 K 失敗（第 %d 次）：%r", w["label"], self.tries[w["key"]], e)
                continue
            rows = missing(flow | filled, kbar_minutes(raw or {}), w["start"], w["end"])
            n = await asyncio.to_thread(store.upsert_fill_1m, CODE, rows, time.time())
            self.done.add(w["key"])
            total += n
            logger.info("走勢圖回補：%s 後端記到 %d 分鐘，用券商 1 分 K 補了 %d 根", w["label"], len(flow), n)
        return total


async def gapfill_loop(store: Any, kbars: Callable[[str, str, str], Awaitable[dict]], connected: Callable[[], bool]) -> None:
    if not enabled():
        logger.info("走勢圖回補：%s", "模擬盤不跑（由正式盤負責）" if os.getenv("SIMULATION", "false").lower() == "true" else "已關閉（GAPFILL=false）")
        return
    filler = GapFiller()
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await filler.run_once(datetime.now(TAIPEI), store, kbars, connected())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("走勢圖回補迴圈例外（已忽略）")
