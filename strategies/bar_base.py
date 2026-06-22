"""K 棒策略基底：吃 1 分 K 而非逐 tick。

為什麼：tick 窗口的時間意義隨成交熱絡度漂移（日盤 20 ticks ≈ 2 秒、
夜盤可能好幾分鐘），K 棒策略的參數才有穩定意義、才能回測。

提供：
  - on_bar(bar) hook：每根 1 分 K 收完呼叫一次（只收 TMF 的 bar）
  - ATR 計算（Wilder 平滑）
  - ATR 移動停損（atr_trail_mult > 0 啟用）：多單 stop 只升不降、
    空單只降不升，bar 收盤價穿越就市價平倉——取代固定點數停損，
    波動大時自動放寬、盤整時自動收緊。

tick 路徑（on_quote）仍然活著：固定點數停損停利（base 的 _check_sl_tp）
照常每 tick 檢查，反應比 bar 級快。
"""
from __future__ import annotations

import logging
from abc import abstractmethod
from collections import deque
from typing import Any

import asyncio

import shioaji as sj

from core.bar_builder import Bar
from core.quote_hub import quote_hub
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class BarStrategy(BaseStrategy):
    """以 1 分 K 為主要訊號來源的策略基底。"""

    def __init__(self) -> None:
        super().__init__()
        self.atr_period: int = 14
        self.atr_trail_mult: float = 0.0   # 0 = 停用 ATR 移動停損
        self._trs: deque[float] = deque(maxlen=200)
        self._atr: float = 0.0
        self._prev_close: float = 0.0
        self._trail_stop: float = 0.0      # 持倉中的移動停損價

    # ── 參數 ──────────────────────────────────────────────────────
    @property
    def _bar_base_params(self) -> dict[str, Any]:
        return {"atr_period": self.atr_period, "atr_trail_mult": self.atr_trail_mult}

    @property
    def _bar_base_param_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "atr_period",     "label": "ATR 週期（分鐘）",          "type": "number", "min": 2, "max": 100},
            {"key": "atr_trail_mult", "label": "ATR 移動停損倍數（0=停用）", "type": "number", "min": 0, "max": 10},
        ]

    def _apply_bar_base_params(self, params: dict[str, Any]) -> None:
        self.atr_period = int(params.get("atr_period", self.atr_period))
        self.atr_trail_mult = float(params.get("atr_trail_mult", self.atr_trail_mult))

    # ── 生命週期 ──────────────────────────────────────────────────
    def start(self, loop: asyncio.AbstractEventLoop, params: dict[str, Any] | None = None) -> None:
        if params:
            self._apply_bar_base_params(params)
        super().start(loop, params)
        quote_hub.subscribe_strategy_bars(self.name, self._on_bar_async)

    async def stop(self) -> None:
        quote_hub.unsubscribe_strategy_bars(self.name)
        await super().stop()

    # ── bar 處理 ──────────────────────────────────────────────────
    async def _on_bar_async(self, bar: Bar) -> None:
        # 只處理本策略交易的合約（TMF）
        if not bar.code.startswith("TMF"):
            return

        self._update_atr(bar)

        if await self._check_atr_trail(bar):
            return

        try:
            await self.on_bar(bar)
        except Exception as e:
            logger.error("策略 [%s] on_bar 發生錯誤: %s", self.name, e)
            self.state.errors.append(str(e))

    def _update_atr(self, bar: Bar) -> None:
        if self._prev_close > 0:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        else:
            tr = bar.high - bar.low
        self._prev_close = bar.close
        self._trs.append(tr)
        if len(self._trs) < self.atr_period:
            return
        if self._atr == 0.0:
            recent = list(self._trs)[-self.atr_period:]
            self._atr = sum(recent) / self.atr_period
        else:
            # Wilder 平滑
            self._atr = (self._atr * (self.atr_period - 1) + tr) / self.atr_period

    async def _check_atr_trail(self, bar: Bar) -> bool:
        """ATR 移動停損。觸發平倉回傳 True。"""
        if self.atr_trail_mult <= 0 or self._atr <= 0 or self.state.position == 0:
            self._trail_stop = 0.0
            return False

        dist = self._atr * self.atr_trail_mult
        if self.state.position > 0:
            new_stop = bar.close - dist
            self._trail_stop = max(self._trail_stop, new_stop) if self._trail_stop else new_stop
            if bar.close <= self._trail_stop:
                return await self._trail_exit(bar)
        else:
            new_stop = bar.close + dist
            self._trail_stop = min(self._trail_stop, new_stop) if self._trail_stop else new_stop
            if bar.close >= self._trail_stop:
                return await self._trail_exit(bar)
        return False

    async def _trail_exit(self, bar: Bar) -> bool:
        prev_pos = self.state.position
        prev_entry = self.state.entry_price
        qty = abs(prev_pos)
        action = sj.constant.Action.Sell if prev_pos > 0 else sj.constant.Action.Buy
        # 先歸零再 await，擋報價/下一根 bar 重入（與 base._check_sl_tp 同模式）
        self.state.position = 0
        self.state.entry_price = 0.0
        self.state.unrealized_pnl = 0.0
        stop_px = self._trail_stop
        self._trail_stop = 0.0
        try:
            await self.place_order(action, qty)
        except Exception as e:
            self.state.position = prev_pos
            self.state.entry_price = prev_entry
            logger.error("策略 [%s] ATR 停損平倉失敗，還原部位: %s", self.name, e)
            self.state.errors.append(f"ATR 停損平倉失敗: {e}")
            return True
        pts = (bar.close - prev_entry) * (1 if prev_pos > 0 else -1)
        self.state.realized_pnl += pts * qty * self.point_value
        logger.info("策略 [%s] ATR 移動停損出場 @ %.0f（stop=%.0f, %+.0f點）",
                    self.name, bar.close, stop_px, pts)
        self._event(f"ATR 停損出場 @ {bar.close:.0f}（{pts:+.0f}點）")
        return True

    # ── 子類別實作 ────────────────────────────────────────────────
    @abstractmethod
    async def on_bar(self, bar: Bar) -> None:
        """每根 1 分 K 收完呼叫（已過濾為 TMF）。"""
        ...

    async def on_quote(self, quote: sj.QuoteFOPv1) -> None:
        """預設 tick 路徑無事可做（SL/TP 在 base._on_quote_async 已檢查）。"""
        return
