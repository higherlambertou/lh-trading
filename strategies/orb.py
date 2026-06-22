"""ORB 開盤區間突破（Opening Range Breakout）。

台指日盤經典策略：
  1. 開盤後前 N 分鐘（預設 08:45 起 15 分鐘）只觀察，記錄區間高低點。
  2. 之後 1 分 K 收盤價突破區間高點 + buffer → 做多；
     跌破區間低點 - buffer → 做空。
  3. 每個方向每天最多進場一次（突破失敗不重複追）。
  4. 出場交給停損停利（base 的固定點數）或 ATR 移動停損（bar_base）。

搭配建議：max_trades_per_day=2、trade_end_hhmm=1330（避免拖到收盤），
atr_trail_mult=2~3 讓趨勢日吃滿、假突破快砍。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.bar_builder import Bar
from strategies.bar_base import BarStrategy

logger = logging.getLogger(__name__)


class ORBStrategy(BarStrategy):
    name = "orb"

    def __init__(self) -> None:
        super().__init__()
        self.or_start_hhmm: int = 845    # 區間觀察起點（日盤開盤）
        self.or_minutes: int = 15        # 觀察幾分鐘
        self.buffer_pts: int = 5         # 突破緩衝（防假突破）
        # 預設風控：日盤尾盤不進場
        self.trade_end_hhmm = self.trade_end_hhmm or 1330

        self._or_day: str = ""           # 區間歸屬日
        self._or_high: float = 0.0
        self._or_low: float = 0.0
        self._or_done: bool = False      # 區間是否已收集完成
        self._long_done: bool = False    # 今日已做過多單
        self._short_done: bool = False

    # ── 參數 ──────────────────────────────────────────────────────
    @property
    def params(self) -> dict[str, Any]:
        return {
            "or_start_hhmm": self.or_start_hhmm,
            "or_minutes": self.or_minutes,
            "buffer_pts": self.buffer_pts,
            **self._bar_base_params,
            **self._base_params,
        }

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "or_start_hhmm", "label": "區間起點 HHMM",      "type": "number", "min": 0, "max": 2359},
            {"key": "or_minutes",    "label": "區間長度（分鐘）",    "type": "number", "min": 5, "max": 120},
            {"key": "buffer_pts",    "label": "突破緩衝點數",        "type": "number", "min": 0, "max": 100},
            *self._bar_base_param_schema,
            *self._base_param_schema,
        ]

    def _apply_params(self, params: dict[str, Any]) -> None:
        self.or_start_hhmm = int(params.get("or_start_hhmm", self.or_start_hhmm))
        self.or_minutes    = int(params.get("or_minutes",    self.or_minutes))
        self.buffer_pts    = int(params.get("buffer_pts",    self.buffer_pts))

    # ── 邏輯 ──────────────────────────────────────────────────────
    def _bar_hhmm(self, bar: Bar) -> int:
        dt = datetime.fromtimestamp(bar.ts)
        return dt.hour * 100 + dt.minute

    def _minutes_since_start(self, hhmm: int) -> int:
        h, m = divmod(hhmm, 100)
        sh, sm = divmod(self.or_start_hhmm, 100)
        return (h * 60 + m) - (sh * 60 + sm)

    async def on_bar(self, bar: Bar) -> None:
        today = datetime.fromtimestamp(bar.ts).strftime("%Y-%m-%d")
        if today != self._or_day:
            self._or_day = today
            self._or_high = 0.0
            self._or_low = 0.0
            self._or_done = False
            self._long_done = False
            self._short_done = False

        hhmm = self._bar_hhmm(bar)
        mins = self._minutes_since_start(hhmm)
        if mins < 0:
            return  # 還沒開盤

        # 收集區間
        if mins < self.or_minutes:
            self._or_high = max(self._or_high, bar.high) if self._or_high else bar.high
            self._or_low = min(self._or_low, bar.low) if self._or_low else bar.low
            return

        if not self._or_done:
            self._or_done = True
            if self._or_high and self._or_low:
                logger.info("[orb] 區間完成: %.0f ~ %.0f（%d 分鐘）",
                            self._or_low, self._or_high, self.or_minutes)
                self._event(f"開盤區間 {self._or_low:.0f}~{self._or_high:.0f}")

        if not self._or_high or not self._or_low:
            return  # 區間視窗內沒收到 bar（如盤前啟動晚了），今天放棄

        # 突破進場（每方向一天一次）
        if (not self._long_done and self.state.position <= 0
                and bar.close > self._or_high + self.buffer_pts):
            self._long_done = True
            logger.info("[orb] 突破區間高點 %.0f → 做多 @ %.0f", self._or_high, bar.close)
            await self._go(1, bar.close)

        elif (not self._short_done and self.state.position >= 0
                and bar.close < self._or_low - self.buffer_pts):
            self._short_done = True
            logger.info("[orb] 跌破區間低點 %.0f → 做空 @ %.0f", self._or_low, bar.close)
            await self._go(-1, bar.close)
