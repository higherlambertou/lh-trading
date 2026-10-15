"""盤前摘要（update.md 發現 37）：每個台股交易日一則 Telegram，只通知、不影響任何交易決策。

內容是「今天的尺度」，不是進場訊號：
  夜盤 TXF 報酬（本機 ticks.db，與盤前紀錄 core/premarket_log 同一個函式）、盤前市場狀態（Hurst／IV／方向，08:30 算好的）、
  近 20 日日盤平均振幅 R20（core/intraday_context，只用今天以前）、典型 σ（近 20 個日盤同時段 σ 的中位數，core/vol_watch 的基準）
  → 30 分鐘雜訊範圍 → 平衡型停利停損點數與 1 口微台的停損金額、今天是不是最後交易日。
訊息不放權益與「占權益 %」（通知通道是第三方服務，見 OPERATION.md 的通道說明：只放合約、方向、口數與停損點數）。
不放 VIX／費半：免費來源 12:16 才有前一天資料（update.md 發現 35），這個時間拿不到。
時間：DAILY_BRIEF_TIME（預設 0835，晚於 08:30 的市場狀態與盤前紀錄）；到點之後到 09:30 之前都會補發；一天一則（冷卻 key 跨重啟）。
任何一塊取不到就在那一行寫「取不到」，其他照給；整個流程的例外都吞掉。DAILY_BRIEF=false 關閉。模擬盤不推播（notify 的規則）。"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core import stop_suggest, vol_watch
from core.daytrade import TAIPEI
from core.notify import notify, sim_blocked

logger = logging.getLogger(__name__)

POLL_SEC = 30.0
WINDOW_END = 9 * 60 + 30
HORIZON = 30
_WD_ZH = "一二三四五六日"


def enabled() -> bool:
    return os.getenv("DAILY_BRIEF", "true").lower() != "false"


def brief_minute() -> int:
    """DAILY_BRIEF_TIME（HHMM，預設 0835）→ 當天的第幾分鐘；格式不對退回 08:35。"""
    raw = os.getenv("DAILY_BRIEF_TIME", "0835").strip().replace(":", "")
    try:
        v = int(raw)
        h, m = divmod(v, 100)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except ValueError:
        pass
    return 8 * 60 + 35


# ── 取資料（同步、會讀資料庫；在執行緒裡呼叫）─────────────────────────

def gather(now: datetime, store: Any, state: Optional[dict[str, Any]], is_open: Callable[[date], bool], previous_open: Callable[[date], date],
           ticks_path: Optional[Path] = None) -> dict[str, Any]:
    today = now.astimezone(TAIPEI).date()
    ds = today.isoformat()
    d: dict[str, Any] = {"date": ds, "weekday": _WD_ZH[today.weekday()], "state": state, "night": None, "r20": None,
                         "sigma": None, "n_sessions": 0, "table": stop_suggest.load_table(), "last_trading_day": False, "errors": []}
    try:
        from core import premarket_log
        d["night"] = premarket_log.txf_night_feature(ticks_path or premarket_log.TICKS_PATH, previous_open(today), now.astimezone(timezone.utc))
    except Exception as e:  # noqa: BLE001
        d["errors"].append(f"夜盤報酬：{type(e).__name__}")
        logger.warning("盤前摘要：夜盤報酬取不到：%r", e)
    try:
        from core import intraday_context
        d["r20"] = intraday_context.compute(store, "TXF", ds).r20
    except Exception as e:  # noqa: BLE001
        d["errors"].append(f"R20：{type(e).__name__}")
        logger.warning("盤前摘要：R20 取不到：%r", e)
    try:
        since = (today - timedelta(days=45)).isoformat()
        sessions = vol_watch.day_sessions(store.bars_1m("TXF", since))
        base = vol_watch.build_baseline(sessions, ds)
        d["sigma"] = vol_watch.typical_sigma(base)
        d["n_sessions"] = min(vol_watch.N_SESSIONS, sum(1 for k in sessions if k < ds))
    except Exception as e:  # noqa: BLE001
        d["errors"].append(f"典型 σ：{type(e).__name__}")
        logger.warning("盤前摘要：典型 σ 取不到：%r", e)
    try:
        from core.reminders import expiry_date
        d["last_trading_day"] = expiry_date(today.year, today.month, is_open) == today
    except Exception as e:  # noqa: BLE001
        d["errors"].append(f"到期日：{type(e).__name__}")
    return d


# ── 組訊息（純函式）──────────────────────────────────────────────

def _night_line(n: Optional[dict[str, Any]]) -> str:
    if not n:
        return "夜盤 TXF：取不到"
    v = n.get("value")
    if v is None:
        return f"夜盤 TXF：沒有資料（{str(n.get('missing_reason') or '原因不明')[:60]}）"
    det = n.get("detail") or {}
    tail = ""
    if det.get("night_close") and det.get("prev_close"):
        tail = f"（收 {det['night_close']:,.0f}，前一日日盤收 {det['prev_close']:,.0f}）"
    return f"夜盤 TXF：{v * 100:+.2f}%{tail}"


def _state_line(s: Optional[dict[str, Any]]) -> str:
    if not s or not s.get("ready") or not s.get("hurst"):
        return "市場狀態：還沒算出來（08:30 的計算沒完成）"
    h, iv = s["hurst"], s.get("iv") or {}
    hv = "—" if h.get("value") is None else f"{h['value']:.2f}"
    z = "" if h.get("z") is None else f"，z={h['z']:+.1f}"
    ivt = f"{iv['percentile']:.0f}%（{iv.get('label')}）" if iv.get("percentile") is not None else f"—（{iv.get('label', '—')}）"
    return f"市場狀態：Hurst {hv}（{h.get('label')}{z}）、IV 百分位 {ivt}、方向 {s.get('direction_label', '—')}"


def compose(d: dict[str, Any]) -> tuple[str, str]:
    lines = [_night_line(d.get("night")), _state_line(d.get("state"))]
    if d.get("r20"):
        lines.append(f"近 20 日日盤平均振幅 R20 ≈ {d['r20']:.0f} 點")
    sigma = d.get("sigma")
    if sigma and sigma > 0:
        card = stop_suggest.build(sigma, 30, session="day", horizon=HORIZON, contract="TMF", equity=None, table=d.get("table"))
        row = next((r for r in card.get("rows", []) if r["name"] == "平衡"), None)
        line = f"典型 σ ≈ {sigma:.1f} 點／分鐘 → {HORIZON} 分鐘雜訊範圍約 {sigma * math.sqrt(HORIZON):.0f} 點"
        if row:
            line += f"；平衡型停利 {row['tp']}／停損 {row['sl']} 點（1 口微台停損 NT${row['loss_twd']:,}）"
        lines.append(line)
    else:
        lines.append("典型 σ：取不到（日盤歷史資料不夠）")
    if d.get("last_trading_day"):
        mo = int(d["date"][5:7])
        lines.append(f"📅 今天是 {mo} 月合約最後交易日（13:30 收盤）。")
    if d.get("errors"):
        lines.append("取不到：" + "、".join(d["errors"]))
    lines.append("這是今天的尺度，不是進場訊號。VIX／費半要中午之後才有資料，沒放進來。")
    return f"📋 盤前摘要 {int(d['date'][5:7])}/{int(d['date'][8:10])}（{d['weekday']}）", "\n".join(lines)


# ── 背景迴圈 ────────────────────────────────────────────────────

class DailyBrief:
    def __init__(self) -> None:
        self.sent_for: Optional[str] = None            # 今天處理過了（發了、休市或模擬盤）就不再看

    async def run_once(self, now: datetime, get_state: Callable[[], Optional[dict[str, Any]]], store: Any,
                       is_open: Callable[[date], bool], previous_open: Callable[[date], date]) -> Optional[str]:
        """到點就組並推播一則；回傳 "sent"／"closed"／"sim"，還沒到點或已處理回 None。"""
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
        d = await asyncio.to_thread(gather, now, store, get_state(), is_open, previous_open)
        title, body = compose(d)
        self.sent_for = today
        notify(title, body, key=f"brief:{today}", cooldown=20 * 3600.0, priority="default")
        return "sent"


daily_brief = DailyBrief()


async def brief_loop(get_state: Callable[[], Optional[dict[str, Any]]], store: Any) -> None:
    if not enabled():
        logger.info("盤前摘要已關閉（DAILY_BRIEF=false）")
        return
    from core.premarket_log import TwCalendar
    cal = TwCalendar()
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await daily_brief.run_once(datetime.now(TAIPEI), get_state, store, lambda x: bool(cal.check(x)["open"]), cal.previous_open)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("盤前摘要迴圈例外（已忽略）")
