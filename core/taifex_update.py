"""期交所日資料自動更新（update.md 發現 65）：TX 日行情（data/taifex/tx）每個平日一次、TXO 每日 ATM IV（data/taifex/vrp）每 7 天一次。

為什麼：開盤前預期日盤振幅（發現 55）的模型用 data/taifex/vrp 擬合；結算隔天的盤前摘要要用 TX 日行情補夜盤報酬（core/daily_brief）。
這兩份以前只在研究時手動下載，停在 2026-10-12。
怎麼跑：只有正式盤後端跑（模擬盤不重複抓）。平日 RUN_AFTER（15:10，期交所日盤資料公布後）以後，在**獨立子行程**（nice -n 10）執行既有的下載工具：
  python -m core.oi_study fetch --only tx、python -m core.vrp_study fetch
——下載與解析是網路與 CPU 工作，不能在交易行程裡跑（會搶報價與下單的 GIL）。已經完整的月份不重抓（core/oi_study.period_complete）。
成功的日期記在 data/taifex/update_state.json（重啟不重抓）；失敗 RETRY_SEC 後再試，一天最多 MAX_TRIES 次。TAIFEX_UPDATE=false 關閉。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from core.daytrade import TAIPEI

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "taifex" / "update_state.json"
RUN_AFTER = (15, 10)
POLL_SEC = 300.0
RETRY_SEC = 1800.0
MAX_TRIES = 3
TIMEOUT_SEC = 900.0
JOBS: dict[str, dict[str, Any]] = {
    "tx": {"every_days": 1, "argv": ["-m", "core.oi_study", "fetch", "--only", "tx"], "text": "TX 日行情"},
    "vrp": {"every_days": 7, "argv": ["-m", "core.vrp_study", "fetch"], "text": "TXO 每日 ATM IV"},
}


def enabled() -> bool:
    return os.getenv("TAIFEX_UPDATE", "true").lower() != "false" and os.getenv("SIMULATION", "false").lower() != "true"


def command(job: str) -> list[str]:
    return ["nice", "-n", "10", sys.executable, *JOBS[job]["argv"]]


async def run_subprocess(job: str) -> tuple[bool, str]:
    """跑一個下載工具（獨立子行程、低優先權）；回傳（成功？, 最後一行輸出）。逾時就殺掉。"""
    proc = await asyncio.create_subprocess_exec(*command(job), cwd=str(ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"超過 {TIMEOUT_SEC:.0f} 秒，已中止"
    except asyncio.CancelledError:                      # 後端關閉：下載子行程不能變成孤兒
        proc.kill()
        raise
    lines = [x for x in out.decode("utf-8", errors="replace").splitlines() if x.strip()]
    return proc.returncode == 0, (lines[-1] if lines else "")[:200]


class TaifexUpdater:
    def __init__(self, state_path: Path = STATE_PATH, run: Callable[[str], Awaitable[tuple[bool, str]]] = run_subprocess) -> None:
        self.state_path, self.run = state_path, run
        self.state: dict[str, dict[str, Any]] = self._load()
        self.tries: dict[tuple[str, str], int] = {}          # (工作, 日期) → 今天試了幾次
        self.retry_at: dict[str, float] = {}

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as e:
            logger.warning("期交所資料更新：狀態檔寫不進去：%r", e)

    def due(self, now: datetime) -> list[str]:
        """現在該跑的工作。平日 RUN_AFTER 以後；距上次成功 ≥ every_days 天；今天沒試滿 MAX_TRIES 次、也不在等重試。"""
        now = now.astimezone(TAIPEI)
        if now.weekday() >= 5 or (now.hour, now.minute) < RUN_AFTER:
            return []
        today = now.date()
        out = []
        for job, spec in JOBS.items():
            last = (self.state.get(job) or {}).get("ok_date")
            try:
                fresh = last is not None and (today - date.fromisoformat(last)).days < spec["every_days"]
            except ValueError:
                fresh = False
            if fresh or self.tries.get((job, today.isoformat()), 0) >= MAX_TRIES or now.timestamp() < self.retry_at.get(job, 0.0):
                continue
            out.append(job)
        return out

    async def tick(self, now: datetime) -> dict[str, bool]:
        done: dict[str, bool] = {}
        for job in self.due(now):
            key = (job, now.astimezone(TAIPEI).date().isoformat())
            self.tries[key] = self.tries.get(key, 0) + 1
            t0 = time.time()
            try:
                ok, msg = await self.run(job)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"{type(e).__name__}: {e}"
            done[job] = ok
            if ok:
                self.state[job] = {"ok_date": key[1], "at": time.time(), "summary": msg}
                self.retry_at.pop(job, None)
                self._save()
                logger.info("期交所資料更新：%s 完成（%.0f 秒）：%s", JOBS[job]["text"], time.time() - t0, msg)
            else:
                self.retry_at[job] = now.timestamp() + RETRY_SEC
                logger.warning("期交所資料更新：%s 失敗（今天第 %d 次，%s）：%s", JOBS[job]["text"], self.tries[key],
                               "30 分鐘後再試" if self.tries[key] < MAX_TRIES else "今天不再試", msg)
        return done


async def taifex_update_loop(updater: Optional[TaifexUpdater] = None) -> None:
    if not enabled():
        logger.info("期交所資料自動更新：%s", "模擬盤不跑（由正式盤負責）" if os.getenv("SIMULATION", "false").lower() == "true" else "已關閉（TAIFEX_UPDATE=false）")
        return
    updater = updater or TaifexUpdater()
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await updater.tick(datetime.now(TAIPEI))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("期交所資料更新迴圈例外（已忽略）")
