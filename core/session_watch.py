"""券商連線假死偵測（update.md 發現 65）。只讀記憶體、不下單、不查券商。

事故：2026-10-19（週一）08:45～10:04、10-21（結算日）10:55～14:46，永豐的 Solace session 斷了——查詢一直回 SessionNotEstablished、
期貨報價完全停止——但 worker 行程還在、broker.is_connected 仍是 True，/api/health 說正常，watchdog 就不重啟。那段時間畫面是舊價格、
部位不更新、手動停損監控不會觸發（沒有新報價）。10/19 是手動重啟 watchdog 才恢復，10/21 是換月重啟順便修好。

判斷（任一成立＝假死）：
  A1 連線錯誤：查詢連續 ≥ ERR_MIN_COUNT 次回連線錯誤（Session error／SessionNotEstablished／NotReady），從第一次起 ≥ ERR_MIN_SEC 秒，
     中間沒有任何成功的回覆（其他錯誤不算、也不打斷）；
  A2 worker 不回話：送出 ≥ ERR_MIN_COUNT 個查詢、最早那個已經 ≥ SILENT_SEC 秒，worker 一個回覆（成功或失敗）都沒有；
  B  報價停止：日盤（08:46～13:44）訂閱了期貨卻 ≥ DAY_QUIET_SEC 秒沒有任何報價；夜盤（15:01～04:59）今晚收過報價、
     之後 ≥ NIGHT_QUIET_SEC 秒沒有（今晚一筆都沒收過就不判斷：可能根本沒有夜盤）。2026-10-07～22 的 ticks 裡，正常盤中沒有超過 60 秒的空檔
     （那幾次 140～150 秒的都是後端重啟）。沒報價的時間從「連上（或重新連上）」與「日盤開盤 08:45」中較晚的那一刻起算，剛連上不會誤判。
只在「應該連得上」的時段判斷：交易日 08:00～13:50、交易日晚上 14:50～隔天 05:00。週末、休市日、每天 05:00～08:00（券商維護）不判斷——
那時候重啟也連不上，只會白白燒登入次數。休市日由 main.py 注入台股休市日曆的查詢函式（在執行緒裡查、一天一次）；沒注入或查不到就當平日。
結果交給 /api/health：broker_connected=False、session=stale、stale_reason＝原因 → watchdog 連續 2 次（約 30 秒）就正常關閉並重啟
（run_live.sh／run_sim.sh 的 stale 分支；舊版 watchdog 會當成 nobroker，一樣會重啟）。SESSION_WATCH=false 關閉。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from core.daytrade import TAIPEI

logger = logging.getLogger(__name__)

ERR_PATTERN = re.compile(r"Session error|SessionNotEstablished|code: NotReady")
ERR_MIN_COUNT = 3
ERR_MIN_SEC = 60.0
SILENT_SEC = 90.0
DAY_QUIET_SEC = 180.0
NIGHT_QUIET_SEC = 300.0
POLL_SEC = 15.0
CAL_RETRY_SEC = 600.0


def short_error(e: str) -> str:
    """「list_positions: Shioaji error Session error … SubCode(SessionNotEstablished) …」→「list_positions: SessionNotEstablished」。"""
    method = e.split(":", 1)[0].strip() if ":" in e else ""
    m = re.search(r"SubCode\((\w+)\)", e) or re.search(r"code: (\w+)", e)
    tail = m.group(1) if m else e[:60]
    return f"{method}: {tail}" if method and len(method) <= 40 and method != tail else tail


def enabled() -> bool:
    return os.getenv("SESSION_WATCH", "true").lower() != "false"


def _minute(now: datetime) -> int:
    return now.hour * 60 + now.minute


def live_window(now: datetime, is_open: Callable[[date], Optional[bool]]) -> Optional[str]:
    """現在是不是『應該連得上券商』的時段：'day'（交易日 08:00～13:50）、'night'（交易日晚上 14:50～隔天 05:00）或 None。
    is_open(d) 回 None（不知道）就當平日。"""
    def open_(d: date) -> bool:
        v = is_open(d)
        return d.weekday() < 5 if v is None else bool(v)
    now = now.astimezone(TAIPEI)
    m, d = _minute(now), now.date()
    if 8 * 60 <= m < 13 * 60 + 50:
        return "day" if open_(d) else None
    if m >= 14 * 60 + 50:
        return "night" if open_(d) else None
    if m < 5 * 60:
        return "night" if open_(d - timedelta(days=1)) else None
    return None


def weekend_quiet(now: datetime) -> bool:
    """週六 05:30 以後、週日、週一 07:30 以前：沒有任何盤，帳務查詢可以放慢（部位不會變）。不看休市日曆（判斷錯了只會讓平日的部位刷新變慢，不值得冒險）。"""
    now = now.astimezone(TAIPEI)
    m, wd = _minute(now), now.weekday()
    return (wd == 5 and m >= 5 * 60 + 30) or wd == 6 or (wd == 0 and m < 7 * 60 + 30)


class SessionWatch:
    """broker 的事件鉤子（reader 執行緒）與 _acall（event loop）會更新這裡；check() 在背景迴圈裡判斷，/api/health 只讀 stale_reason。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset(None)
        self.stale_reason: Optional[str] = None
        self.stale_since: Optional[float] = None

    def reset(self, t: Optional[float]) -> None:
        with self._lock:
            self.connected_at = t
            self.last_ok: Optional[float] = None
            self.err_n = 0
            self.err_since: Optional[float] = None
            self.last_err = ""
            self.unanswered_n = 0
            self.unanswered_since: Optional[float] = None
            self.last_quote: Optional[float] = None

    # ── 鉤子（要快、永不丟例外）─────────────────────────────────────
    def on_connected(self, t: float) -> None:
        self.reset(t)

    def on_request(self, t: float) -> None:
        with self._lock:
            self.unanswered_n += 1
            if self.unanswered_since is None:
                self.unanswered_since = t

    def on_response(self, t: float, error: Optional[str]) -> None:
        with self._lock:
            self.unanswered_n, self.unanswered_since = 0, None
            if error is None:
                self.last_ok, self.err_n, self.err_since = t, 0, None
            elif ERR_PATTERN.search(error):
                self.err_n += 1
                if self.err_since is None:
                    self.err_since = t
                self.last_err = short_error(error)

    def on_quote(self, t: float) -> None:
        self.last_quote = t

    # ── 判斷 ──────────────────────────────────────────────────────
    def evaluate(self, now_ts: float, now: datetime, window: Optional[str], has_subs: bool) -> Optional[str]:
        """假死的原因（給人看的一句話）或 None。純計算，不改狀態。"""
        if window is None or self.connected_at is None:
            return None
        with self._lock:
            err_n, err_since, last_ok, last_err = self.err_n, self.err_since, self.last_ok, self.last_err
            un_n, un_since, last_quote, conn = self.unanswered_n, self.unanswered_since, self.last_quote, self.connected_at
        if err_n >= ERR_MIN_COUNT and err_since is not None and now_ts - err_since >= ERR_MIN_SEC and (last_ok is None or last_ok < err_since):
            return f"券商查詢連續 {err_n} 次連線錯誤、{int(now_ts - err_since)} 秒沒有成功（最近一次：{last_err}）"
        if un_n >= ERR_MIN_COUNT and un_since is not None and now_ts - un_since >= SILENT_SEC:
            return f"worker {int(now_ts - un_since)} 秒沒有回覆任何查詢（{un_n} 個在等）"
        if not has_subs:
            return None
        now = now.astimezone(TAIPEI)
        m = _minute(now)
        if window == "day" and 8 * 60 + 46 <= m <= 13 * 60 + 44:
            open_ts = now.replace(hour=8, minute=45, second=0, microsecond=0).timestamp()
            ref = max(last_quote or 0.0, conn, open_ts)
            if now_ts - ref >= DAY_QUIET_SEC:
                return f"日盤中 {int(now_ts - ref)} 秒沒有收到任何報價"
        elif window == "night" and (m >= 15 * 60 + 1 or m <= 4 * 60 + 59):
            day0 = now.date() if m >= 15 * 60 else now.date() - timedelta(days=1)
            night_ts = datetime(day0.year, day0.month, day0.day, 15, 0, tzinfo=TAIPEI).timestamp()
            if last_quote is not None and last_quote >= night_ts and now_ts - max(last_quote, conn) >= NIGHT_QUIET_SEC:
                return f"夜盤中 {int(now_ts - max(last_quote, conn))} 秒沒有收到報價"
        return None

    def check(self, now_ts: float, now: datetime, window: Optional[str], has_subs: bool) -> Optional[str]:
        """判斷並更新 stale_reason（狀態改變時記一行 log）。"""
        reason = self.evaluate(now_ts, now, window, has_subs)
        if reason and not self.stale_reason:
            self.stale_since = now_ts
            logger.error("券商連線假死：%s → /api/health 回報 stale，watchdog 會正常關閉並重啟", reason)
        elif not reason and self.stale_reason:
            logger.info("券商連線恢復（假死狀態解除，持續 %.0f 秒）", now_ts - (self.stale_since or now_ts))
            self.stale_since = None
        self.stale_reason = reason
        return reason

    def snapshot(self, now_ts: Optional[float] = None) -> dict[str, Any]:
        now_ts = time.time() if now_ts is None else now_ts
        age = lambda t: None if t is None else round(now_ts - t, 1)          # noqa: E731
        return {"enabled": enabled(), "stale_reason": self.stale_reason, "connected_age": age(self.connected_at), "last_ok_age": age(self.last_ok),
                "last_quote_age": age(self.last_quote), "err_n": self.err_n, "err_age": age(self.err_since), "unanswered_n": self.unanswered_n,
                "unanswered_age": age(self.unanswered_since), "last_err": self.last_err}


