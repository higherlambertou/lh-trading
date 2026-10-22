"""傍晚摘要（update.md 發現 68）：每個交易日一則 Telegram，給夜盤用。只通知、不影響任何交易決策。

內容：今晚夜盤預期振幅與下一個日盤預期振幅（core/precheck.evening，發現 67／55）、今天日盤實際走多少 vs 早上的預期（core/forecast_log）、
36 小時內的 CPI／非農／FOMC（與下單前檢查同一份）、預測命中率。訊息不放權益。
時間：EVENING_BRIEF_TIME（預設 1620；期交所約 16:10 公布當天的臺指 VIX、lh-intel 約 16:15 抓到）以後；夜盤預測還沒好就等，
到 WAIT_UNTIL（17:30）還沒有就照發、寫明原因；21:00 以後不補。一天一則（冷卻 key 跨重啟）。EVENING_BRIEF=false 關閉；模擬盤不推播。"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from core.daytrade import TAIPEI
from core.notify import notify, sim_blocked

logger = logging.getLogger(__name__)

POLL_SEC = 60.0
WAIT_UNTIL = 17 * 60 + 30
WINDOW_END = 21 * 60
_WD_ZH = "一二三四五六日"


def enabled() -> bool:
    return os.getenv("EVENING_BRIEF", "true").lower() != "false"


def brief_minute() -> int:
    raw = os.getenv("EVENING_BRIEF_TIME", "1620").strip().replace(":", "")
    try:
        h, m = divmod(int(raw), 100)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except ValueError:
        pass
    return 16 * 60 + 20


def _n0(x: Any) -> str:
    return "—" if x is None else f"{x:,.0f}"


def today_vs_forecast(db_path: Path, today: str, bars: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """（同步）早上的日盤預測與今天實際的高低差；沒有預測回 None。"""
    from core import forecast_log as fl
    try:
        with closing(fl._conn(db_path)) as c:
            r = c.execute("SELECT lo, mid, hi FROM forecast_log WHERE kind='day' AND target=?", (today,)).fetchone()
    except sqlite3.Error:
        return None
    if not r:
        return None
    return {"lo": r[0], "mid": r[1], "hi": r[2], "actual": fl.realized_day(bars, today)}


def compose(d: dict[str, Any]) -> tuple[str, str]:
    lines: list[str] = []
    ev = d.get("evening") or {}
    night, nxt = ev.get("night") or {}, ev.get("next_day") or {}
    if night.get("expect"):
        e = night["expect"]
        lines.append(f"🌙 今晚夜盤預期振幅約 {_n0(e['mid'])} 點（68% 區間 {_n0(e['lo'])}～{_n0(e['hi'])}；近 20 個夜盤平均 {_n0(night.get('r20n'))}）")
    else:
        lines.append(f"🌙 今晚夜盤預期振幅：{night.get('note') or ev.get('error') or '還沒有'}")
    if nxt.get("expect"):
        e = nxt["expect"]
        lines.append(f"下一個日盤預期振幅約 {_n0(e['mid'])} 點（68% 區間 {_n0(e['lo'])}～{_n0(e['hi'])}）")
    tv = d.get("today")
    if tv and tv.get("actual") is not None:
        where = "落在區間內" if tv["lo"] <= tv["actual"] <= tv["hi"] else ("比區間大" if tv["actual"] > tv["hi"] else "比區間小")
        lines.append(f"今天日盤實際走了 {_n0(tv['actual'])} 點；早上預期約 {_n0(tv['mid'])} 點（68% 區間 {_n0(tv['lo'])}～{_n0(tv['hi'])}）→ {where}")
    events = d.get("events")
    if events is None:
        lines.append("事件：讀不到 lh-intel 的事件行事曆")
    elif events:
        lines += [f"📅 {e['when']} {e['label']}：{e['note']}" for e in events]
    else:
        lines.append("36 小時內沒有美國重要事件（CPI、非農、FOMC）")
    hits = d.get("hits") or {}
    mn = hits.get("min_show", 5)
    parts = []
    for k, unit, name in (("day", "天", "日盤"), ("night", "晚", "夜盤")):
        h = hits.get(k) or {}
        if h.get("n", 0) >= mn:
            parts.append(f"{name}近 {h['n']} {unit}有 {h['inside']} {unit}落在區間內")
    if parts:
        lines.append("預測驗證：" + "、".join(parts) + "（模型說應約 68%）")
    lines.append("這是會走多遠的尺度，不是方向訊號。")
    dd = date.fromisoformat(d["date"])
    return f"🌆 傍晚摘要 {dd.month}/{dd.day}（{_WD_ZH[dd.weekday()]}）", "\n".join(lines)


class EveningBrief:
    def __init__(self) -> None:
        self.sent_for: Optional[str] = None

    async def run_once(self, now: datetime, gather: Callable[[float], dict[str, Any]], is_open: Callable[[date], bool]) -> Optional[str]:
        """到點就組並推播；回傳 "sent"／"closed"／"sim"／"waiting"，還沒到點或已處理回 None。"""
        t = now.astimezone(TAIPEI)
        today = t.date().isoformat()
        m = t.hour * 60 + t.minute
        if self.sent_for == today or t.weekday() >= 5 or not (brief_minute() <= m < WINDOW_END):
            return None
        if sim_blocked():
            self.sent_for = today
            return "sim"
        if not await asyncio.to_thread(is_open, t.date()):
            self.sent_for = today
            return "closed"
        d = await asyncio.to_thread(gather, now.timestamp())
        night = ((d.get("evening") or {}).get("night") or {})
        if not night.get("expect") and m < WAIT_UNTIL:
            return "waiting"                                                             # 臺指 VIX 還沒進來：等一下再發
        title, body = compose({**d, "date": today})
        self.sent_for = today
        notify(title, body, key=f"evening:{today}", cooldown=20 * 3600.0, priority="default")
        return "sent"


evening_brief = EveningBrief()


def gather_all(now: float, store: Any) -> dict[str, Any]:
    """（同步、讀資料庫；丟執行緒）傍晚摘要要的全部資料。任何一塊讀不到就那一塊空著。"""
    from core import forecast_log, precheck
    today = datetime.fromtimestamp(now, TAIPEI).date().isoformat()
    out: dict[str, Any] = {"evening": precheck.fetch_evening(now, store), "events": precheck.fetch_events(now)}
    try:
        out["today"] = today_vs_forecast(store.path, today, store.bars_1m("TXF", today))
    except Exception:  # noqa: BLE001
        out["today"] = None
    out["hits"] = forecast_log.summary(store.path)
    return out


async def evening_loop(store: Any, is_open: Callable[[date], bool]) -> None:
    """is_open（台股休市日曆）由 main.py 傳進來。"""
    if not enabled():
        logger.info("傍晚摘要已關閉（EVENING_BRIEF=false）")
        return
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await evening_brief.run_once(datetime.now(TAIPEI), lambda now: gather_all(now, store), is_open)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("傍晚摘要迴圈例外（已忽略）")
