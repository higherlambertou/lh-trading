"""tick → K 棒聚合器。

為什麼需要：策略若直接吃 tick，「N 期均線」的時間意義隨成交熱絡度漂移
（日盤 20 ticks ≈ 2 秒、夜盤可能是好幾分鐘），同一組參數在不同時段
代表完全不同的東西。聚合成固定時間 K 棒後，參數才有穩定意義，
也才能用歷史資料回測。

執行緒模型：
  - feed() 由 shioaji 報價執行緒（C thread）呼叫——只做純 Python
    dict/deque 操作（GIL 保護），不做 I/O、不等鎖。
  - 完成的 bar 由 QuoteHub 用 call_soon_threadsafe 派發到 event loop，
    跟現有的 quote 派發同一條路。
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

HISTORY_BARS = 500   # 每檔合約保留最近幾根（夠算 ATR/均線 warmup）


@dataclass
class Bar:
    code: str
    ts: int        # 該根 K 棒起始時間（epoch 秒，對齊 interval）
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def range(self) -> float:
        return self.high - self.low


class BarBuilder:
    """把逐筆 tick 聚合成固定秒數的 K 棒（預設 60s = 1 分 K）。"""

    def __init__(self, interval_sec: int = 60) -> None:
        self.interval = interval_sec
        self._building: dict[str, Bar] = {}            # code → 進行中的 bar
        self._history: dict[str, deque[Bar]] = {}      # code → 已完成 bars

    def feed(self, code: str, price: float, volume: int, ts: float) -> Bar | None:
        """餵一筆 tick。若這筆 tick 跨入新的分鐘，回傳「剛完成」的上一根 bar。

        ⚠️ 在報價執行緒呼叫：只能做純記憶體操作。
        """
        bucket = int(ts) // self.interval * self.interval
        cur = self._building.get(code)

        if cur is None:
            self._building[code] = Bar(code, bucket, price, price, price, price, volume)
            return None

        if cur.ts == bucket:
            if price > cur.high:
                cur.high = price
            if price < cur.low:
                cur.low = price
            cur.close = price
            cur.volume += volume
            return None

        # 跨分鐘：上一根完成，開新的一根
        self._building[code] = Bar(code, bucket, price, price, price, price, volume)
        hist = self._history.setdefault(code, deque(maxlen=HISTORY_BARS))
        hist.append(cur)
        return cur

    def history(self, code: str, n: int | None = None) -> list[Bar]:
        """最近 n 根已完成的 bar（由舊到新）。n=None 取全部（至多 HISTORY_BARS）。"""
        hist = self._history.get(code)
        if not hist:
            return []
        bars = list(hist)
        return bars if n is None else bars[-n:]

    def current(self, code: str) -> Bar | None:
        """進行中（尚未收完）的那根 bar。"""
        return self._building.get(code)