session_watch = SessionWatch()


# ── 休市日（一天查一次，在執行緒裡）──────────────────────────────

class TradingDays:
    def __init__(self, check: Optional[Callable[[date], Any]] = None) -> None:
        self._check = check
        self._known: dict[date, Optional[bool]] = {}
        self._retry_at: dict[date, float] = {}

    def is_open(self, d: date) -> Optional[bool]:
        return self._known.get(d)

    async def ensure(self, days: list[date], now_ts: float) -> None:
        """還不知道的日子在執行緒裡查一次；查不到 CAL_RETRY_SEC 秒後再試（這段期間當平日）。"""
        for d in days:
            if d in self._known and self._known[d] is not None:
                continue
            if now_ts < self._retry_at.get(d, 0.0):
                continue
            if self._check is None:                       # 沒有日曆：當平日
                return
            try:
                res = await asyncio.to_thread(self._check, d)
                self._known[d] = bool(res["open"]) if isinstance(res, dict) else bool(res)
            except Exception as e:  # noqa: BLE001
                self._known[d] = None
                self._retry_at[d] = now_ts + CAL_RETRY_SEC
                logger.debug("連線假死偵測：休市日曆查不到 %s（當平日）：%r", d, e)
        if len(self._known) > 20:                                            # 只留最近的
            for k in sorted(self._known)[:-10]:
                self._known.pop(k, None)


async def session_watch_loop(has_subs: Callable[[], bool], days: Optional[TradingDays] = None) -> None:
    """每 POLL_SEC 秒判斷一次。SESSION_WATCH=false 關閉（stale_reason 永遠是 None）。"""
    if not enabled():
        logger.info("連線假死偵測已關閉（SESSION_WATCH=false）")
        return
    days = days or TradingDays()
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            now_ts = time.time()
            now = datetime.fromtimestamp(now_ts, TAIPEI)
            await days.ensure([now.date(), now.date() - timedelta(days=1)], now_ts)
            session_watch.check(now_ts, now, live_window(now, days.is_open), has_subs())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("連線假死偵測迴圈例外（已忽略）")
