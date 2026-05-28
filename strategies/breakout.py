from collections import deque
from typing import Any
import logging

import shioaji as sj

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class BreakoutStrategy(BaseStrategy):
    """
    區間突破策略（順勢）：
    - 價格突破前 lookback 筆的最高價 → 做多
    - 價格跌破前 lookback 筆的最低價 → 做空（並平倉反手）
    """

    name = "breakout"

    def __init__(self, lookback: int = 20) -> None:
        super().__init__()
        self.lookback = lookback
        self.prices: deque[float] = deque(maxlen=lookback)

    # ─── 參數支援 ────────────────────────────────────────────────────

    @property
    def params(self) -> dict[str, Any]:
        return {"lookback": self.lookback}

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "lookback", "label": "回顧筆數", "type": "number", "min": 2, "max": 200},
        ]

    def _apply_params(self, params: dict[str, Any]) -> None:
        self.lookback = int(params.get("lookback", self.lookback))
        self.prices = deque(maxlen=self.lookback)  # 重建 buffer
        logger.info("[breakout] 套用參數: lookback=%d", self.lookback)

    # ─── 策略邏輯 ────────────────────────────────────────────────────

    async def on_quote(self, quote: sj.QuoteFOPv1) -> None:
        price = float(quote.close)

        # buffer 未滿前只累積，並以「之前」的區間判斷突破，故先比較後 append
        if len(self.prices) < self.lookback:
            self.prices.append(price)
            return

        highest = max(self.prices)
        lowest = min(self.prices)
        self.prices.append(price)

        if price > highest and self.state.position <= 0:
            if self.state.position < 0:
                await self.place_order(sj.constant.Action.Buy, abs(self.state.position))
                self.state.realized_pnl += (
                    (self.state.entry_price - price) * abs(self.state.position) * self.point_value
                )
            await self.place_order(sj.constant.Action.Buy, 1)
            self.state.entry_price = price
            self.state.position = 1
            logger.info("[breakout] 突破上緣 %.0f → 做多 @ %.0f", highest, price)

        elif price < lowest and self.state.position >= 0:
            if self.state.position > 0:
                await self.place_order(sj.constant.Action.Sell, self.state.position)
                self.state.realized_pnl += (
                    (price - self.state.entry_price) * self.state.position * self.point_value
                )
            await self.place_order(sj.constant.Action.Sell, 1)
            self.state.entry_price = price
            self.state.position = -1
            logger.info("[breakout] 跌破下緣 %.0f → 做空 @ %.0f", lowest, price)
