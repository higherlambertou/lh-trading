"""回測 API：把 core.backtest 放在背景「子行程」跑，前端輪詢進度與結果。

為什麼用子行程、不用執行緒：回測是純 Python 的 CPU 密集計算，在交易行程裡跑會跟下單、報價派發搶 GIL，
延遲一旦拉長，停損就慢。子行程另外用 nice 降低優先權；同時最多一個工作、預設只開 2 個平行行程（BACKTEST_WORKERS）。
工作狀態只在記憶體（重啟後端就沒了；重跑一次幾秒到幾分鐘）。BACKTEST_API=false 可整個關掉。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)
router = APIRouter()

ROOT = Path(__file__).resolve().parent.parent
ENABLED = os.getenv("BACKTEST_API", "true").lower() == "true"
WORKERS = max(1, int(os.getenv("BACKTEST_WORKERS", "2")))
JOB_TIMEOUT_SEC = float(os.getenv("BACKTEST_TIMEOUT_SEC", "1800"))
KEEP_JOBS = 10
LOG_TAIL = 40
KILL_GRACE_SEC = 3.0

STRATEGY_NAMES = ("ma_cross", "breakout", "rsi", "bollinger", "momentum", "orb", "vwap_revert")


class BacktestRequest(BaseModel):
    strategy: str = "all"
    cost: float = Field(4.0, ge=0, le=50)
    split: float = Field(0.6, ge=0.3, le=0.9)
    null: int = Field(0, ge=0, le=12)
    walk_forward: Optional[str] = None            # "TRAIN,TEST"，例如 "120,20"
    top: int = Field(3, ge=1, le=10)
    source: Literal["bars", "flow"] = "bars"
    session: Literal["day", "night", "all"] = "day"

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v: str) -> str:
        if v != "all" and v not in STRATEGY_NAMES:
            raise ValueError(f"strategy 必須是 all 或 {', '.join(STRATEGY_NAMES)}")
        return v

    @field_validator("walk_forward")
    @classmethod
    def _wf(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        m = re.fullmatch(r"(\d{1,3}),(\d{1,3})", v)
        if not m or int(m.group(1)) < 5 or int(m.group(2)) < 1:
            raise ValueError("walk_forward 要寫成 TRAIN,TEST（天數），例如 120,20")
        return v


def build_argv(req: BacktestRequest, out_path: str) -> list[str]:
    argv = [sys.executable, "-m", "core.backtest", "--strategy", req.strategy, "--cost", str(req.cost),
            "--split", str(req.split), "--source", req.source, "--session", req.session, "--top", str(req.top),
            "--workers", str(WORKERS), "--json", out_path]
    if req.null:
        argv += ["--null", str(req.null)]
    if req.walk_forward:
        argv += ["--walk-forward", req.walk_forward]
    nice = shutil.which("nice")
    return [nice, "-n", "10", *argv] if nice and os.name == "posix" else argv


class Busy(Exception):
    pass


class Job:
    def __init__(self, req: BacktestRequest) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.req = req
        self.status = "running"            # running | done | failed | cancelled
        self.started = time.time()
        self.finished: Optional[float] = None
        self.done = 0
        self.total = len(STRATEGY_NAMES) if req.strategy == "all" else 1
        self.log: list[str] = []
        self.error = ""
        self.reports: Optional[list[dict[str, Any]]] = None
        self.proc: Optional[subprocess.Popen] = None
        self.cancelled = False
        self.timed_out = False

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        end = self.finished or time.time()
        d = {"id": self.id, "status": self.status, "request": self.req.model_dump(), "started_at": self.started,
             "elapsed_sec": round(end - self.started, 1), "progress": {"done": self.done, "total": self.total},
             "error": self.error, "log_tail": self.log[-LOG_TAIL:] if full else [], "has_reports": self.reports is not None}
        if full:
            d["reports"] = self.reports
        return d


class BacktestManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    # ── 對外 ──────────────────────────────────────────────────────
    def start(self, req: BacktestRequest) -> Job:
        with self._lock:
            if any(j.status == "running" for j in self._jobs.values()):
                raise Busy()
            job = Job(req)
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > KEEP_JOBS:                 # 只留最近幾個；跑到一半的不會被收掉（上面保證同時只有一個）
                old = self._order.pop(0)
                self._jobs.pop(old, None)
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"backtest-{job.id}").start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status != "running":
            return False
        job.cancelled = True
        self._kill(job)
        return True

    def shutdown(self) -> None:
        for j in self.list():
            if j.status == "running":
                j.cancelled = True
                self._kill(j)

    # ── 內部 ──────────────────────────────────────────────────────
    @staticmethod
    def _kill(job: Job) -> None:
        proc = job.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)       # 整個行程群組（含平行的子行程）一起收
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        def hard() -> None:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        t = threading.Timer(KILL_GRACE_SEC, hard)
        t.daemon = True
        t.start()

    def _run(self, job: Job) -> None:
        fd, out_path = tempfile.mkstemp(prefix="backtest_", suffix=".json")
        os.close(fd)
        timer: Optional[threading.Timer] = None
        try:
            argv = build_argv(job.req, out_path)
            job.proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        bufsize=1, start_new_session=True)
            if job.cancelled:           # 取消／關機發生在子行程建好之前：那時 _kill 看到 proc 還沒有就跳過了，這裡補殺，否則會把整個回測跑完
                self._kill(job)
            timer = threading.Timer(JOB_TIMEOUT_SEC, self._on_timeout, args=(job,))
            timer.daemon = True
            timer.start()
            assert job.proc.stdout is not None
            for line in job.proc.stdout:
                line = line.rstrip("\n")
                job.log.append(line)
                if line.startswith("══ "):
                    job.done += 1
            rc = job.proc.wait()
            if job.cancelled:
                job.status, job.error = "cancelled", "已取消"
            elif job.timed_out:
                job.status, job.error = "failed", f"超過 {JOB_TIMEOUT_SEC:g} 秒，已中止"
            elif rc != 0:
                job.status = "failed"
                job.error = "\n".join(l for l in job.log[-8:] if l.strip()) or f"回測行程異常結束（代碼 {rc}）"
            else:
                try:
                    job.reports = json.loads(Path(out_path).read_text(encoding="utf-8"))
                    job.status = "done"
                    job.done = job.total
                except Exception as e:
                    job.status, job.error = "failed", f"讀不到回測結果：{e}"
        except Exception as e:
            logger.exception("回測工作失敗")
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            if timer:
                timer.cancel()
            job.finished = time.time()
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def _on_timeout(self, job: Job) -> None:
        job.timed_out = True
        self._kill(job)


manager = BacktestManager()


def _require_enabled() -> None:
    if not ENABLED:
        raise HTTPException(503, "回測 API 已關閉（BACKTEST_API=false）")


@router.get("/info")
def info() -> dict[str, Any]:
    """本機有多少資料可回測：bars_1m（日盤 K 棒）與 flow_1m（TMF，含夜盤）。"""
    _require_enabled()
    from core.market_store import MarketStore
    ms = MarketStore()
    out: dict[str, Any] = {"bars": {"code": "TXF", "days": 0, "first": None, "last": None},
                           "flow": {"code": "TMF", "minutes": 0, "first_ts": None, "last_ts": None},
                           "workers": WORKERS, "timeout_sec": JOB_TIMEOUT_SEC}
    try:
        with sqlite3.connect(f"file:{ms.path}?mode=ro", uri=True, timeout=5) as c:
            n, lo, hi = c.execute("SELECT COUNT(DISTINCT date), MIN(date), MAX(date) FROM bars_1m WHERE code='TXF'").fetchone()
            out["bars"].update(days=n, first=lo, last=hi)
            n, lo, hi = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM flow_1m WHERE code='TMF'").fetchone()
            out["flow"].update(minutes=n, first_ts=lo, last_ts=hi)
    except sqlite3.Error as e:
        out["error"] = str(e)
    return out


@router.post("/run")
def run(req: BacktestRequest) -> dict[str, Any]:
    _require_enabled()
    try:
        job = manager.start(req)
    except Busy:
        raise HTTPException(409, "已經有一個回測在跑，等它跑完（或先取消）")
    return {"id": job.id}


@router.get("/jobs")
def jobs() -> list[dict[str, Any]]:
    _require_enabled()
    return [j.to_dict(full=False) for j in manager.list()]


@router.get("/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    _require_enabled()
    j = manager.get(job_id)
    if not j:
        raise HTTPException(404, "找不到這個回測工作（後端重啟過，或已超過保留的數量）")
    return j.to_dict(full=True)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str) -> dict[str, Any]:
    _require_enabled()
    if not manager.get(job_id):
        raise HTTPException(404, "找不到這個回測工作")
    return {"cancelled": manager.cancel(job_id)}
