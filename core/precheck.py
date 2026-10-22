"""下單前檢查（update.md 發現 60）：把散在各處、和「這筆單風險多大」有關的資訊集中成一張卡——只顯示、不擋單、不是進場訊號。

  ① 今天預期日盤振幅（〈時段脈絡〉的開盤前預期，發現 55）；
  ② 隱含波動與它在過去一年的百分位（〈市場狀態〉的 IV，發現 61 起歷史已回填）；
  ③ 接下來 36 小時內的美國重要事件（CPI／非農／FOMC，同事件提醒，發現 52；只有 CPI 附上已確認的跳空倍數）；
  ④ 13:45 以後到隔天 05:00：今晚夜盤的預期振幅（core/night_range，發現 67）與下一個日盤的預期振幅（發現 55 的模型，用今天收盤的臺指 VIX）。
全部讀記憶體或唯讀資料庫（lh-intel 在背景執行緒讀），任何一塊壞了只讓那一塊寫原因，不影響其他。"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from core import event_reminder as er
from core import night_range

logger = logging.getLogger(__name__)

EVENT_HOURS = 36.0
SESSIONS_RETRY_SEC = 600.0          # 還沒有前一個日盤的資料（收盤後同步）時，多久重讀一次日盤歷史


def upcoming_major(now: float, upcoming: Optional[list[dict[str, Any]]], hours: float = EVENT_HOURS) -> Optional[list[dict[str, Any]]]:
    """lh-intel 的事件 → 接下來 hours 小時內的 CPI／非農／FOMC（時間、說法、這個事件的歷史事實）；讀不到是 None。"""
    if upcoming is None:
        return None
    out = []
    for ev in upcoming:
        kind = er.kind_of(ev)
        t = ev.get("scheduled_time")
        if kind is None or t is None or not (now <= float(t) <= now + hours * 3600):
            continue
        s = er.STAT[kind]
        note = (f"歷史上隔天日盤開盤跳空約平常的 {s['gap_amp']:.1f} 倍（中位數 {s['median_event']} vs {s['median_ctrl']} 點）" if kind == "CPI"
                else "歷史上沒有確認會讓隔天日盤變大")
        out.append({"kind": kind, "label": s["label"], "when": er.when_text(float(t), now), "time": float(t), "note": note})
    return sorted(out, key=lambda x: x["time"])


def build(ctx: Optional[dict[str, Any]], state: Optional[dict[str, Any]], events: Optional[list[dict[str, Any]]], now: Optional[float] = None,
          evening_: Optional[dict[str, Any]] = None, hits: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """ctx＝/api/market/live 的 ctx（含 day_iv）；state＝/api/market/state（含 iv）；events＝upcoming_major 的結果。"""
    now = time.time() if now is None else now
    day_iv = (ctx or {}).get("day_iv")
    iv = (state or {}).get("iv") if (state or {}).get("ready") else None
    return {
        "day_range": day_iv if day_iv else {"note": "還沒有（後端剛啟動、盤前資料還沒算好、或不在日盤）"},
        "iv": None if not iv else {"value": iv.get("value"), "percentile": iv.get("percentile"), "label": iv.get("label"), "history_n": iv.get("history_n"), "note": iv.get("note") or ""},
        "events": events, "events_note": None if events is not None else "讀不到 lh-intel 的事件行事曆", "event_hours": EVENT_HOURS, "as_of": now,
        "evening": evening_,
        "hits": hits,                       # 預測紀錄的命中率（core/forecast_log，發現 68）
    }


def fetch_events(now: float, reader: Optional[Callable[[float], Optional[list[dict[str, Any]]]]] = None) -> Optional[list[dict[str, Any]]]:
    """（同步、會讀資料庫）丟執行緒呼叫。"""
    try:
        from core import intel_reader
        up = (reader or (lambda t: intel_reader.upcoming_events(t, ahead_days=2)))(now)
        return upcoming_major(now, up)
    except Exception:  # noqa: BLE001
        logger.debug("下單前檢查：讀事件失敗", exc_info=True)
        return None


# ── 傍晚到凌晨：今晚夜盤與下一個日盤（發現 67、55）────────────────

_sess: dict[str, Any] = {"prev": None, "at": 0.0, "sessions": None}


def _sessions_until(store: Any, prev: str, now: float, loader: Optional[Callable[[Any, str], list]] = None) -> list:
    """到 prev（含）為止的日盤歷史。讀到 prev 那天之後整晚沿用；還沒有就每 SESSIONS_RETRY_SEC 秒重讀一次（約 0.8 秒的 CPU，在執行緒裡）。"""
    have = _sess["sessions"]
    fresh = _sess["prev"] == prev and have is not None and (bool(have) and have[-1].date == prev or now - _sess["at"] < SESSIONS_RETRY_SEC)
    if fresh:
        return have
    if loader is None:
        from core.intraday_context import load_sessions
        loader = lambda st, before: load_sessions(st, "TXF", before=before)              # noqa: E731
    target = (date.fromisoformat(prev) + timedelta(days=1)).isoformat()
    sessions = loader(store, target)
    _sess.update(prev=prev, at=now, sessions=sessions)
    return sessions


def evening(now: float, store: Any, loader: Optional[Callable[[Any, str], list]] = None,
            vix_fn: Optional[Callable[[str], tuple]] = None, model_fn: Optional[Callable[[], Optional[dict]]] = None) -> Optional[dict[str, Any]]:
    """（同步、會讀資料庫；丟執行緒）13:45 以後到隔天 05:00 才有：{"prev_day", "night", "next_day"}；其他時段 None。
    next_day 只在 13:45～24:00（過了午夜就是「今天」的日盤，由 ① 顯示）。"""
    from core import iv_day_range
    from core.intraday_context import SESSION_MIN, window_range
    import numpy as np
    dt_now = datetime.fromtimestamp(now, night_range.TAIPEI)
    prev = night_range.prev_trading_day(dt_now)
    if prev is None:
        return None
    sessions = _sessions_until(store, prev, now, loader)
    s_prev = sessions[-1] if sessions and sessions[-1].date == prev else None
    day_range = close = None
    if s_prev is not None:
        day_range = float(window_range(s_prev, 0, SESSION_MIN))
        k = np.flatnonzero(np.isfinite(s_prev.cl))
        close = float(s_prev.cl[k[-1]]) if len(k) else None
    target = (date.fromisoformat(prev) + timedelta(days=1)).isoformat()
    vix, _ = (vix_fn or iv_day_range.live_vix)(target)
    vix_ok = vix if vix and vix[0] == prev else None
    night = night_range.for_tonight((model_fn or night_range.load_model)(), prev, day_range, close, vix_ok)
    next_day = None
    if dt_now.hour * 60 + dt_now.minute >= 13 * 60 + 45:
        if s_prev is None:
            next_day = {"note": f"還沒有 {prev} 的日盤資料（收盤後同步）"}
        elif vix_ok is None:
            next_day = {"note": f"等 {prev[5:]} 收盤的臺指選擇權波動率指數（lh-intel 約 16:15 抓到）"}
        else:
            next_day = iv_day_range.for_today(sessions, target, vix=vix_ok)
    return {"prev_day": prev, "night": night, "next_day": next_day}


def fetch_evening(now: float, store: Any) -> Optional[dict[str, Any]]:
    """（同步）丟執行緒呼叫；任何錯誤都回 {"error": ...}，不影響卡片的其他部分。"""
    try:
        return evening(now, store)
    except Exception as e:  # noqa: BLE001
        logger.debug("下單前檢查：夜盤／下一個日盤的預期振幅失敗", exc_info=True)
        return {"error": f"{type(e).__name__}"}
