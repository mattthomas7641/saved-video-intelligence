"""Run one video through download -> transcribe -> OCR -> analyze -> relevance."""
import logging
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from app.config import DELETE_VIDEO_AFTER_PROCESS
from app.models import Video, Status
from app import download as download_mod
from app import transcribe as transcribe_mod
from app import ocr as ocr_mod
from app import analyze as analyze_mod
from app import relevance as relevance_mod

log = logging.getLogger("pipeline")


def process_video(session: Session, video: Video) -> None:
    try:
        # 1. Download
        video.status = Status.DOWNLOADING
        _touch(session, video)

        result = download_mod.download_video(video.tiktok_url, video.id)
        video.local_video_path = result.video_path
        video.thumbnail_path = result.thumbnail_path
        video.author = result.author
        video.caption = result.caption
        video.hashtags = result.hashtags
        video.upload_date = result.upload_date
        video.duration_seconds = result.duration_seconds
        video.status = Status.DOWNLOADED
        _touch(session, video)

        if not video.local_video_path or not Path(video.local_video_path).exists():
            raise RuntimeError("Download did not produce a video file.")

        # 2. Transcribe
        video.status = Status.TRANSCRIBING
        _touch(session, video)
        video.transcript = transcribe_mod.transcribe(video.local_video_path)

        # 3. OCR (best-effort, never fatal)
        try:
            video.ocr_text = ocr_mod.extract_on_screen_text(video.local_video_path, video.duration_seconds)
        except Exception as e:  # noqa: BLE001
            log.warning("OCR failed for video %s: %s", video.id, e)
            video.ocr_text = ""

        video.status = Status.TRANSCRIBED
        _touch(session, video)

        # 4. Analyze
        video.status = Status.ANALYZING
        _touch(session, video)
        analysis = analyze_mod.analyze(
            caption=video.caption, hashtags=video.hashtags, author=video.author,
            transcript=video.transcript, ocr_text=video.ocr_text,
        )
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

        # 5. Relevance heuristic
        video.needs_verification = relevance_mod.is_stale(
            saved_date=video.saved_date, upload_date=video.upload_date,
            has_promo_code=video.has_promo_code, has_dated_offer=video.has_dated_offer,
        )

        video.status = Status.DONE
        video.error_message = None

        # 6. Cleanup
        if DELETE_VIDEO_AFTER_PROCESS and video.local_video_path:
            try:
                Path(video.local_video_path).unlink(missing_ok=True)
                video.local_video_path = None
            except OSError:
                pass

        _touch(session, video)

    except Exception as e:  # noqa: BLE001
        log.exception("Failed processing video %s", video.id)
        video.status = Status.ERROR
        video.error_message = str(e)[:500]
        _touch(session, video)


def _touch(session: Session, video: Video) -> None:
    video.updated_at = datetime.utcnow()
    session.add(video)
    session.commit()
    session.refresh(video)
