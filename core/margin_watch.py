"""保證金風險推播（update.md 發現 34）：權益快被維持保證金追上時通知，只通知、不動任何委託。

為什麼：帳戶只有 5 萬多、1 口微台的原始保證金約 3.5 萬，加碼或大行情時，權益可能很快逼近維持保證金（跌破就被追繳、甚至被期貨商強制平倉）。
判斷（每分鐘一次，用快取的保證金資料，不查券商）：
  · 券商自己回報 margin_call > 0（追繳金額）→ 緊急；
  · 否則「權益總值 ÷ 維持保證金」低於 NOTIFY_MARGIN_URGENT（預設 120%）→ 緊急、低於 NOTIFY_MARGIN_WARN（預設 150%）→ 警告；
  · 沒有持倉（維持保證金 0）就不判斷。
每個等級有冷卻（警告 30 分鐘、緊急／追繳 10 分鐘）；回到安全範圍會推播一次「已解除」。「低於 100% 會被追繳」以期貨商規定為準，這裡只是提醒。"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.notify import notify

logger = logging.getLogger(__name__)

COOLDOWN = {"warn": 1800.0, "urgent": 600.0, "call": 600.0}
RECOVER_COOLDOWN = 600.0


def _pct(name: str, default: float) -> float:
    try:
        v = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return v if v > 0 else default


def thresholds() -> tuple[float, float]:
    """(警告, 緊急)，單位 %；緊急一定不高於警告。"""
    warn, urgent = _pct("NOTIFY_MARGIN_WARN", 150.0), _pct("NOTIFY_MARGIN_URGENT", 120.0)
    return warn, min(urgent, warn)


def assess(margin: Optional[dict[str, Any]]) -> dict[str, Any]:
    """margin＝/api/position/margin 的快取內容。回傳 {level: ok|warn|urgent|call, ratio（%，沒有持倉是 None）, equity, maintenance, call}。"""
    m = margin or {}
    equity = float(m.get("equity_amount") or m.get("equity") or 0)          # 權益總值（期貨商算風險指標用的）；沒有就用權益數
    maintenance, call = float(m.get("maintenance_margin") or 0), float(m.get("margin_call") or 0)
    warn, urgent = thresholds()
    ratio = None if maintenance <= 0 else equity / maintenance * 100
    if call > 0:
        level = "call"
    elif ratio is None:
        level = "ok"
    elif ratio < urgent:
        level = "urgent"
    elif ratio < warn:
        level = "warn"
    else:
        level = "ok"
    return {"level": level, "ratio": ratio, "equity": equity, "maintenance": maintenance, "call": call}


def message(a: dict[str, Any]) -> tuple[str, str]:
    ratio = "—" if a["ratio"] is None else f"{a['ratio']:.0f}%"
    base = f"權益 {a['equity']:,.0f} 元、維持保證金 {a['maintenance']:,.0f} 元（權益 ÷ 維持保證金 ＝ {ratio}；低於 100% 會被追繳，以期貨商規定為準）。"
    if a["level"] == "call":
        return "🚨 券商回報需要追繳保證金", f"追繳金額 {a['call']:,.0f} 元。{base}請立刻補錢或減碼。"
    if a["level"] == "urgent":
        return "🚨 保證金風險：權益快被維持保證金追上", f"{base}請立刻減碼或補錢。"
    if a["level"] == "warn":
        return "⚠️ 保證金風險偏高", f"{base}留意不要再加碼。"
    return "✅ 保證金風險已解除", base


class MarginWatcher:
    """記住上一次的等級：變嚴重或冷卻過了才再推；從有風險回到 ok 推播一次。"""

    def __init__(self) -> None:
        self.level = "ok"

    def check(self, margin: Optional[dict[str, Any]]) -> str:
        try:
            a = assess(margin)
            prev, self.level = self.level, a["level"]
            if a["level"] != "ok":
                title, msg = message(a)
                notify(title, msg, key=f"margin:{a['level']}", cooldown=COOLDOWN[a["level"]], priority="urgent" if a["level"] != "warn" else "high")
            elif prev != "ok":
                title, msg = message(a)
                notify(title, msg, key="margin:recover", cooldown=RECOVER_COOLDOWN, priority="default")
            return a["level"]
        except Exception:  # noqa: BLE001
            logger.exception("保證金風險檢查失敗（已忽略）")
            return self.level


margin_watcher = MarginWatcher()
