from collections import deque
from typing import Any
import logging

import shioaji as sj

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class RSIStrategy(BaseStrategy):
    """
    RSI 超買超賣策略（震盪反轉）：
    - RSI 低於 oversold（超賣）→ 做多
    - RSI 高於 overbought（超買）→ 做空（並平倉反手）

    RSI 採用 period 筆價差的簡單平均（非 Wilder 平滑），逐 tick 計算。
    """

    name = "rsi"

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
    ) -> None:
        super().__init__()
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.prices: deque[float] = deque(maxlen=period + 1)  # 需 period+1 筆才有 period 個價差

    # ─── 參數支援 ────────────────────────────────────────────────────

    @property
    def params(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "oversold": self.oversold,
            "overbought": self.overbought,
            **self._base_params,
        }

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "period",     "label": "RSI 週期", "type": "number", "min": 2,  "max": 100},
            {"key": "oversold",   "label": "超賣門檻", "type": "number", "min": 1,  "max": 50},
            {"key": "overbought", "label": "超買門檻", "type": "number", "min": 50, "max": 99},
            *self._base_param_schema,
        ]

    def _apply_params(self, params: dict[str, Any]) -> None:
        self.period = int(params.get("period", self.period))
        self.oversold = float(params.get("oversold", self.oversold))
        self.overbought = float(params.get("overbought", self.overbought))
        self.prices = deque(maxlen=self.period + 1)  # 重建 buffer
        logger.info(
            "[rsi] 套用參數: period=%d  oversold=%.0f  overbought=%.0f",
            self.period, self.oversold, self.overbought,
        )

    # ─── 策略邏輯 ────────────────────────────────────────────────────

    def _rsi(self) -> float | None:
        if len(self.prices) < self.period + 1:
            return None
        prices = list(self.prices)
        deltas = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
        avg_gain = sum(d for d in deltas if d > 0) / self.period
        avg_loss = sum(-d for d in deltas if d < 0) / self.period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    async def on_quote(self, quote: sj.QuoteFOPv1) -> None:
        price = float(quote.close)
        self.prices.append(price)

        rsi = self._rsi()
        if rsi is None:
            return

        if rsi < self.oversold and self.state.position <= 0:
            logger.info("[rsi] 超賣 RSI=%.1f → 做多 @ %.0f", rsi, price)
            await self._go(1, price)

        elif rsi > self.overbought and self.state.position >= 0:
            logger.info("[rsi] 超買 RSI=%.1f → 做空 @ %.0f", rsi, price)
            await self._go(-1, price)
