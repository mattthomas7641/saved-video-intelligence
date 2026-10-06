"""Database models."""
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import SQLModel, Field


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
    id: Optional[int] = Field(default=None, primary_key=True)

    # From the TikTok export
    tiktok_url: str = Field(index=True, unique=True)
    saved_date: Optional[datetime] = None

    # From yt-dlp
    author: Optional[str] = None
    caption: Optional[str] = None
    hashtags: Optional[str] = None  # comma-separated
    upload_date: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    local_video_path: Optional[str] = None
    thumbnail_path: Optional[str] = None

    # From whisper / OCR
    transcript: Optional[str] = None
    ocr_text: Optional[str] = None

    # From Claude analysis
    category: Optional[str] = None
    summary: Optional[str] = None
    tags: Optional[str] = None  # comma-separated
    worth_rewatching_score: Optional[int] = None  # 1-5
    worth_rewatching_reason: Optional[str] = None
    has_promo_code: bool = False
    promo_code_text: Optional[str] = None
    has_dated_offer: bool = False
    offer_deadline_text: Optional[str] = None
    mentions_link_in_bio: bool = False
    key_facts: Optional[str] = None  # comma-separated

    # Analysis usage/cost (for spend tracking)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    batch_id: Optional[str] = None

    # Derived
    needs_verification: bool = False  # heuristic relevance flag

    # Pipeline state
    status: Status = Field(default=Status.PENDING)
    error_message: Optional[str] = None

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
    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: int = Field(foreign_key="video.id", index=True)

    action_type: ActionType
    status: ActionStatus = Field(default=ActionStatus.QUEUED)

    brief: Optional[str] = None    # JSON: what to do (project spec / skill / company+role+url)
    result: Optional[str] = None   # JSON: PR url, or {resume_path, cover_letter_path, ...}
    error_message: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
