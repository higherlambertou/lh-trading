from collections import deque
from typing import Any
import logging
import statistics

import shioaji as sj

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class BollingerStrategy(BaseStrategy):
    """
    布林通道策略（均值回歸）：
    - 中軌 = period 筆移動平均，上下軌 = 中軌 ± num_std × 標準差
    - 價格跌破下軌 → 做多
    - 價格突破上軌 → 做空（並平倉反手）
    """

    name = "bollinger"

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        super().__init__()
        self.period = period
        self.num_std = num_std
        self.prices: deque[float] = deque(maxlen=period)

    # ─── 參數支援 ────────────────────────────────────────────────────

    @property
    def params(self) -> dict[str, Any]:
        return {"period": self.period, "num_std": self.num_std, **self._base_params}

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "period",  "label": "通道週期",   "type": "number", "min": 2, "max": 200},
            {"key": "num_std", "label": "標準差倍數", "type": "number", "min": 1, "max": 4},
            *self._base_param_schema,
        ]

    def _apply_params(self, params: dict[str, Any]) -> None:
        self.period = int(params.get("period", self.period))
        self.num_std = float(params.get("num_std", self.num_std))
        self.prices = deque(maxlen=self.period)  # 重建 buffer
        logger.info("[bollinger] 套用參數: period=%d  num_std=%.1f", self.period, self.num_std)

    # ─── 策略邏輯 ────────────────────────────────────────────────────

    def _bands(self) -> tuple[float, float, float] | None:
        if len(self.prices) < self.period:
            return None
        mean = sum(self.prices) / len(self.prices)
        std = statistics.pstdev(self.prices)
        upper = mean + self.num_std * std
        lower = mean - self.num_std * std
        return mean, upper, lower

    async def on_quote(self, quote: sj.QuoteFOPv1) -> None:
        price = float(quote.close)
        self.prices.append(price)

        bands = self._bands()
        if bands is None:
            return
        mean, upper, lower = bands

        if price < lower and self.state.position <= 0:
            if self.state.position < 0:
                await self.place_order(sj.constant.Action.Buy, abs(self.state.position))
                self.state.realized_pnl += (
                    (self.state.entry_price - price) * abs(self.state.position) * self.point_value
                )
            await self.place_order(sj.constant.Action.Buy, 1)
            self.state.entry_price = price
            self.state.position = 1
            logger.info("[bollinger] 跌破下軌 %.0f → 做多 @ %.0f", lower, price)

        elif price > upper and self.state.position >= 0:
            if self.state.position > 0:
                await self.place_order(sj.constant.Action.Sell, self.state.position)
                self.state.realized_pnl += (
                    (price - self.state.entry_price) * self.state.position * self.point_value
                )
            await self.place_order(sj.constant.Action.Sell, 1)
            self.state.entry_price = price
            self.state.position = -1
            logger.info("[bollinger] 突破上軌 %.0f → 做空 @ %.0f", upper, price)
