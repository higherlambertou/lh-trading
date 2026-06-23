from collections import deque
from typing import Any
import logging



from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class MomentumStrategy(BaseStrategy):
    """
    動能策略（追漲殺跌）：
    - 比較目前價格與 period 筆之前的價格
    - 漲幅超過 threshold_pct（%）→ 做多
    - 跌幅超過 threshold_pct（%）→ 做空（並平倉反手）
    """

    name = "momentum"

    def __init__(self, period: int = 10, threshold_pct: float = 0.1) -> None:
        super().__init__()
        self.period = period
        self.threshold_pct = threshold_pct
        self.prices: deque[float] = deque(maxlen=period + 1)  # 最舊一筆即 period 筆前

    # ─── 參數支援 ────────────────────────────────────────────────────

    @property
    def params(self) -> dict[str, Any]:
        return {"period": self.period, "threshold_pct": self.threshold_pct, **self._base_params}

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "period",        "label": "回顧筆數",   "type": "number", "min": 1, "max": 200},
            {"key": "threshold_pct", "label": "進場門檻(%)", "type": "number", "min": 0, "max": 10},
            *self._base_param_schema,
        ]

    def _apply_params(self, params: dict[str, Any]) -> None:
        self.period = int(params.get("period", self.period))
        self.threshold_pct = float(params.get("threshold_pct", self.threshold_pct))
        self.prices = deque(maxlen=self.period + 1)  # 重建 buffer
        logger.info(
            "[momentum] 套用參數: period=%d  threshold_pct=%.2f",
            self.period, self.threshold_pct,
        )

    # ─── 策略邏輯 ────────────────────────────────────────────────────

    async def on_quote(self, quote: dict) -> None:
        price = float(quote["close"])
        self.prices.append(price)

        if len(self.prices) < self.period + 1:
            return

        ref = self.prices[0]  # period 筆之前的價格
        if ref == 0:
            return
        change_pct = (price - ref) / ref * 100.0

        if change_pct > self.threshold_pct and self.state.position <= 0:
            logger.info("[momentum] 動能 +%.2f%% → 做多 @ %.0f", change_pct, price)
            await self._go(1, price)

        elif change_pct < -self.threshold_pct and self.state.position >= 0:
            logger.info("[momentum] 動能 %.2f%% → 做空 @ %.0f", change_pct, price)
            await self._go(-1, price)
