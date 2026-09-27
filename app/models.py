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
