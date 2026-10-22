"""下單前檢查（update.md 發現 60）：把散在各處、和「這筆單風險多大」有關的資訊集中成一張卡——只顯示、不擋單、不是進場訊號。

  ① 今天預期日盤振幅（〈時段脈絡〉的開盤前預期，發現 55）；
  ② 隱含波動與它在過去一年的百分位（〈市場狀態〉的 IV，發現 61 起歷史已回填）；
  ③ 接下來 36 小時內的美國重要事件（CPI／非農／FOMC，同事件提醒，發現 52；只有 CPI 附上已確認的跳空倍數）。
全部讀記憶體或唯讀資料庫（lh-intel 在背景執行緒讀），任何一塊壞了只讓那一塊寫原因，不影響其他。"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from core import event_reminder as er

logger = logging.getLogger(__name__)

EVENT_HOURS = 36.0


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


def build(ctx: Optional[dict[str, Any]], state: Optional[dict[str, Any]], events: Optional[list[dict[str, Any]]], now: Optional[float] = None) -> dict[str, Any]:
    """ctx＝/api/market/live 的 ctx（含 day_iv）；state＝/api/market/state（含 iv）；events＝upcoming_major 的結果。"""
    now = time.time() if now is None else now
    day_iv = (ctx or {}).get("day_iv")
    iv = (state or {}).get("iv") if (state or {}).get("ready") else None
    return {
        "day_range": day_iv if day_iv else {"note": "還沒有（後端剛啟動、盤前資料還沒算好、或不在日盤）"},
        "iv": None if not iv else {"value": iv.get("value"), "percentile": iv.get("percentile"), "label": iv.get("label"), "history_n": iv.get("history_n"), "note": iv.get("note") or ""},
        "events": events, "events_note": None if events is not None else "讀不到 lh-intel 的事件行事曆", "event_hours": EVENT_HOURS, "as_of": now,
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
