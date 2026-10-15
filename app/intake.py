"""Shared intake for anything you send to the agent, whichever doorway it came
through: the "Send to Sidequest" iOS Shortcut (app/ntfy_intake.py) or Telegram
(app/telegram_bot.py).

What you can send:
- a video link from TikTok, Instagram, YouTube or X: downloaded, transcribed
  and OCR'd like a saved TikTok (if the download is blocked, it's judged from
  the link instead)
- any other link (article, GitHub repo, product page): read by the agent
- a screenshot or photo: OCR'd here and shown to the model as an image
- plain text: researched as-is

Every item becomes a Video row (the library's one table) and goes to the
research lane:
a small thread pool, separate from bulk jobs, running
pipeline.process_shared_video. What differs is how progress and results get
back to you, which each doorway supplies as a Reporter.
"""
import hashlib
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
from sqlmodel import select

log = logging.getLogger(__name__)

LANE_WORKERS = 2   # shared videos processed at once, independent of bulk jobs
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}
MAX_IMAGE_BYTES = 15_000_000

URL_RE = re.compile(r"https?://[^\s<>\"']+")
TIKTOK_HOST_RE = re.compile(r"^https?://([\w-]+\.)*tiktok(v)?\.com/", re.I)
# Links yt-dlp can download as a video. Everything else is read as a page.
MEDIA_RE = re.compile(
    r"^https?://([\w-]+\.)*(tiktok(v)?\.com|instagram\.com/(reels?|p|tv)/|youtube\.com/(shorts/|watch)|youtu\.be/"
    r"|(x|twitter)\.com/\w+/status/|facebook\.com/(reel|watch)|fb\.watch/|threads\.net/)", re.I)

_lane = ThreadPoolExecutor(max_workers=LANE_WORKERS, thread_name_prefix="shared-video")
_in_flight: set[int] = set()
_in_flight_lock = threading.Lock()


class Reporter:
    """How one doorway reports back. The default reports nothing (dashboard re-runs)."""
    chat_id: int | None = None
    message_id: int | None = None

    def progress(self, text: str) -> None:
        pass

    def finished(self, action, video) -> None:
        pass

    def failed(self, text: str) -> None:
        log.warning("Shared video failed: %s", text)


# ---------------- link handling ----------------

def extract_link(text: str) -> tuple[str | None, str]:
    """First URL in the message, plus everything else as your note. TikTok's
    share text ("Check out this video…") is dropped so it doesn't pose as a note."""
    m = URL_RE.search(text or "")
    if not m:
        return None, (text or "").strip()
    url = m.group(0).rstrip(").,!?")
    note = (text[: m.start()] + " " + text[m.end():]).strip()
    if re.fullmatch(r"(?is).*(check out|watch).*tiktok.*", note) or note.lower().startswith("#"):
        note = ""
    return url, note


def is_tiktok(url: str) -> bool:
    return bool(TIKTOK_HOST_RE.match(url))


def canonical_tiktok_url(url: str, http: httpx.Client | None = None) -> str:
    """Short share links (vm./vt.tiktok.com, tiktok.com/t/…) redirect to the real
    video URL; resolve them so the same video shared twice dedupes."""
    from app.ingest import _normalize

    direct = _normalize(url)
    if direct:
        return direct
    client = http or httpx.Client(timeout=15, follow_redirects=True,
                                  headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36"})
    try:
        final = str(client.head(url).url)
        return _normalize(final) or url
    except httpx.HTTPError:
        return url
    finally:
        if http is None:
            client.close()


def is_media(url: str) -> bool:
    return bool(MEDIA_RE.match(url))


def canonical_url(url: str) -> str:
    """Drop tracking parameters from Instagram/YouTube-Shorts/X links (?igsh=, ?exln=...)
    so the same post shared twice is one item. Other links keep their query string."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    host = parts.netloc.lower()
    if any(h in host for h in ("instagram.com", "x.com", "twitter.com", "threads.net")) or "/shorts/" in parts.path:
        path = parts.path if parts.path.endswith("/") or "x.com" in host or "twitter" in host else parts.path + "/"
        return urlunsplit((parts.scheme, host.removeprefix("m.").replace("instagram.com", "www.instagram.com")
                           .replace("www.www.", "www."), path, "", ""))
    return url


def ingest_shared_link(url: str, note: str) -> int:
    """Find or create the Video for a shared link and mark it as shared."""
    canonical = canonical_tiktok_url(url) if is_tiktok(url) else canonical_url(url)
    # A GitHub/article/product link has nothing to download or transcribe: the
    # agent reads the page itself, so it skips straight to analysis.
    return _upsert(canonical, note, downloadable=is_media(url), caption=None if is_media(url) else canonical)


def ingest_shared_text(text: str) -> int:
    """Plain text with no link (a note, a name, a recommendation someone sent)."""
    digest = hashlib.sha256(text.strip().encode()).hexdigest()[:20]
    return _upsert(f"text:{digest}", "", downloadable=False, caption=text.strip()[:4000])


def ingest_shared_image(data: bytes, filename: str, note: str = "") -> int:
    """A screenshot or photo: saved under data/shared/, OCR'd, and later shown to the model."""
    from app import ocr
    from app.config import DATA_DIR

    digest = hashlib.sha256(data).hexdigest()[:20]
    suffix = Path(filename or "").suffix.lower() if Path(filename or "").suffix.lower() in IMAGE_SUFFIXES else ".jpg"
    folder = DATA_DIR / "shared"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}{suffix}"
    path.write_bytes(data)
    return _upsert(f"image:{digest}", note, downloadable=False, caption="(shared screenshot or photo)",
                   ocr_text=ocr.extract_image_text(str(path)), image_path=str(path))


