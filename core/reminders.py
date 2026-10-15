"""收盤前提醒（update.md 發現 34）：當沖倉位還沒平，在收盤前推播提醒。只通知、不動任何委託。

為什麼：期貨商通常在收盤前約 15 分鐘（日盤約 13:30）強制沖銷還沒沖掉的當沖倉位（見 core/daytrade.py；細節以期貨商規定為準）。
怎麼知道哪些是當沖倉位：手動下單勾了當沖（倉別 DayTrade）時登記（daytrade.note_open）；停損停利監控的 octype＝DayTrade 也算（有落地，重啟後還在）。
券商的持倉資料不帶倉別，所以「勾了當沖但後端重啟過又沒掛停損停利」的部位認不出來——這是已知的限制。
提醒時間：13:15、13:25（週一～五，台北時間）；每個時間點只推一次。

合約到期提醒（update.md 發現 37）：持有的期貨／月選擇權在最後交易日（每月第三個週三 13:30；遇休市順延）前提醒，只通知。
三個時間點，各只推一次：到期前 1～3 天的 09:00 之後（提早通知）、最後交易日 08:50 之後、最後交易日 12:30 之後（最後機會，緊急）。
代碼解析：期貨 TMF／MXF／TXF＋月份字母（A～L＝1～12 月）＋年份個位數；月選擇權 TXO＋履約價＋月份字母（買權 A～L、賣權 M～X）＋年份個位數；
週選擇權等認不得的格式一律不提醒（寧可不提醒也不要提醒錯日期）。休市日用證交所公開日曆（core/premarket_log.TwCalendar）。"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
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


# ── 合約到期提醒 ────────────────────────────────────────────────

_FUT_RE = re.compile(r"^(?:TMF|MXF|TXF)([A-L])(\d)$")
_OPT_RE = re.compile(r"^TXO\d+([A-X])(\d)$")
_WD_ZH = "一二三四五六日"


def code_month(code: str, today: date) -> Optional[tuple[int, int, bool]]:
    """合約代碼 → (年, 月, 是不是期貨)；認不得的格式回 None。年份個位數取「今年−1 年起」最近的一年（合約不會離現在太遠）。"""
    c = (code or "").upper()
    m = _FUT_RE.match(c)
    is_fut = m is not None
    if m is None:
        m = _OPT_RE.match(c)
    if m is None:
        return None
    letter, digit = m.group(1), int(m.group(2))
    month = (ord(letter) - ord("A")) % 12 + 1          # 買權 A～L、賣權 M～X 都是 1～12 月
    base = today.year - 1
    return base + (digit - base) % 10, month, is_fut


def third_wednesday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(2 - d.weekday()) % 7 + 14)


def expiry_date(year: int, month: int, is_open: Callable[[date], bool]) -> date:
    """最後交易日＝第三個週三；那天休市就順延到下一個交易日（最多往後找 10 天）。"""
    d = third_wednesday(year, month)
    for _ in range(10):
        if is_open(d):
            return d
        d += timedelta(days=1)
    return third_wednesday(year, month)


def expiring(positions: list[dict[str, Any]], today: date, is_open: Callable[[date], bool], within_days: int = 3) -> dict[date, list[dict[str, Any]]]:
    """最後交易日在 today ～ today＋within_days 之內的持倉，依到期日分組。"""
    out: dict[date, list[dict[str, Any]]] = {}
    cache: dict[tuple[int, int], date] = {}
    for p in positions or []:
        qty = int(p.get("quantity", 0) or 0)
        ym = code_month(str(p.get("code", "")), today)
        if qty <= 0 or ym is None:
            continue
        y, mo, is_fut = ym
        e = cache.setdefault((y, mo), expiry_date(y, mo, is_open))
        if 0 <= (e - today).days <= within_days:
            out.setdefault(e, []).append({"code": str(p.get("code")), "direction": p.get("direction"), "quantity": qty, "is_future": is_fut})
    return out


def expiry_stage(now: datetime, days: int) -> Optional[str]:
    """到期前還有幾天（日曆天）＋現在時間 → 要發哪一則；沒有就 None。"""
    t = now.astimezone(daytrade.TAIPEI)
    m = t.hour * 60 + t.minute
    if days == 0:
        if 12 * 60 + 30 <= m < 13 * 60 + 30:
            return "final"
        if 8 * 60 + 50 <= m < 12 * 60 + 30:
            return "today"
        return None
    if 1 <= days <= 3 and m >= 9 * 60:
        return "early"
    return None


def expiry_message(stage: str, e: date, items: list[dict[str, Any]], now: datetime) -> tuple[str, str]:
    held = "、".join(f"{i['code']} {'多' if i['direction'] == 'Buy' else '空'} {i['quantity']} 口" for i in items)
    wd = _WD_ZH[e.weekday()]
    tail = "最後交易日 13:30 收盤後以最終結算價現金結算；要續抱請在那之前平倉、改持下個月合約（以期貨商規定為準）。"
    if any(i["is_future"] for i in items):
        tail += "結算後建議重啟後端：程式只在啟動時解析近月合約並訂閱，不重啟會停在舊合約（結算當天的實際行為沒有實測過）。"
    if stage == "final":
        t = now.astimezone(daytrade.TAIPEI)
        left = max(0, 13 * 60 + 30 - (t.hour * 60 + t.minute))
        return f"⏰ 合約今天 13:30 結算（還剩約 {left} 分鐘）", f"{held}。收盤後無法再平倉，會以最終結算價現金結算。"
    if stage == "today":
        return f"📅 今天是合約最後交易日（{e:%m/%d}（{wd}））", f"{held}。{tail}"
    return f"📅 合約 {e:%m/%d}（{wd}）13:30 結算，你還有持倉", f"{held}。{tail}"


def check_expiry(now: datetime, positions: list[dict[str, Any]], is_open: Callable[[date], bool]) -> list[tuple[date, str, list[dict[str, Any]]]]:
    """該提醒的就推播，回傳 [(到期日, 階段, 持倉)]。同一個到期日同一階段只推一次（冷卻 key 跨重啟）。"""
    t = now.astimezone(daytrade.TAIPEI)
    sent = []
    for e, items in sorted(expiring(positions, t.date(), is_open).items()):
        stage = expiry_stage(now, (e - t.date()).days)
        if stage is None:
            continue
        title, msg = expiry_message(stage, e, items, now)
        cool = 5 * 86400.0 if stage == "early" else 86400.0
        notify(title, msg, key=f"expiry:{e.isoformat()}:{stage}", cooldown=cool, priority="urgent" if stage == "final" else "high")
        sent.append((e, stage, items))
    return sent


_calendar: Any = None


def _is_open(d: date) -> bool:
    """證交所休市日曆（沿用盤前紀錄的那個，查不到退回『平日』）。可能做網路請求，只能在執行緒裡呼叫。"""
    global _calendar
    if _calendar is None:
        from core.premarket_log import TwCalendar
        _calendar = TwCalendar()
    return bool(_calendar.check(d)["open"])


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
        try:
            positions = get_positions()
            if positions:                       # 沒有持倉就不用算（日曆可能要上網，丟到執行緒）
                await asyncio.to_thread(check_expiry, daytrade.taipei_now(), positions, _is_open)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("合約到期提醒例外（已忽略）")
