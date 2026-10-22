"""週報（update.md 發現 63）：每週六 10:00 一則 Telegram，整理上一週（週一～週六，含週五夜盤）的已實現損益。**只通知、不影響任何交易決策。**

資料來源是**券商的已實現損益**（`list_profit_loss(begin, end)`，一週只查一次帳務，遠低於 5 秒 25 次的上限）——手動單、策略單、在別的軟體下的單都在裡面；
trade_log 只記得經過本系統的單，所以只拿來補「手動停損監控觸發了幾次」。
內容：平倉筆數與口數、損益（券商的 pnl）、手續費＋交易稅、扣完費用後、賺／賠筆數與各自平均、最大一筆虧損、各合約、各天。
訊息**不放帳號與權益**（連「占權益 %」也不行）。費用另外列出、不猜券商的 pnl 有沒有含費用——第一則請和對帳單核對一次（ARCHITECTURE.md〈週報〉）。
時間：WEEKLY_REPORT_TIME（預設 1000）之後到週日結束；一週一則（冷卻 key＝那週週一，跨重啟）；查詢失敗 10 分鐘後再試。WEEKLY_REPORT=false 關閉；模擬盤不推播（notify 的規則）。
不接進任何策略、風控或下單模組（`tests/test_weekly_report.py` 靜態檢查）。"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from core.daytrade import TAIPEI
from core.notify import notify, sim_blocked

logger = logging.getLogger(__name__)

POLL_SEC = 60.0
RETRY_SEC = 600.0
_WD_ZH = "一二三四五六日"
REASON_ZH = {"stop_loss": "停損", "take_profit": "停利"}


def enabled() -> bool:
    return os.getenv("WEEKLY_REPORT", "true").lower() != "false"


def report_minute() -> int:
    """WEEKLY_REPORT_TIME（HHMM，預設 1000）→ 週六的第幾分鐘；格式不對退回 10:00。"""
    raw = os.getenv("WEEKLY_REPORT_TIME", "1000").strip().replace(":", "")
    try:
        h, m = divmod(int(raw), 100)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except ValueError:
        pass
    return 10 * 60


def week_of(now: datetime) -> tuple[date, date]:
    """要報的那一週：週六、週日報這週；其他日子報上一週。回傳 (週一, 週六)。"""
    d = now.astimezone(TAIPEI).date()
    mon = d - timedelta(days=d.weekday())
    if d.weekday() < 5:
        mon -= timedelta(days=7)
    return mon, mon + timedelta(days=5)


def _day(raw: Any) -> Optional[str]:
    """券商的日期（'20261016' 或 '2026-10-16'）→ ISO；認不得 None。"""
    s = str(raw or "").strip().replace("-", "").replace("/", "")[:8]
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat()
    except ValueError:
        return None


def _num(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v else 0.0


# ── 整理（純函式）────────────────────────────────────────────────

def summarize(rows: list[dict[str, Any]], mon: date, sat: date, stops: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """券商的已實現損益列（每列一筆平倉）→ 週報要的數字。日期不在這週的列不算（券商的區間查詢邊界不一定和我們一樣）。"""
    lo, hi = mon.isoformat(), sat.isoformat()
    trades = []
    for r in rows or []:
        d = _day(r.get("date"))
        if d is None or not (lo <= d <= hi):
            continue
        cost = _num(r.get("fee")) + _num(r.get("tax"))
        trades.append({"date": d, "code": str(r.get("code") or "?"), "qty": int(_num(r.get("quantity"))), "pnl": _num(r.get("pnl")), "cost": cost})
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    by_code: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "qty": 0, "pnl": 0.0, "cost": 0.0})
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "cost": 0.0})
    for t in trades:
        c, d = by_code[t["code"]], by_day[t["date"]]
        c["n"] += 1
        c["qty"] += t["qty"]
        c["pnl"] += t["pnl"]
        c["cost"] += t["cost"]
        d["n"] += 1
        d["pnl"] += t["pnl"]
        d["cost"] += t["cost"]
    pnl, cost = sum(t["pnl"] for t in trades), sum(t["cost"] for t in trades)
    return {
        "mon": lo, "sat": hi, "n": len(trades), "qty": sum(t["qty"] for t in trades),
        "pnl": round(pnl, 2), "cost": round(cost, 2), "net": round(pnl - cost, 2),
        "wins": len(wins), "losses": len(losses), "flat": len(trades) - len(wins) - len(losses),
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else None,
        "worst": min(trades, key=lambda t: t["pnl"]) if losses else None,
        "by_code": {k: {**v, "pnl": round(v["pnl"], 2), "cost": round(v["cost"], 2)} for k, v in sorted(by_code.items())},
        "by_day": {k: {**v, "pnl": round(v["pnl"], 2), "cost": round(v["cost"], 2)} for k, v in sorted(by_day.items())},
        "stops": stops or {},
    }


def _twd(x: float) -> str:
    return f"{'+' if x > 0 else '−' if x < 0 else ''}NT${abs(x):,.0f}"


def compose(s: dict[str, Any]) -> tuple[str, str]:
    m, sa = date.fromisoformat(s["mon"]), date.fromisoformat(s["sat"])
    title = f"🗓 週報 {m.month}/{m.day}～{sa.month}/{sa.day}"
    if s["n"] == 0:
        return title, "這週沒有平倉紀錄（券商的已實現損益是空的）。"
    L = [f"平倉 {s['n']} 筆、{s['qty']} 口：損益 {_twd(s['pnl'])}，手續費＋稅 NT${s['cost']:,.0f}，扣完 {_twd(s['net'])}",
         f"賺 {s['wins']} 筆（平均 {_twd(s['avg_win']) if s['avg_win'] is not None else '—'}）、賠 {s['losses']} 筆（平均 {_twd(s['avg_loss']) if s['avg_loss'] is not None else '—'}）"
         + (f"、持平 {s['flat']} 筆" if s["flat"] else "")]
    if s["worst"]:
        w = s["worst"]
        L.append(f"最大一筆虧損：{w['code']} {w['qty']} 口 {_twd(w['pnl'])}（{int(w['date'][5:7])}/{int(w['date'][8:10])}）")
    if len(s["by_code"]) > 1:
        L.append("各合約：" + "；".join(f"{k} {v['n']} 筆 {_twd(v['pnl'])}" for k, v in s["by_code"].items()))
    L.append("各天：" + "；".join(f"{int(k[5:7])}/{int(k[8:10])}（{_WD_ZH[date.fromisoformat(k).weekday()]}）{_twd(v['pnl'])}" for k, v in s["by_day"].items()))
    st = s.get("stops") or {}
    if st:
        L.append("停損監控觸發：" + "、".join(f"{REASON_ZH.get(k, k)} {v} 次" for k, v in sorted(st.items())))
    L.append("損益是券商的已實現損益（日期以券商為準）；手續費與稅另列。勝率不是優勢——看扣完費用後的數字。")
    return title, "\n".join(L)


# ── 取資料 ───────────────────────────────────────────────────────

def stop_counts(path: Path, mode: str, mon: date, sat: date) -> dict[str, int]:
    """（同步、唯讀）trade_log 的手動停損監控出場：這週各種原因的次數。讀不到回 {}。"""
    try:
        start = datetime(mon.year, mon.month, mon.day, tzinfo=TAIPEI).timestamp()
        end = datetime(sat.year, sat.month, sat.day, tzinfo=TAIPEI).timestamp() + 86400
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as conn:
            rows = conn.execute("SELECT exit_reason, COUNT(*) FROM exit_fills WHERE mode=? AND exit_ts>=? AND exit_ts<? AND strategy_name='manual_monitor' "
                                "GROUP BY exit_reason", (mode, start, end)).fetchall()
        return {str(r[0] or "其他"): int(r[1]) for r in rows}
    except Exception:  # noqa: BLE001
        logger.debug("週報：讀 trade_log 失敗", exc_info=True)
        return {}


async def build(now: datetime, fetch: Callable[[str, str], Awaitable[list[dict[str, Any]]]],
                stops: Optional[Callable[[date, date], dict[str, int]]] = None) -> dict[str, Any]:
    """查這週的已實現損益並整理；fetch 失敗會丟例外（呼叫端決定重試）。"""
    mon, sat = week_of(now)
    rows = await fetch(mon.isoformat(), sat.isoformat())
    st = await asyncio.to_thread(stops, mon, sat) if stops else {}
    return summarize(list(rows or []), mon, sat, st)


# ── 背景迴圈 ────────────────────────────────────────────────────

class WeeklyReport:
    def __init__(self) -> None:
        self.done_for: Optional[str] = None            # 這週處理過了（發了或模擬盤）就不再看
        self.retry_at = 0.0

    async def run_once(self, now: datetime, fetch: Callable[[str, str], Awaitable[list[dict[str, Any]]]],
                       stops: Optional[Callable[[date, date], dict[str, int]]] = None) -> Optional[str]:
        """到點就查、組、推一則；回傳 "sent"／"sim"／"failed"，還沒到點或已處理回 None。"""
        t = now.astimezone(TAIPEI)
        mon, _ = week_of(t)
        key = mon.isoformat()
        in_window = (t.weekday() == 5 and t.hour * 60 + t.minute >= report_minute()) or t.weekday() == 6
        if self.done_for == key or not in_window or now.timestamp() < self.retry_at:
            return None
        if sim_blocked():
            self.done_for = key
            return "sim"
        try:
            s = await build(t, fetch, stops)
        except Exception as e:  # noqa: BLE001
            self.retry_at = now.timestamp() + RETRY_SEC
            logger.warning("週報：查已實現損益失敗，%d 秒後再試：%r", int(RETRY_SEC), e)
            return "failed"
        title, body = compose(s)
        self.done_for = key
        notify(title, body, key=f"weekly:{key}", cooldown=8 * 86400.0, priority="default")
        return "sent"


weekly_report = WeeklyReport()


async def weekly_loop(fetch: Callable[[str, str], Awaitable[list[dict[str, Any]]]], stops: Optional[Callable[[date, date], dict[str, int]]] = None) -> None:
    if not enabled():
        logger.info("週報已關閉（WEEKLY_REPORT=false）")
        return
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await weekly_report.run_once(datetime.now(TAIPEI), fetch, stops)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("週報迴圈例外（已忽略）")