def _upsert(key: str, note: str, downloadable: bool, caption: str | None = None, ocr_text: str = "",
            image_path: str | None = None) -> int:
    from app.db import get_session
    from app.models import Status, Video

    with get_session() as session:
        video = session.exec(select(Video).where(Video.tiktok_url == key)).first()
        if not video:
            video = Video(tiktok_url=key, saved_date=datetime.utcnow())
            if not downloadable:
                video.status = Status.TRANSCRIBED
                video.caption = caption
                video.transcript = ""
                video.ocr_text = ocr_text
                video.thumbnail_path = image_path
        video.source = "telegram"  # "shared on purpose", whichever doorway it came through
        if note:
            video.user_note = note
        video.updated_at = datetime.utcnow()
        session.add(video)
        session.commit()
        session.refresh(video)
        _ensure_placeholder_action(session, video.id)
        return video.id


def _ensure_placeholder_action(session, video_id: int) -> None:
    """Give a new share its card at once ("Looking into it"); analysis fills in the type."""
    from app.models import Action, ActionStatus, ActionType

    if session.exec(select(Action.id).where(Action.video_id == video_id)).first() is None:
        session.add(Action(video_id=video_id, action_type=ActionType.OTHER, status=ActionStatus.QUEUED))
        session.commit()


def recover_interrupted() -> int:
    """At startup nothing is running, so any share still marked in progress was cut off
    by a restart: mark it so the home page offers Retry (never re-run it silently: that
    would spend without being asked)."""
    from app.db import get_session
    from app.models import Action, ActionStatus, ActionType, Video

    with get_session() as session:
        rows = session.exec(select(Action).join(Video, Video.id == Action.video_id)
                            .where(Video.source == "telegram")
                            .where(Action.status.in_([ActionStatus.IN_PROGRESS, ActionStatus.QUEUED]))).all()
        n = 0
        for a in rows:
            if a.status == ActionStatus.QUEUED and a.error_message:  # deliberately parked (agent paused)
                continue
            a.status, a.error_message = ActionStatus.FAILED, "Interrupted by an app restart. Tap Retry."
            session.add(a)
            n += 1
        # Shares from before intake created cards up front may have none at all: give them one.
        with_action = select(Action.video_id)
        for v in session.exec(select(Video).where(Video.source == "telegram")
                              .where(Video.id.not_in(with_action))).all():
            session.add(Action(video_id=v.id, action_type=ActionType.OTHER, status=ActionStatus.FAILED,
                               error_message="Interrupted by an app restart. Tap Retry."))
            n += 1
        session.commit()
        return n


def shared_image_path(video) -> str | None:
    """The image to show the model, for screenshot/photo shares."""
    path = video.thumbnail_path if (video.tiktok_url or "").startswith("image:") else None
    return path if path and Path(path).is_file() else None


# ---------------- the research lane ----------------

def in_flight() -> int:
    with _in_flight_lock:
        return len(_in_flight)


def submit(video_id: int, reporter: Reporter, deep: bool = False) -> bool:
    """Queue one video; False if it's already being worked on."""
    with _in_flight_lock:
        if video_id in _in_flight:
            return False
        _in_flight.add(video_id)
    _lane.submit(_process, video_id, reporter, deep)
    return True


def _process(video_id: int, reporter: Reporter, deep: bool) -> None:
    from app.analyze import FatalAnalysisError
    from app.db import get_session
    from app.models import Video
    from app.pipeline import process_shared_video

    try:
        with get_session() as session:
            video = session.get(Video, video_id)
            try:
                action = process_shared_video(session, video, reporter.progress, chat_id=reporter.chat_id,
                                              message_id=reporter.message_id, deep=deep)
            except FatalAnalysisError as e:
                log.warning("Shared item %s stopped: %s", video_id, e)
                reporter.failed(str(e))
                return
            except Exception as e:  # noqa: BLE001
                from app.netutil import user_message
                log.exception("Shared video %s failed", video_id)
                reporter.failed(user_message(e, "processing this"))
                return
            reporter.finished(action, video)
    finally:
        with _in_flight_lock:
            _in_flight.discard(video_id)


def rerun(action_id: int, deep: bool = False) -> bool:
    """Re-run research for one action (dashboard Retry / Research deeper, or the
    notification's button), reporting through whichever doorway is set up."""
    from app.db import get_session
    from app.models import Action

    with get_session() as session:
        action = session.get(Action, action_id)
        if not action:
            return False
        video_id, chat_id = action.video_id, action.telegram_chat_id
    return submit(video_id, reporter_for(chat_id, deep), deep=deep)


def reporter_for(chat_id: int | None, deep: bool = False) -> Reporter:
    from app import ntfy_intake, telegram_bot

    if chat_id and telegram_bot.ready():
        return telegram_bot.reporter_new_message(chat_id, "🔁 Researching deeper…" if deep else "🔁 Re-running…")
    if ntfy_intake.ready():
        return ntfy_intake.NtfyReporter()
    return Reporter()
