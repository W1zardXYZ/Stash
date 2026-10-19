"""A small persistent job queue backed by the jobs table, executed by worker threads.

Jobs survive restarts (running jobs are re-queued on startup). Handlers register with
@handler("type") and receive a JobContext for progress reporting and cancellation checks.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import text

from .config import settings
from .db import session_scope
from .models import Job

log = logging.getLogger("stash.jobs")

_handlers: dict[str, Callable[["JobContext"], dict | None]] = {}
_claim_lock = threading.Lock()
_wake = threading.Event()


def handler(job_type: str):
    def deco(fn):
        _handlers[job_type] = fn
        return fn

    return deco


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, job_id: int, job_type: str, payload: dict, asset_id: int | None, source_id: int | None):
        self.id = job_id
        self.type = job_type
        self.payload = payload
        self.asset_id = asset_id
        self.source_id = source_id
        self._log: list[str] = []
        self._last_flush = 0.0
        self._progress = 0.0
        self._message = ""

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log.append(f"[{stamp}] {msg}")
        self._flush()

    def progress(self, value: float, message: str | None = None) -> None:
        self._progress = max(0.0, min(1.0, value))
        if message is not None:
            self._message = message
        self._flush()

    def _flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_flush < 1.0:
            return
        self._last_flush = now
        with session_scope() as db:
            job = db.get(Job, self.id)
            if job:
                job.progress = self._progress
                job.message = self._message
                job.log = "\n".join(self._log[-500:])

    def cancelled(self) -> bool:
        now = time.time()
        if now - getattr(self, "_cancel_checked", 0.0) < 2.0:
            return getattr(self, "_cancel_state", False)
        self._cancel_checked = now
        with session_scope() as db:
            job = db.get(Job, self.id)
            self._cancel_state = job is None or job.status == "cancelled"
        return self._cancel_state

    def check_cancel(self) -> None:
        if self.cancelled():
            raise JobCancelled()

    def finish_log(self) -> str:
        return "\n".join(self._log[-500:])


def enqueue(
    job_type: str,
    payload: dict | None = None,
    asset_id: int | None = None,
    source_id: int | None = None,
    priority: int = 0,
    dedupe: bool = True,
    db=None,
) -> int:
    """Queue a job. With dedupe, an identical queued/running job is reused."""
    payload = payload or {}
    key = f"{job_type}:{asset_id}:{source_id}:{sorted(payload.items()) if dedupe else time.time()}"
    key = key[:300]

    def _do(s):
        if dedupe:
            existing = s.query(Job).filter(Job.dedupe_key == key, Job.status.in_(("queued", "running"))).first()
            if existing:
                return existing.id
        job = Job(type=job_type, payload=payload, asset_id=asset_id, source_id=source_id, priority=priority, dedupe_key=key)
        s.add(job)
        s.flush()
        return job.id

    if db is not None:
        jid = _do(db)
    else:
        with session_scope() as s:
            jid = _do(s)
    _wake.set()
    return jid


def _claim() -> tuple[int, str, dict, int | None, int | None] | None:
    with _claim_lock, session_scope() as db:
        row = db.execute(
            text("SELECT id FROM jobs WHERE status='queued' ORDER BY priority DESC, id ASC LIMIT 1")
        ).first()
        if not row:
            return None
        job = db.get(Job, row[0])
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.progress = 0.0
        return job.id, job.type, dict(job.payload or {}), job.asset_id, job.source_id


def _run_one(claimed) -> None:
    job_id, job_type, payload, asset_id, source_id = claimed
    ctx = JobContext(job_id, job_type, payload, asset_id, source_id)
    fn = _handlers.get(job_type)
    status, result, message = "done", {}, ""
    try:
        if fn is None:
            raise RuntimeError(f"no handler for job type {job_type}")
        result = fn(ctx) or {}
    except JobCancelled:
        status, message = "cancelled", "cancelled"
    except Exception as e:
        status = "error"
        message = str(e)[:2000]
        ctx.log(traceback.format_exc())
        detail = getattr(e, "log", None)
        if detail:
            ctx._log.append(detail[-20000:])
        log.warning("job %s (%s) failed: %s", job_id, job_type, e)
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        if job.status == "cancelled":
            status = "cancelled"
        job.status = status
        job.result = result
        job.progress = 1.0 if status == "done" else job.progress
        job.message = message or ctx._message
        job.log = ctx.finish_log()
        job.finished_at = datetime.now(timezone.utc)


class WorkerPool:
    def __init__(self, n: int):
        self.n = max(1, n)
        self.threads: list[threading.Thread] = []
        self.stop = threading.Event()

    def start(self) -> None:
        with session_scope() as db:
            db.execute(text("UPDATE jobs SET status='queued' WHERE status='running'"))
        for i in range(self.n):
            t = threading.Thread(target=self._loop, name=f"stash-worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                claimed = _claim()
            except Exception as e:  # database busy etc.
                log.warning("claim failed: %s", e)
                claimed = None
            if claimed is None:
                _wake.wait(timeout=2.0)
                _wake.clear()
                continue
            _run_one(claimed)

    def shutdown(self) -> None:
        self.stop.set()
        _wake.set()


def run_pending_sync(max_jobs: int = 10000) -> int:
    """Run queued jobs in the current thread (tests / CLI)."""
    n = 0
    while n < max_jobs:
        claimed = _claim()
        if not claimed:
            break
        _run_one(claimed)
        n += 1
    return n


pool: WorkerPool | None = None


def start_workers() -> WorkerPool:
    global pool
    pool = WorkerPool(settings.workers)
    pool.start()
    return pool
