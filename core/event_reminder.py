"""美國重要事件提醒（update.md 發現 52）：CPI、非農、FOMC 利率決策公布前推播一次。**只通知、不動任何委託、不是進場訊號。**

不管有沒有持倉都提醒（知道今晚有大事本身就有用）；有會留到晚上的持倉（不含登記當沖的）就在訊息裡列出來、用比較高的優先順序。
每則訊息只講事件日分析（發現 50，判定標準事先登記、只跑一次）的事實，不做預測：
  - CPI：公布後第一個日盤的**開盤跳空**是平常的 1.59 倍（兩關都過）；日盤振幅沒有放大。
  - 非農：沒有確認會放大（振幅 1.10 倍、跳空探索期沒過）——只說「沒有確認」。
  - FOMC：日盤振幅沒有放大；開盤跳空探索期 2.05 倍、保留期 1.13 倍**沒確認**（2021～2024 年 1.6～2.4 倍，2025 年起 1.05～1.18 倍）——兩段都照實寫。
時間：CPI／非農在美東 08:30 公布＝台北 20:30（夏令）或 21:30（冬令）；FOMC 聲明 14:00 ET＝台北隔天 02:00／03:00。
**提醒窗口＝事件的美國當地日期當天，台北時間 12:30 起到公布前**（日盤收盤 13:45，12:30 還有時間決定要不要平倉）。每個事件推一次（冷卻 key＝事件 uid，跨重啟）；
如果一開始是沒有持倉的版本、之後才開了倉，會再補一則「你現在有持倉」（只限同一次後端執行期間；重啟後不補）。
事件時間讀 lh-intel 的 `intel.db`（`core/intel_reader.upcoming_events`，唯讀、永遠不丟例外，10 分鐘快取）；讀不到就這次不提醒。訊息不放權益。
`EVENT_REMINDER=false` 關閉。不接進任何策略、風控或下單模組（`tests/test_event_reminder.py` 靜態檢查）。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date, datetime
from typing import Any, Callable, Optional

from core import daytrade, intel_reader
from core.notify import notify
from core.reminders import open_daytrade_positions

logger = logging.getLogger(__name__)

EVENTS_ALL = ("CPI", "NFP", "FOMC")
POLL_SEC = 60.0
EVENTS_TTL = 600.0
START_HHMM = (12, 30)
COOLDOWN = 2 * 86400.0
TAIPEI = intel_reader.TAIPEI

# 發現 50 的結果（update.md；data/event_day_result.json）：反應日（開盤晚於事件的第一個日盤）的開盤跳空與日盤振幅，相對一般日的倍數
STAT = {
    "years": "2021～2026",
    "CPI": {"label": "美國 CPI", "n": 61, "gap_amp": 1.59, "gap_lo": 1.22, "gap_hi": 2.03, "median_event": 154, "median_ctrl": 100, "range_amp": 1.02},
    "NFP": {"label": "美國非農就業", "n": 66, "range_amp": 1.10, "gap_amp": 1.20, "gap_explore": 1.03, "gap_holdout": 1.84},
    "FOMC": {"label": "FOMC 利率決策", "n": 45, "range_amp": 1.04, "gap_explore": 2.05, "gap_holdout": 1.13, "gap_early": "1.6～2.4", "gap_late": "1.05～1.18"},
}


def enabled() -> bool:
    return os.getenv("EVENT_REMINDER", "true").lower() != "false"


def kind_of(ev: dict[str, Any]) -> Optional[str]:
    """lh-intel 的事件 → CPI／NFP／FOMC；不是這三種（或沒有時間）None。"""
    if ev.get("scheduled_time") is None:
        return None
    src, title = ev.get("source"), str(ev.get("title_en") or "")
    if src == "bls" and title == "Consumer Price Index":
        return "CPI"
    if src == "bls" and title == "Employment Situation":
        return "NFP"
    if src == "fed" and title.startswith("FOMC 利率決策"):
        return "FOMC"
    return None


def window_start(ev: dict[str, Any]) -> Optional[float]:
    """提醒窗口的起點＝事件的美國當地日期（`local_date`）當天台北 12:30。CPI／非農與台北同一天；FOMC 在台北隔天凌晨，所以前一天 12:30 就開始。"""
    try:
        d = date.fromisoformat(str(ev.get("local_date") or ev.get("scheduled_date")))
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, START_HHMM[0], START_HHMM[1], tzinfo=TAIPEI).timestamp()


def due_events(now_ts: float, upcoming: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """現在落在『窗口起點 ≤ 現在 < 公布時間』的事件（三種之一）。"""
    out = []
    for ev in upcoming or []:
        if kind_of(ev) is None:
            continue
        start = window_start(ev)
        if start is not None and start <= now_ts < float(ev["scheduled_time"]):
            out.append(ev)
    return out


def carried(positions: Optional[list[dict[str, Any]]], watches: Optional[list[dict[str, Any]]], now: datetime) -> list[dict[str, Any]]:
    """會留到晚上的持倉：有口數、而且不是當沖倉位（當沖倉位收盤前就會被沖掉）。"""
    day = {(i["code"], i["direction"]) for i in open_daytrade_positions(positions or [], watches or [], daytrade.marks(now))}
    out = []
    for p in positions or []:
        qty = int(p.get("quantity", 0) or 0)
        code = str(p.get("code", ""))
        if qty > 0 and code and (code, p.get("direction")) not in day:
            out.append({"code": code, "direction": p.get("direction"), "quantity": qty})
    return out


def when_text(ev_time: float, now_ts: float) -> str:
    """『今晚 21:30』『明天凌晨 03:00』這類相對說法（以台北時間）。"""
    t, now = datetime.fromtimestamp(ev_time, TAIPEI), datetime.fromtimestamp(now_ts, TAIPEI)
    days = (t.date() - now.date()).days
    hhmm = t.strftime("%H:%M")
    if days == 0:
        return f"今晚 {hhmm}" if t.hour >= 18 else f"今天凌晨 {hhmm}" if t.hour < 6 else f"今天 {hhmm}"
    if days == 1:
        return f"明天凌晨 {hhmm}" if t.hour < 6 else f"明天 {hhmm}"
    return f"{t:%m/%d} {hhmm}"


def facts(kind: str) -> str:
    s, e = STAT["years"], STAT[kind]
    if kind == "CPI":
        return (f"{s} 年 {e['n']} 次 CPI 公布後的第一個日盤，開盤離前一日收盤的距離，中位數 {e['median_event']} 點，平常 {e['median_ctrl']} 點（約 {e['gap_amp']:.1f} 倍）；"
                "日盤的振幅沒有明顯變大。")
    if kind == "NFP":
        return f"{s} 年 {e['n']} 次非農公布後的第一個日盤，沒有確認開盤跳空或日盤振幅會比平常大（振幅約 {e['range_amp']:.1f} 倍，跳空沒有穩定放大）。"
    return (f"{s} 年 {e['n']} 次利率決策後的第一個日盤，日盤的振幅沒有放大（約 {e['range_amp']:.2f} 倍）；開盤跳空 2021～2024 年約平常的 {e['gap_early']} 倍、"
            f"2025 年起 {e['gap_late']} 倍，沒有確認。")


def message(kind: str, ev_time: float, now_ts: float, items: list[dict[str, Any]], follow_up: bool = False) -> tuple[str, str]:
    held = "、".join(f"{i['code']} {'多' if i['direction'] == 'Buy' else '空'} {i['quantity']} 口" for i in items)
    title = f"📅 {when_text(ev_time, now_ts)} {STAT[kind]['label']}"
    if items:
        title += "，你現在有持倉" if follow_up else "，你還有持倉"
    body = (f"持有 {held}。" if items else "") + facts(kind) + "這是歷史統計、不是方向預測" + ("；要不要留倉自行決定。" if items else "。")
    return title, body


class EventReminder:
    """推播與狀態：每個事件第一次進窗口時推一則（有持倉的版本帶持倉）；沒有持倉的版本推過、之後才開倉，補一則『你現在有持倉』。"""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}              # uid → "flat"（推的是沒有持倉的版本）／"held"／"seen"（key 已被用掉、不知道是哪種，例如重啟前推過）

    def check(self, now_ts: float, positions: Optional[list[dict[str, Any]]], watches: Optional[list[dict[str, Any]]],
              upcoming: Optional[list[dict[str, Any]]]) -> list[str]:
        sent: list[str] = []
        due = due_events(now_ts, upcoming)
        if not due:
            return sent
        items = carried(positions, watches, datetime.fromtimestamp(now_ts, TAIPEI))
        for ev in due:
            kind, uid, t = kind_of(ev), str(ev.get("uid") or ev["scheduled_time"]), float(ev["scheduled_time"])
            st = self.state.get(uid)
            if st is None:
                title, body = message(kind, t, now_ts, items)
                ok = notify(title, body, key=f"event:{uid}", cooldown=COOLDOWN, priority="high" if items else "default")
                self.state[uid] = ("held" if items else "flat") if ok else "seen"
                if ok:
                    sent.append(kind)
            elif st == "flat" and items:
                title, body = message(kind, t, now_ts, items, follow_up=True)
                if notify(title, body, key=f"event:{uid}:held", cooldown=COOLDOWN, priority="high"):
                    sent.append(kind)
                self.state[uid] = "held"
        return sent


class CachedEvents:
    """lh-intel 的事件，每 EVENTS_TTL 秒才讀一次資料庫（提醒迴圈每分鐘看一次，不需要每次都開檔）。"""

    def __init__(self, fetch: Callable[[float], Optional[list[dict[str, Any]]]], ttl: float = EVENTS_TTL) -> None:
        self.fetch, self.ttl, self.at, self.data = fetch, ttl, -1e18, None

    def get(self, now_ts: float) -> Optional[list[dict[str, Any]]]:
        if now_ts - self.at >= self.ttl or now_ts < self.at:
            data = self.fetch(now_ts)
            self.at = now_ts
            if data is not None or self.data is None:          # 這次讀不到就沿用上一份（資料庫暫時被鎖不該讓提醒消失）
                self.data = data
        return self.data


def _fetch(now_ts: float) -> Optional[list[dict[str, Any]]]:
    return intel_reader.upcoming_events(now_ts, ahead_days=3)


async def event_reminder_loop(get_positions: Callable[[], list[dict[str, Any]]], get_watches: Callable[[], list[dict[str, Any]]]) -> None:
    """positions／watches 由呼叫端提供（讀快取與記憶體，不查券商）。EVENT_REMINDER=false 關閉。"""
    if not enabled():
        logger.info("事件提醒已關閉（EVENT_REMINDER=false）")
        return
    reminder, events = EventReminder(), CachedEvents(_fetch)

    def run() -> None:
        now = time.time()
        reminder.check(now, get_positions(), get_watches(), events.get(now))

    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await asyncio.to_thread(run)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("事件提醒迴圈例外（已忽略）")
