"""tick 落地（SQLite）——回測資料來源。

報價已經免費推進來了，不存白不存：有了逐筆歷史才能 replay 回測、
驗證策略參數，而不是用真錢猜。

執行緒模型：
  - record() 由 shioaji 報價執行緒呼叫：只做 queue.put_nowait（無鎖等待，
    滿了就丟棄該筆並計數，絕不阻塞報價路徑）。
  - 獨立 writer thread 批次寫入（每秒或每 500 筆 commit 一次），
    SQLite 連線只屬於 writer thread，不跨執行緒共用。

開關：環境變數 RECORD_TICKS=false 可關（預設開）。
瘦身：行情事件裡只有 ~25% 是真實成交，其餘是「報價更新」（volume=0，價格與 tick_type 都是上一筆成交的，
沒有新資訊），預設不存（RECORD_QUOTE_UPDATES=true 可恢復）；價格路徑只由成交決定，所以不損失任何資訊。
同時新增 total_volume 欄位（日累計成交量，可用來去除重複回報與核對漏筆）。
資料庫：data/ticks.db（已加入 .gitignore）。舊資料可用 python -m core.tick_store compact 預覽/清掉報價更新列。
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

from core.live_state import TradeDetector

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ticks.db"
FLUSH_INTERVAL = 1.0     # 秒
FLUSH_BATCH = 500        # 筆
QUEUE_MAX = 50_000       # 佇列上限（writer 卡住時的緩衝，約幾分鐘量）
INSERT_SQL = "INSERT INTO ticks (code, ts, price, volume, tick_type, total_volume) VALUES (?,?,?,?,?,?)"


class TickRecorder:
    def __init__(self) -> None:
        self._q: queue.Queue[tuple] = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dropped = 0
        self.enabled = os.getenv("RECORD_TICKS", "true").lower() == "true"
        self.keep_quote_updates = os.getenv("RECORD_QUOTE_UPDATES", "false").lower() == "true"
        self._detector = TradeDetector()                  # 只在 event loop 執行緒呼叫，不需要鎖

    # ── 報價進入點呼叫（QuoteHub._inject_quote）──────────────────
    def record(self, code: str, ts: float, price: float, volume: int, tick_type: int,
               total_volume: int = 0) -> None:
        if not self.enabled or self._thread is None:
            return
        if not self.keep_quote_updates and not self._detector.is_trade(code, volume, total_volume):
            return                                        # 報價更新／重複回報：沒有新資訊，不存
        try:
            self._q.put_nowait((code, ts, price, volume, tick_type, total_volume))
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
                tick_type INTEGER NOT NULL,
                total_volume INTEGER NOT NULL DEFAULT 0
            )"""
        )
        if "total_volume" not in [r[1] for r in conn.execute("PRAGMA table_info(ticks)")]:
            conn.execute("ALTER TABLE ticks ADD COLUMN total_volume INTEGER NOT NULL DEFAULT 0")   # 舊資料庫升級
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
                    conn.executemany(INSERT_SQL, buf)
                    conn.commit()
                    buf.clear()
                except Exception as e:
                    logger.error("TickRecorder 寫入失敗（丟棄 %d 筆）: %s", len(buf), e)
                    buf.clear()
                last_flush = now

        # 收尾：佇列裡還沒搬進 buf 的也要寫掉（stop() 之後不該讓已收下的 tick 憑空消失）
        while True:
            try:
                buf.append(self._q.get_nowait())
            except queue.Empty:
                break
        if buf:
            try:
                conn.executemany(INSERT_SQL, buf)
                conn.commit()
            except Exception:
                pass
        conn.close()


tick_recorder = TickRecorder()


def compact(path: Path | None = None, keep_days: int | None = None, apply: bool = False) -> dict:
    """瘦身：刪掉 volume=0 的報價更新列（價格與方向都是上一筆成交的，沒有資訊量）；
    keep_days 給定時，另外刪掉更早的成交。預設只預覽（dry-run），apply=True 才真的刪並 VACUUM。
    VACUUM 需要獨佔鎖——請在服務停止時執行。"""
    path = Path(path or DB_PATH)
    size_before = path.stat().st_size
    conn = sqlite3.connect(path, timeout=30)
    try:
        total = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        quote_rows = conn.execute("SELECT COUNT(*) FROM ticks WHERE volume = 0").fetchone()[0]
        cutoff = time.time() - keep_days * 86400 if keep_days else None
        old_rows = (conn.execute("SELECT COUNT(*) FROM ticks WHERE volume > 0 AND ts < ?", (cutoff,)).fetchone()[0]
                    if cutoff else 0)
        if apply:
            conn.execute("DELETE FROM ticks WHERE volume = 0")
            if cutoff:
                conn.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
    finally:
        conn.close()
    return {"applied": apply, "total_rows": total, "quote_update_rows": quote_rows, "old_trade_rows": old_rows,
            "size_before": size_before, "size_after": path.stat().st_size}


def _main() -> None:  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="ticks.db 瘦身（請先停止服務）")
    ap.add_argument("cmd", choices=["compact"])
    ap.add_argument("--keep-days", type=int, default=None, help="另外刪掉這麼多天以前的成交（預設不刪）")
    ap.add_argument("--apply", action="store_true", help="真的執行（預設只預覽）")
    a = ap.parse_args()
    r = compact(keep_days=a.keep_days, apply=a.apply)
    mb = lambda n: f"{n / 1e6:.1f} MB"  # noqa: E731
    print(f"共 {r['total_rows']:,} 列；報價更新（volume=0）{r['quote_update_rows']:,} 列"
          + (f"；{a.keep_days} 天前的成交 {r['old_trade_rows']:,} 列" if a.keep_days else ""))
    if r["applied"]:
        print(f"已清理並 VACUUM：{mb(r['size_before'])} → {mb(r['size_after'])}")
    else:
        print("（預覽，沒有改動；加 --apply 才會執行，且請先停止服務）")


if __name__ == "__main__":  # pragma: no cover
    _main()
