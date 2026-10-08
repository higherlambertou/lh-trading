"""逐分鐘外/內盤成交統計——市場指標視覺化工具的資料基礎。

視覺化需求文件規定：必須先用歷史回放做統計驗證，才能即時上線。驗證需要三個指標「合在一起的歷史」，
但外/內盤比例原本只活在記憶體（LiveState，重啟就沒了），沒有任何歷史可回放。

這裡把 TMF 的真實成交每分鐘彙總成一列存進 market_state.db 的 flow_1m：買/賣/不明 的筆數與口數，加上該分鐘的成交價 OHLC。
任何長度的視窗占比（最近 20/100/300 筆或 N 分鐘）都能由它重算；Hurst 與 IV 一天才變一次，存在 indicator_daily，回放時用日期對上即可。

只存聚合、不存逐筆。「真實成交」的判定與即時面板、scalp(flow_source=1) 共用 TradeDetector（volume>0 且 total_volume 增加）。
開盤前試算行情在 QuoteHub 就被隔離了，不會進來（見 core/quote_hub.py）。

執行緒模型：feed() 在 event loop 執行緒呼叫（純記憶體）；完成的分鐘丟進 queue，writer thread 批次寫入，SQLite 連線不跨執行緒。
收盤後最後一分鐘沒有下一筆成交來「關閉」它，所以 flush_stale() 由 MarketStateService.tick() 每 30 秒呼叫一次補上。
RECORD_FLOW=false 可整個關掉。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

from core.live_state import TradeDetector
from core.market_store import MarketStore

logger = logging.getLogger(__name__)

PREFIX = "TMF"               # 追蹤的合約前綴（與即時面板一致）；回補用的券商連續合約也是它
QUEUE_MAX = 5000             # 約 3 天的分鐘數，writer 卡住時的緩衝
STALE_AFTER_SEC = 75         # 一分鐘過了這麼久還沒有下一筆成交，就當它已完成
FLUSH_BATCH = 50


@dataclass
class MinuteAcc:
    """一個合約一分鐘內的累計。即時（逐筆）與回補（歷史逐筆）共用同一套規則，兩邊的數字才可比。"""
    ts: int                              # 分鐘起點（真實 epoch 秒）
    buy_n: int = 0
    sell_n: int = 0
    unk_n: int = 0
    buy_vol: int = 0
    sell_vol: int = 0
    unk_vol: int = 0
    open: float | None = None
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0

    @property
    def trades(self) -> int:
        return self.buy_n + self.sell_n + self.unk_n

    def add(self, price: float, volume: int, tick_type: int) -> None:
        """tick_type：1 外盤（買方主動）、2 內盤（賣方主動）、其他＝無法判斷。"""
        volume = int(volume)
        if tick_type == 1:
            self.buy_n += 1
            self.buy_vol += volume
        elif tick_type == 2:
            self.sell_n += 1
            self.sell_vol += volume
        else:
            self.unk_n += 1
            self.unk_vol += volume
        if self.open is None:
            self.open = self.high = self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price

    def row(self) -> dict[str, Any]:
        return {"ts": self.ts, "buy_n": self.buy_n, "sell_n": self.sell_n, "unk_n": self.unk_n,
                "buy_vol": self.buy_vol, "sell_vol": self.sell_vol, "unk_vol": self.unk_vol,
                "open": self.open, "high": self.high, "low": self.low, "close": self.close}


def aggregate_trades(ts: Sequence[float], price: Sequence[float], volume: Sequence[int], tick_type: Sequence[int],
                     drop: Sequence[bool] | None = None) -> list[dict[str, Any]]:
    """逐筆成交（ts 為真實 epoch 秒、需已依時間排序）→ 每分鐘一列。volume<=0 或價格<=0 略過；drop 為 True 的筆也略過
    （如開盤前試算時段）。回補與本機 ticks.db 彙總都用它，規則與即時的 FlowRecorder 一致。"""
    out: list[dict[str, Any]] = []
    cur: MinuteAcc | None = None
    for i in range(len(ts)):
        if (drop is not None and drop[i]) or volume[i] <= 0 or price[i] <= 0:
            continue
        minute = int(ts[i]) // 60 * 60
        if cur is None or minute > cur.ts:
            if cur is not None:
                out.append(cur.row())
            cur = MinuteAcc(minute)
        cur.add(float(price[i]), int(volume[i]), int(tick_type[i]))      # 比 cur 更早的遲到成交併進目前這分鐘（極少見）
    if cur is not None:
        out.append(cur.row())
    return out


class FlowRecorder:
    def __init__(self, store: MarketStore | None = None, prefix: str = PREFIX) -> None:
        self.enabled = os.getenv("RECORD_FLOW", "true").lower() == "true"
        self.prefix = prefix
        self._store = store
        self._detector = TradeDetector()                  # 只在 event loop 執行緒呼叫，不需要鎖
        self._cur: MinuteAcc | None = None
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dropped = 0

    # ── 報價進入點呼叫（QuoteHub._inject_quote，event loop 執行緒）──────────────
    def feed(self, code: str, price: float, volume: int, total_volume: int, tick_type: int, ts: float) -> None:
        if not self.enabled or self._thread is None or not code.startswith(self.prefix) or not price:
            return
        if not self._detector.is_trade(code, volume, total_volume):
            return                                        # 報價更新／重複回報：不是成交
        minute = int(ts) // 60 * 60
        if self._cur is not None and minute > self._cur.ts:
            self._emit(self._cur)
            self._cur = None
        if self._cur is None:
            self._cur = MinuteAcc(minute)
        self._cur.add(price, volume, tick_type)

    def flush_stale(self, now: float | None = None) -> None:
        """目前這分鐘已經過了 STALE_AFTER_SEC 還沒有下一筆成交（收盤、午休）→ 送出去。"""
        if self._cur is not None and (now if now is not None else time.time()) - self._cur.ts >= STALE_AFTER_SEC:
            self._emit(self._cur)
            self._cur = None

    def _emit(self, acc: MinuteAcc) -> None:
        try:
            self._q.put_nowait(acc.row())
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning("FlowRecorder 佇列滿，累計丟棄 %d 分鐘", self._dropped)

    # ── 生命週期 ──────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled:
            logger.info("FlowRecorder 停用（RECORD_FLOW=false）")
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="flow-recorder", daemon=True)
        self._thread.start()
        logger.info("FlowRecorder 已啟動（每分鐘外/內盤成交統計 → market_state.db）")

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._cur is not None:                         # 關機時，進行中的那一分鐘也存下來
            self._emit(self._cur)
            self._cur = None
        self._stop.set()
        self._thread.join(timeout=5)
        self._thread = None

    # ── writer thread ─────────────────────────────────────────────
    def _run(self) -> None:
        store = self._store or MarketStore()
        buf: list[dict[str, Any]] = []
        while True:
            try:
                buf.append(self._q.get(timeout=1.0))
            except queue.Empty:
                pass
            while len(buf) < FLUSH_BATCH:                 # 有多少拿多少，一次寫
                try:
                    buf.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if buf:
                try:
                    store.upsert_flow_1m(self.prefix, buf)
                except Exception as e:
                    logger.error("FlowRecorder 寫入失敗（丟棄 %d 分鐘）: %r", len(buf), e)
                buf.clear()
            elif self._stop.is_set():
                break


flow_recorder = FlowRecorder()

