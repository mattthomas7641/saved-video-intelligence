"""Background worker: processes all PENDING/ERROR videos sequentially in a thread."""
import logging
import threading

from sqlmodel import select

from app.db import get_session
from app.models import Video, Status
from app.pipeline import process_video

log = logging.getLogger("worker")

_lock = threading.Lock()
_running = False
_current_video_id: int | None = None


def is_running() -> bool:
    return _running


def current_video_id() -> int | None:
    return _current_video_id


def start_processing(retry_errors: bool = False, limit: int | None = None) -> bool:
    """Kick off background processing if not already running. Returns True if started."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    thread = threading.Thread(target=_run_loop, args=(retry_errors, limit), daemon=True)
    thread.start()
    return True


def _run_loop(retry_errors: bool, limit: int | None) -> None:
    global _running, _current_video_id
    try:
        session = get_session()
        try:
            statuses = [Status.PENDING] + ([Status.ERROR] if retry_errors else [])
            processed = 0
            seen: set[int] = set()
            while limit is None or processed < limit:
                video = session.exec(
                    select(Video).where(Video.status.in_(statuses), Video.id.not_in(seen)).order_by(Video.id)
                ).first()
                if not video:
                    break
                _current_video_id = video.id
                seen.add(video.id)
                process_video(session, video)
                processed += 1
        finally:
            session.close()
    except Exception:  # noqa: BLE001
        log.exception("Worker loop crashed")
    finally:
        _current_video_id = None
        _running = False


def progress_summary() -> dict:
    session = get_session()
    try:
        counts = {}
        for status in Status:
            counts[status.value] = len(
                session.exec(select(Video.id).where(Video.status == status)).all()
            )
        total = sum(counts.values())
        return {
            "total": total,
            "counts": counts,
            "running": _running,
            "current_video_id": _current_video_id,
        }
    finally:
        session.close()
