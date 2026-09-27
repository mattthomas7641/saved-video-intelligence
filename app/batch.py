"""Anthropic Message Batches: half-price analysis for videos that are already collected."""
import logging
import threading
import time

import anthropic
from anthropic import Anthropic
from sqlmodel import select

from app.analyze import build_params, parse_message, get_api_key, cost_usd
from app.db import get_session
from app.models import Video, Status
from app.pipeline import apply_analysis

log = logging.getLogger("batch")
DEFAULT_BATCH_COST = 0.0014  # per video, until we have real numbers
_poller_started = False


def observed_cost(batch: bool) -> float | None:
    """Average real cost of already-analyzed videos, if there are enough to trust."""
    session = get_session()
    try:
        costs = list(session.exec(select(Video.cost_usd).where(Video.cost_usd.is_not(None)).limit(400)).all())
    finally:
        session.close()
    if len(costs) < 15:
        return None
    avg = sum(costs) / len(costs)
    return avg if not batch else avg * 0.5


def submit(limit: int | None, cap_usd: float | None) -> dict:
    """Send collected videos (newest saves first) to the Batch API, as many as the cap allows."""
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key."}
    session = get_session()
    try:
        videos = session.exec(select(Video).where(Video.status == Status.TRANSCRIBED)
                              .order_by(Video.saved_date.desc(), Video.id)).all()
        if limit:
            videos = videos[:limit]
        per = observed_cost(batch=True) or DEFAULT_BATCH_COST
        if cap_usd is not None:
            videos = videos[: int(cap_usd / per)]
        if not videos:
            return {"ok": False, "error": "Nothing collected yet, or the cap is too low for even one video."}

        requests = [{"custom_id": str(v.id), "params": build_params(v)} for v in videos]
        try:
            batch = Anthropic(api_key=api_key).messages.batches.create(requests=requests)
        except anthropic.AuthenticationError:
            return {"ok": False, "error": "Anthropic rejected the API key."}
        except anthropic.BadRequestError as e:
            msg = "Out of Anthropic credit. Add credit first." if "credit" in str(e).lower() else str(e)[:200]
            return {"ok": False, "error": msg}

        for v in videos:
            v.status, v.batch_id, v.error_message = Status.SUBMITTED, batch.id, None
            session.add(v)
        session.commit()
    finally:
        session.close()
    start_poller()
    return {"ok": True, "count": len(videos), "estimate": round(len(videos) * per, 2), "batch_id": batch.id}


def poll_once() -> int:
    """Collect results from any finished batches. Returns how many videos were completed."""
    api_key = get_api_key()
    session = get_session()
    applied = 0
    try:
        batch_ids = {b for b in session.exec(select(Video.batch_id).where(Video.status == Status.SUBMITTED)).all() if b}
        if not batch_ids or not api_key:
            return 0
        client = Anthropic(api_key=api_key)
        for batch_id in batch_ids:
            try:
                if client.messages.batches.retrieve(batch_id).processing_status != "ended":
                    continue
                for entry in client.messages.batches.results(batch_id):
                    video = session.get(Video, int(entry.custom_id))
                    if video is None or video.status != Status.SUBMITTED:
                        continue
                    if entry.result.type == "succeeded":
                        try:
                            apply_analysis(video, parse_message(entry.result.message), batch=True)
                            applied += 1
                        except Exception as e:  # noqa: BLE001
                            video.status, video.error_message = Status.TRANSCRIBED, f"Batch parse failed: {e}"[:500]
                    else:
                        video.status = Status.TRANSCRIBED  # can be analyzed again live or in a new batch
                        video.error_message = f"Batch result: {entry.result.type}"
                    video.batch_id = None
                    session.add(video)
                session.commit()
            except Exception:  # noqa: BLE001
                log.exception("Polling batch %s failed", batch_id)
    finally:
        session.close()
    return applied


def _loop() -> None:
    while True:
        try:
            poll_once()
        except Exception:  # noqa: BLE001
            log.exception("Batch poller error")
        time.sleep(45)


def start_poller() -> None:
    global _poller_started
    if not _poller_started:
        _poller_started = True
        threading.Thread(target=_loop, daemon=True).start()
