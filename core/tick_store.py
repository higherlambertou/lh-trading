"""tick 落地（SQLite）——回測資料來源。

報價已經免費推進來了，不存白不存：有了逐筆歷史才能 replay 回測、
驗證策略參數，而不是用真錢猜。

執行緒模型：
  - record() 由 shioaji 報價執行緒呼叫：只做 queue.put_nowait（無鎖等待，
    滿了就丟棄該筆並計數，絕不阻塞報價路徑）。
  - 獨立 writer thread 批次寫入（每秒或每 500 筆 commit 一次），
    SQLite 連線只屬於 writer thread，不跨執行緒共用。

開關：環境變數 RECORD_TICKS=false 可關（預設開）。
資料庫：data/ticks.db（已加入 .gitignore）。
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ticks.db"
FLUSH_INTERVAL = 1.0     # 秒
FLUSH_BATCH = 500        # 筆
QUEUE_MAX = 50_000       # 佇列上限（writer 卡住時的緩衝，約幾分鐘量）


class TickRecorder:
    def __init__(self) -> None:
        self._q: queue.Queue[tuple] = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dropped = 0
        self.enabled = os.getenv("RECORD_TICKS", "true").lower() == "true"

    # ── 報價執行緒呼叫 ────────────────────────────────────────────
    def record(self, code: str, ts: float, price: float, volume: int, tick_type: int) -> None:
        if not self.enabled or self._thread is None:
            return
        try:
            self._q.put_nowait((code, ts, price, volume, tick_type))
        except queue.Full:
            self._dropped += 1
            if self._dropped % 10_000 == 1:
                logger.warning("TickRecorder 佇列滿，累計丟棄 %d 筆", self._dropped)

    # ── 生命週期 ──────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled:
            logger.info("TickRecorder 停用（RECORD_TICKS=false）")
            return
        if self._thread is not None:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tick-recorder", daemon=True)
        self._thread.start()
        logger.info("TickRecorder 已啟動 → %s", DB_PATH)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        self._thread = None

    # ── writer thread ─────────────────────────────────────────────
    def _run(self) -> None:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ticks (
                code      TEXT    NOT NULL,
                ts        REAL    NOT NULL,
                price     REAL    NOT NULL,
                volume    INTEGER NOT NULL,
                tick_type INTEGER NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_code_ts ON ticks(code, ts)")
        # WAL：寫入不擋讀（回測腳本可同時讀同一個 db）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()

        buf: list[tuple] = []
        last_flush = time.monotonic()
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
                buf.append(item)
            except queue.Empty:
                pass
            now = time.monotonic()
            if buf and (len(buf) >= FLUSH_BATCH or now - last_flush >= FLUSH_INTERVAL):
                try:
                    conn.executemany("INSERT INTO ticks VALUES (?,?,?,?,?)", buf)
                    conn.commit()
                    buf.clear()
                except Exception as e:
                    logger.error("TickRecorder 寫入失敗（丟棄 %d 筆）: %s", len(buf), e)
                    buf.clear()
                last_flush = now

        # 收尾 flush
        if buf:
            try:
                conn.executemany("INSERT INTO ticks VALUES (?,?,?,?,?)", buf)
                conn.commit()
            except Exception:
                pass
        conn.close()


tick_recorder = TickRecorder()
