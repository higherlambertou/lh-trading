from collections import deque
from typing import Any
import logging

import shioaji as sj

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class MACrossStrategy(BaseStrategy):
    """
    均線交叉策略：
    - 快線(short_period)上穿慢線(long_period) → 做多
    - 快線下穿慢線 → 做空（並平倉反手）
    """

    name = "ma_cross"

    def __init__(self, short_period: int = 5, long_period: int = 20) -> None:
        super().__init__()
        self.short_period = short_period
        self.long_period = long_period
        self.prices: deque[float] = deque(maxlen=long_period)
        self._prev_signal: int = 0  # 1=多, -1=空, 0=無訊號

    # ─── 參數支援 ────────────────────────────────────────────────────

    @property
    def params(self) -> dict[str, Any]:
        return {
            "short_period": self.short_period,
            "long_period": self.long_period,
            **self._base_params,
        }

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "short_period", "label": "快線週期", "type": "number", "min": 1,  "max": 50},
            {"key": "long_period",  "label": "慢線週期", "type": "number", "min": 2,  "max": 200},
            *self._base_param_schema,
        ]

    def _apply_params(self, params: dict[str, Any]) -> None:
        self.short_period = int(params.get("short_period", self.short_period))
        self.long_period  = int(params.get("long_period",  self.long_period))
        self.prices = deque(maxlen=self.long_period)  # 重建 buffer
        logger.info("[ma_cross] 套用參數: short=%d  long=%d", self.short_period, self.long_period)

    # ─── 策略邏輯 ────────────────────────────────────────────────────

    def _ma(self, period: int) -> float | None:
        if len(self.prices) < period:
            return None
        return sum(list(self.prices)[-period:]) / period

    async def on_quote(self, quote: dict) -> None:
        price = float(quote["close"])
        self.prices.append(price)

        short_ma = self._ma(self.short_period)
        long_ma  = self._ma(self.long_period)

        if short_ma is None or long_ma is None:
            return

        signal = 1 if short_ma > long_ma else -1

        if signal == self._prev_signal:
            return

        self._prev_signal = signal
        logger.info(
            "[ma_cross] 訊號切換: %s  快MA=%.1f  慢MA=%.1f  價格=%.0f",
            "多" if signal == 1 else "空", short_ma, long_ma, price,
        )

        if signal == 1 and self.state.position <= 0:
            await self._go(1, price)

        elif signal == -1 and self.state.position >= 0:
            await self._go(-1, price)
