"""VWAP 偏離回歸策略。

邏輯：
  - 當日 VWAP = Σ(典型價×量) / Σ量，逐根 1 分 K 累計（每日重置）。
  - 價格偏離 VWAP 超過 dev_pts → 反向進場，賭回歸：
      高於 VWAP + dev_pts → 做空；低於 VWAP - dev_pts → 做多。
  - 回到 VWAP 附近（exit_ratio × dev_pts 以內）→ 獲利平倉。
  - 失敗保護交給 base 的固定停損（stop_loss_pts）或 ATR 移動停損。

適合震盪日；強趨勢日會連續逆勢挨打——務必設 stop_loss_pts 和
daily_max_loss，或搭配 max_trades_per_day 限制攤次。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.bar_builder import Bar
from strategies.bar_base import BarStrategy

logger = logging.getLogger(__name__)


class VWAPRevertStrategy(BarStrategy):
    name = "vwap_revert"

    def __init__(self) -> None:
        super().__init__()
        self.dev_pts: int = 40          # 偏離多少點觸發反向進場
        self.exit_ratio: float = 0.25   # 回到 dev_pts×此比例以內就平倉
        self.warmup_bars: int = 10      # 開盤先累積幾根 bar 再開始（VWAP 未穩前不交易）
        # 預設風控
        self.stop_loss_pts = self.stop_loss_pts or 60

        self._day: str = ""
        self._cum_pv: float = 0.0
        self._cum_vol: float = 0.0
        self._bars_today: int = 0

    # ── 參數 ──────────────────────────────────────────────────────
    @property
    def params(self) -> dict[str, Any]:
        return {
            "dev_pts": self.dev_pts,
            "exit_ratio": round(self.exit_ratio, 2),
            "warmup_bars": self.warmup_bars,
            **self._bar_base_params,
            **self._base_params,
        }

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "dev_pts",     "label": "進場偏離點數",            "type": "number", "min": 10,   "max": 300},
            {"key": "exit_ratio",  "label": "平倉比例（×偏離點數）",    "type": "number", "min": 0,    "max": 1},
            {"key": "warmup_bars", "label": "暖機 K 棒數",             "type": "number", "min": 1,    "max": 60},
            *self._bar_base_param_schema,
            *self._base_param_schema,
        ]

    def _apply_params(self, params: dict[str, Any]) -> None:
        self.dev_pts     = int(params.get("dev_pts",     self.dev_pts))
        self.exit_ratio  = float(params.get("exit_ratio", self.exit_ratio))
        self.warmup_bars = int(params.get("warmup_bars", self.warmup_bars))

    # ── 邏輯 ──────────────────────────────────────────────────────
    @property
    def vwap(self) -> float:
        return self._cum_pv / self._cum_vol if self._cum_vol > 0 else 0.0

    async def on_bar(self, bar: Bar) -> None:
        today = datetime.fromtimestamp(bar.ts).strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._cum_pv = 0.0
            self._cum_vol = 0.0
            self._bars_today = 0

        typical = (bar.high + bar.low + bar.close) / 3
        vol = max(bar.volume, 1)
        self._cum_pv += typical * vol
        self._cum_vol += vol
        self._bars_today += 1

        if self._bars_today < self.warmup_bars:
            return

        vwap = self.vwap
        dev = bar.close - vwap

        # 持倉中：回到 VWAP 附近就獲利平倉
        if self.state.position != 0:
            if abs(dev) <= self.dev_pts * self.exit_ratio:
                prev_pos = self.state.position
                prev_entry = self.state.entry_price
                pts = (bar.close - prev_entry) * (1 if prev_pos > 0 else -1)
                logger.info("[vwap] 回歸 VWAP（dev=%.0f）→ 平倉 @ %.0f（%+.0f點）",
                            dev, bar.close, pts)
                # _go 的反向會自動平倉，但這裡是「平倉不反手」→ 直接送平倉單
                import shioaji as sj
                action = sj.constant.Action.Sell if prev_pos > 0 else sj.constant.Action.Buy
                qty = abs(prev_pos)
                self.state.position = 0
                self.state.entry_price = 0.0
                self.state.unrealized_pnl = 0.0
                try:
                    await self.place_order(action, qty)
                except Exception as e:
                    self.state.position = prev_pos
                    self.state.entry_price = prev_entry
                    self.state.errors.append(f"回歸平倉失敗: {e}")
                    return
                self.state.realized_pnl += pts * qty * self.point_value
                self._event(f"回歸平倉 @ {bar.close:.0f}（{pts:+.0f}點）")
            return

        # 空手：偏離夠大才反向進場
        if dev >= self.dev_pts:
            logger.info("[vwap] 高於 VWAP %.0f 點（VWAP=%.0f）→ 做空 @ %.0f", dev, vwap, bar.close)
            await self._go(-1, bar.close)
        elif dev <= -self.dev_pts:
            logger.info("[vwap] 低於 VWAP %.0f 點（VWAP=%.0f）→ 做多 @ %.0f", -dev, vwap, bar.close)
            await self._go(1, bar.close)
