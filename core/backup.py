"""資料庫自動備份：IV 歷史、每日日誌、成交紀錄都無法重建。

用 SQLite 線上備份 API（Connection.backup）：資料庫正被寫入也能拿到一致的快照，不需要停服務。
備份到 data/backup/YYYY-MM-DD/（BACKUP_DIR 可改到別的磁碟或雲端同步資料夾——備份和原檔放同一顆硬碟，
擋得住誤刪與資料庫損毀，擋不住硬碟壞掉），保留最近 BACKUP_KEEP_DAYS 天（預設 14）。

備份對象：market_state.db、trade_log.db（小、無法重建）。
不備份 ticks.db：大、且價格路徑可由 1 分 K 重建到分鐘精度。

排程：交易日 BACKUP_TIME（預設 14:00，盤後流程之後）由 daily_summary 的背景排程呼叫；
手動：python -m core.backup
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_FILES = ("market_state.db", "trade_log.db")
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def backup_root() -> Path:
    return Path(os.getenv("BACKUP_DIR") or DATA_DIR / "backup")


def backup_databases(day: str, *, data_dir: Path | None = None, dest_root: Path | None = None,
                     keep_days: int = 14, files: tuple[str, ...] = DB_FILES) -> dict[str, Any]:
    """把資料庫備份到 dest_root/<day>/，並清掉超過 keep_days 的舊備份。回傳每個檔案的結果。
    先寫成暫存檔、驗證完整性（PRAGMA integrity_check）後才換成正式檔，所以不會留下壞掉的備份。"""
    data_dir = data_dir or DATA_DIR
    dest = (dest_root or backup_root()) / day
    dest.mkdir(parents=True, exist_ok=True)
    results = []
    for name in files:
        src = data_dir / name
        if not src.exists():
            continue
        tmp = dest / f"{name}.{os.getpid()}.tmp"              # 加 pid：sim/live 兩個進程同時備份也不會互相踩
        try:
            with closing(sqlite3.connect(src, timeout=10)) as s, closing(sqlite3.connect(tmp)) as d:
                s.backup(d)
            with closing(sqlite3.connect(tmp)) as d:
                d.execute("PRAGMA journal_mode=DELETE")           # 副本會繼承來源的 WAL 旗標；改成單一獨立檔，丟進雲端同步資料夾也不會長出 -wal/-shm
                ok = d.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            if not ok:
                raise RuntimeError("備份的完整性檢查失敗")
            os.replace(tmp, dest / name)
            results.append({"file": name, "bytes": (dest / name).stat().st_size, "ok": True})
        except Exception as e:
            results.append({"file": name, "ok": False, "error": repr(e)})
            logger.error("備份 %s 失敗: %r", name, e)
        finally:
            tmp.unlink(missing_ok=True)
    return {"dir": str(dest), "files": results, "pruned": _prune(dest.parent, keep_days, day)}


def _prune(root: Path, keep_days: int, today: str) -> list[str]:
    """刪掉 root 底下「日期命名」且早於 today - keep_days 的備份資料夾（其他資料夾絕不動）。"""
    cutoff = date.fromisoformat(today) - timedelta(days=keep_days)
    pruned = []
    for p in sorted(root.iterdir()) if root.exists() else []:
        if p.is_dir() and _DAY_DIR.match(p.name):
            try:
                old = date.fromisoformat(p.name) < cutoff
            except ValueError:
                continue
            if old:
                shutil.rmtree(p, ignore_errors=True)
                pruned.append(p.name)
    return pruned


def _main() -> None:  # pragma: no cover
    from dotenv import load_dotenv
    load_dotenv()
    r = backup_databases(datetime.now().strftime("%Y-%m-%d"), keep_days=int(os.getenv("BACKUP_KEEP_DAYS", "14")))
    print(f"備份到 {r['dir']}")
    for f in r["files"]:
        print(f"  {f['file']}: " + (f"{f['bytes'] / 1024:.0f} KB ✓" if f["ok"] else f"失敗 {f['error']}"))
    if r["pruned"]:
        print("已清掉舊備份:", ", ".join(r["pruned"]))


if __name__ == "__main__":  # pragma: no cover
    _main()
