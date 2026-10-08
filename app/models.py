"""Database models."""
from datetime import datetime
from enum import Enum

from sqlmodel import Field, SQLModel


class Status(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    ANALYZING = "analyzing"
    DONE = "done"
    SUBMITTED = "submitted"  # sent to the Batch API, waiting for results
    ERROR = "error"


class Video(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    # From the TikTok export
    tiktok_url: str = Field(index=True, unique=True)
    saved_date: datetime | None = None
    user_note: str | None = None  # your own note when sharing to the bot account; null for export-upload imports

    # From yt-dlp
    author: str | None = None
    caption: str | None = None
    hashtags: str | None = None  # comma-separated
    upload_date: datetime | None = None
    duration_seconds: float | None = None
    local_video_path: str | None = None
    thumbnail_path: str | None = None

    # From whisper / OCR
    transcript: str | None = None
    ocr_text: str | None = None

    # From Claude analysis
    category: str | None = None
    summary: str | None = None
    tags: str | None = None  # comma-separated
    worth_rewatching_score: int | None = None  # 1-5
    worth_rewatching_reason: str | None = None
    has_promo_code: bool = False
    promo_code_text: str | None = None
    has_dated_offer: bool = False
    offer_deadline_text: str | None = None
    mentions_link_in_bio: bool = False
    key_facts: str | None = None  # comma-separated

    # Analysis usage/cost (for spend tracking)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    batch_id: str | None = None

    # Derived
    needs_verification: bool = False  # heuristic relevance flag

    # Pipeline state
    status: Status = Field(default=Status.PENDING)
    error_message: str | None = None

    # User state
    watched: bool = False
    archived: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ActionType(str, Enum):
    SKILL = "skill"      # a tool/technique/snippet worth adopting
    PROJECT = "project"  # a buildable project/agent shown in the video
    JOB = "job"          # a job posting worth tracking/applying to


class ActionStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DRAFTED = "drafted"          # PR opened, or resume/cover letter written
    NEEDS_INPUT = "needs_input"  # agent got stuck or needs a decision from you
    DONE = "done"
    DISMISSED = "dismissed"
    FAILED = "failed"


class Action(SQLModel, table=True):
    """One actionable thing a saved video pointed at. Kept separate from Video
    so this fast-iterating workflow doesn't require altering the core table."""
    id: int | None = Field(default=None, primary_key=True)
    video_id: int = Field(foreign_key="video.id", index=True)

    action_type: ActionType
    status: ActionStatus = Field(default=ActionStatus.QUEUED)

    brief: str | None = None    # JSON: what to do (project spec / skill / company+role+url)
    result: str | None = None   # JSON: PR url, or {resume_path, cover_letter_path, ...}
    error_message: str | None = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
