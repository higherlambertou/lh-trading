"""資料庫自動備份：IV 歷史、每日日誌、成交紀錄都無法重建。

用 SQLite 線上備份 API（Connection.backup）：資料庫正被寫入也能拿到一致的快照，不需要停服務。
備份到 data/backup/YYYY-MM-DD/（BACKUP_DIR 可改到別的磁碟或雲端同步資料夾——備份和原檔放同一顆硬碟，
擋得住誤刪與資料庫損毀，擋不住硬碟壞掉），保留最近 BACKUP_KEEP_DAYS 天（預設 14）。

備份對象：market_state.db、trade_log.db、signals.db、option_spread.db（小、無法重建；option_spread.db＝盤中選擇權價差取樣，2026-10-22 起才備份）。
不備份 ticks.db：大、且價格路徑可由 1 分 K 重建到分鐘精度。

**異地副本**（update.md 發現 66）：BACKUP_MIRROR_DIR 設成雲端同步資料夾裡的一個資料夾（例如 Google Drive 的
~/Library/CloudStorage/GoogleDrive-帳號/我的雲端硬碟/lh-trading-backup），本機備份做完後再複製一份過去、一樣保留 BACKUP_KEEP_DAYS 天。
那個資料夾的**上一層必須已經存在**（Google Drive 沒開時掛載點不在——這時不建立、只記「略過」，免得在掛載點建一個一般資料夾）；
複製失敗不算備份失敗（本機那份已經好了），結果記在備份根目錄的 mirror_state.json（系統健康面板會顯示）。

排程：交易日 BACKUP_TIME（預設 14:00，盤後流程之後）由 daily_summary 的背景排程呼叫；
手動：python -m core.backup
"""
from __future__ import annotations

import json
import logging
import os
import time
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_FILES = ("market_state.db", "trade_log.db", "signals.db", "option_spread.db")   # signals.db＝影子訊號的前向資料、option_spread.db＝盤中價差取樣（都補不回來）
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


MIRROR_STATE = "mirror_state.json"


def backup_root() -> Path:
    return Path(os.getenv("BACKUP_DIR") or DATA_DIR / "backup")


def mirror_root() -> Optional[Path]:
    raw = os.getenv("BACKUP_MIRROR_DIR", "").strip().strip("\"'")
    return Path(raw).expanduser() if raw else None


def mirror_backup(day: str, local_day: Path, mirror: Path, keep_days: int, files: tuple[str, ...] = None) -> dict[str, Any]:  # type: ignore[assignment]
    """把本機這一天的備份（local_day 底下）複製到 mirror/<day>/。回傳 {"status": "ok"|"skipped"|"failed", ...}；永遠不丟例外。"""
    files = files or DB_FILES
    if not mirror.parent.exists():
        return {"status": "skipped", "reason": f"雲端資料夾不在：{mirror.parent}（Google Drive 沒開？）"}
    try:
        dest = mirror / day
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        for name in files:
            src = local_day / name
            if not src.exists():
                continue
            tmp = dest / f"{name}.{os.getpid()}.tmp"
            try:
                shutil.copyfile(src, tmp)
                if tmp.stat().st_size != src.stat().st_size:
                    raise OSError("複製後大小不一樣")
                os.replace(tmp, dest / name)
            finally:
                tmp.unlink(missing_ok=True)
            copied.append(name)
        return {"status": "ok", "dir": str(dest), "files": copied, "pruned": _prune(mirror, keep_days, day)}
    except Exception as e:  # noqa: BLE001
        logger.warning("備份的雲端副本失敗（本機那份已完成）: %r", e)
        return {"status": "failed", "error": repr(e)}


def _write_mirror_state(root: Path, day: str, res: dict[str, Any]) -> None:
    """最近一次雲端副本的結果（系統健康面板讀）。成功時記 last_ok_day；失敗、略過保留上一次成功的日子。"""
    p = root / MIRROR_STATE
    try:
        old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        old = {}
    state = {"last_attempt_day": day, "last_attempt_at": time.time(), "last_status": res.get("status"),
             "reason": res.get("reason") or res.get("error"), "last_ok_day": day if res.get("status") == "ok" else old.get("last_ok_day"),
             "dir": res.get("dir") or old.get("dir")}
    try:
        root.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("雲端副本狀態檔寫不進去：%r", e)


def backup_databases(day: str, *, data_dir: Path | None = None, dest_root: Path | None = None,
                     keep_days: int = 14, files: tuple[str, ...] = DB_FILES, mirror: Path | None | bool = True) -> dict[str, Any]:
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
    out: dict[str, Any] = {"dir": str(dest), "files": results, "pruned": _prune(dest.parent, keep_days, day)}
    m = mirror_root() if mirror is True else (mirror or None)                 # True＝看 BACKUP_MIRROR_DIR；None／False＝不做副本
    if m is not None:
        out["mirror"] = mirror_backup(day, dest, Path(m), keep_days, files)
        _write_mirror_state(dest.parent, day, out["mirror"])
    return out


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
    m = r.get("mirror")
    if m:
        print(f"雲端副本：{m['status']}" + (f" → {m.get('dir')}" if m.get("dir") else "") + (f"（{m.get('reason') or m.get('error')}）" if m.get("reason") or m.get("error") else ""))
    else:
        print("雲端副本：沒有設定 BACKUP_MIRROR_DIR")


if __name__ == "__main__":  # pragma: no cover
    _main()
