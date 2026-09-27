"""Background job runner: several videos in flight at once, with a spend cap and clean stop."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from sqlmodel import select

from app.config import DEFAULT_WORKERS
from app.db import get_session
from app.models import Video, Status
from app import pipeline
from app.analyze import FatalAnalysisError
from app.throttle import throttle

log = logging.getLogger("worker")

# What each job mode works on.
_MODES = {
    "full": [Status.PENDING],         # collect + live analysis
    "collect": [Status.PENDING],      # free phase only
    "analyze": [Status.TRANSCRIBED],  # live analysis of already-collected videos
}


class Job:
    def __init__(self, mode, limit, workers, cap_usd, retry_errors):
        self.mode, self.limit, self.workers, self.cap_usd = mode, limit, workers, cap_usd
        self.retry_errors = retry_errors
        self.stop = threading.Event()
        self.running = True
        self.reason = ""          # why it ended early, shown to the user
        self.queued = 0           # videos this job set out to handle
        self.started = 0
        self.finished = 0
        self.failed = 0
        self.spent = 0.0
        self.started_at = time.time()
        self.ended_at = None
        self.lock = threading.Lock()


_job: Job | None = None
_guard = threading.Lock()


def start_job(mode="full", limit=None, workers=None, cap_usd=None, retry_errors=False) -> bool:
    global _job
    with _guard:
        if _job and _job.running:
            return False
        throttle.reset()
        _job = Job(mode, limit, max(1, min(int(workers or DEFAULT_WORKERS), 8)), cap_usd, retry_errors)
    threading.Thread(target=_run, args=(_job,), daemon=True).start()
    return True


def stop_job() -> None:
    if _job and _job.running:
        _job.stop.set()


def start_processing(retry_errors: bool = False, limit: int | None = None) -> bool:
    """Original entry point used by the dashboard's quick 'Analyze next'."""
    return start_job("full", limit=limit, retry_errors=retry_errors)


def is_running() -> bool:
    return bool(_job and _job.running)


def _candidate_ids(job: Job) -> list[int]:
    statuses = list(_MODES[job.mode]) + ([Status.ERROR] if job.retry_errors and job.mode != "analyze" else [])
    session = get_session()
    try:
        # Newest saves first: they matter most if the run is cut short.
        q = select(Video.id).where(Video.status.in_(statuses)).order_by(Video.saved_date.desc(), Video.id)
        ids = session.exec(q).all()
    finally:
        session.close()
    return ids[: job.limit] if job.limit else ids


def _run(job: Job) -> None:
    ex = ThreadPoolExecutor(max_workers=job.workers)
    inflight: set = set()
    try:
        ids = _candidate_ids(job)
        job.queued = len(ids)
        for vid in ids:
            if job.stop.is_set():
                job.reason = job.reason or "Stopped."
                break
            if job.cap_usd is not None and job.spent >= job.cap_usd:
                job.reason = "Reached your spending cap."
                break
            while len(inflight) >= job.workers * 2 and not job.stop.is_set():
                done, _ = wait(inflight, timeout=1, return_when=FIRST_COMPLETED)
                inflight -= done
            job.started += 1
            inflight.add(ex.submit(_task, job, vid))
    except Exception:  # noqa: BLE001
        log.exception("Job crashed")
        job.reason = "Stopped after an unexpected error."
    finally:
        wait(inflight)
        ex.shutdown()
        job.running = False
        job.ended_at = time.time()


def _task(job: Job, video_id: int) -> None:
    if job.stop.is_set():
        return
    session = get_session()
    try:
        video = session.get(Video, video_id)
        if video is None:
            return
        cost = 0.0
        if job.mode in ("full", "collect"):
            pipeline.collect_video(session, video, should_stop=job.stop.is_set)
            if throttle.consecutive_blocks >= 12:
                job.reason = "TikTok keeps blocking downloads. Wait an hour, then resume."
                job.stop.set()
        if job.mode in ("full", "analyze") and video.status == Status.TRANSCRIBED and not job.stop.is_set():
            cost = pipeline.analyze_video(session, video)
        with job.lock:
            job.spent += cost
            ok = video.status in (Status.DONE, Status.TRANSCRIBED) and not video.error_message
            job.finished += 1
            job.failed += 0 if ok else 1
    except FatalAnalysisError as e:
        job.reason = str(e)
        job.stop.set()
    except Exception:  # noqa: BLE001
        log.exception("Task failed for video %s", video_id)
        with job.lock:
            job.failed += 1
    finally:
        session.close()


def recover_stuck() -> None:
    """After a restart or crash, put half-finished videos back in line."""
    session = get_session()
    try:
        for v in session.exec(select(Video).where(Video.status.in_(
                [Status.DOWNLOADING, Status.DOWNLOADED, Status.TRANSCRIBING, Status.ANALYZING]))).all():
            v.status = Status.TRANSCRIBED if v.status == Status.ANALYZING else Status.PENDING
            session.add(v)
        session.commit()
    finally:
        session.close()


def job_status() -> dict:
    j = _job
    if j is None:
        return {"active": False}
    elapsed = (j.ended_at or time.time()) - j.started_at
    rate = j.finished / elapsed * 60 if elapsed > 5 and j.finished else 0.0
    remaining = max(0, j.queued - j.finished)
    return {
        "active": True, "running": j.running, "mode": j.mode, "workers": j.workers,
        "queued": j.queued, "finished": j.finished, "failed": j.failed,
        "spent": round(j.spent, 4), "cap": j.cap_usd, "reason": j.reason,
        "rate_per_min": round(rate, 1),
        "eta_seconds": int(remaining / rate * 60) if rate else None,
        "throttled_for": throttle.paused_for(),
        "stopping": j.stop.is_set() and j.running,
    }


def progress_summary() -> dict:
    session = get_session()
    try:
        counts = {status.value: 0 for status in Status}
        from sqlalchemy import func
        for status, n in session.exec(select(Video.status, func.count()).group_by(Video.status)).all():
            counts[status.value if hasattr(status, "value") else str(status).lower()] = n
        return {"total": sum(counts.values()), "counts": counts, "running": is_running(), "current_video_id": None}
    finally:
        session.close()
