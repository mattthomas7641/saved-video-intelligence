"""Per-video stages. Collect (download, transcribe, OCR) is free; analyze is the paid Claude call."""
import logging
import random
import time
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app import analyze as analyze_mod
from app import download as download_mod
from app import ocr as ocr_mod
from app import relevance as relevance_mod
from app import transcribe as transcribe_mod
from app.config import DELETE_VIDEO_AFTER_PROCESS
from app.models import Action, ActionStatus, ActionType, Status, Video
from app.throttle import looks_like_throttle, throttle

log = logging.getLogger("pipeline")


def collect_video(session: Session, video: Video, should_stop=lambda: False) -> None:
    """Download, transcribe and read on-screen text. Leaves the video TRANSCRIBED."""
    try:
        throttle.wait(should_stop)
        time.sleep(random.uniform(0.2, 0.9))  # stay polite when several workers run at once
        video.status = Status.DOWNLOADING
        _touch(session, video)

        result = download_mod.download_video(video.tiktok_url, video.id)
        throttle.report_ok()
        video.local_video_path = result.video_path
        video.thumbnail_path = result.thumbnail_path
        video.author = result.author
        video.caption = result.caption
        video.hashtags = result.hashtags
        video.upload_date = result.upload_date
        video.duration_seconds = result.duration_seconds

        if video.local_video_path and Path(video.local_video_path).exists():
            video.status = Status.TRANSCRIBING
            _touch(session, video)
            video.transcript = transcribe_mod.transcribe(video.local_video_path)
            try:
                video.ocr_text = ocr_mod.extract_on_screen_text(video.local_video_path, video.duration_seconds)
            except Exception as e:  # noqa: BLE001 - OCR is best effort
                log.warning("OCR failed for video %s: %s", video.id, e)
                video.ocr_text = ""
        else:
            video.transcript = ""  # photo/slideshow post: analyze the caption alone
            video.ocr_text = ""

        video.status = Status.TRANSCRIBED
        video.error_message = None
        if DELETE_VIDEO_AFTER_PROCESS and video.local_video_path:
            Path(video.local_video_path).unlink(missing_ok=True)
            video.local_video_path = None
        _touch(session, video)

    except Exception as e:  # noqa: BLE001
        message = str(e)
        if looks_like_throttle(message):
            throttle.report_block()
            video.status = Status.PENDING  # try again later, not a permanent failure
            video.error_message = "TikTok slowed us down; will retry."
        else:
            log.exception("Collect failed for video %s", video.id)
            video.status = Status.ERROR
            video.error_message = message[:500]
        _touch(session, video)


def _queue_action_if_new(session: Session, video: Video, analysis: analyze_mod.AnalysisResult) -> None:
    """Idempotent: one Action per video, created the first time analysis flags it actionable."""
    if not analysis.is_actionable:
        return
    existing = session.exec(select(Action.id).where(Action.video_id == video.id)).first()
    if existing:
        return
    session.add(Action(
        video_id=video.id,
        action_type=ActionType(analysis.action_type),
        status=ActionStatus.QUEUED,
        brief=analysis.action_brief,
    ))


def apply_analysis(session: Session, video: Video, analysis: analyze_mod.AnalysisResult, batch: bool = False) -> float:
    video.category = analysis.category
    video.summary = analysis.summary
    video.tags = ",".join(analysis.tags)
    video.worth_rewatching_score = analysis.worth_rewatching_score
    video.worth_rewatching_reason = analysis.worth_rewatching_reason
    video.has_promo_code = analysis.has_promo_code
    video.promo_code_text = analysis.promo_code_text
    video.has_dated_offer = analysis.has_dated_offer
    video.offer_deadline_text = analysis.offer_deadline_text
    video.mentions_link_in_bio = analysis.mentions_link_in_bio
    video.key_facts = ",".join(analysis.key_facts)
    video.needs_verification = relevance_mod.is_stale(
        saved_date=video.saved_date, upload_date=video.upload_date,
        has_promo_code=video.has_promo_code, has_dated_offer=video.has_dated_offer)
    video.input_tokens = analysis.input_tokens
    video.output_tokens = analysis.output_tokens
    video.cost_usd = analyze_mod.cost_usd(analysis.input_tokens, analysis.output_tokens, batch=batch)
    video.batch_id = None
    video.status = Status.DONE
    video.error_message = None
    _queue_action_if_new(session, video, analysis)
    return video.cost_usd


def analyze_video(session: Session, video: Video) -> float:
    """Live Claude analysis of a TRANSCRIBED video. Returns the cost in dollars."""
    video.status = Status.ANALYZING
    _touch(session, video)
    try:
        cost = apply_analysis(session, video, analyze_mod.analyze_video(video))
    except analyze_mod.FatalAnalysisError:
        video.status = Status.TRANSCRIBED  # nothing is wrong with the video; keep it for later
        _touch(session, video)
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("Analysis failed for video %s", video.id)
        video.status = Status.TRANSCRIBED
        video.error_message = ("Analysis failed: " + str(e))[:500]
        _touch(session, video)
        return 0.0
    _touch(session, video)
    return cost


def process_video(session: Session, video: Video) -> float:
    collect_video(session, video)
    return analyze_video(session, video) if video.status == Status.TRANSCRIBED else 0.0


def _touch(session: Session, video: Video) -> None:
    video.updated_at = datetime.utcnow()
    session.add(video)
    session.commit()
    session.refresh(video)
