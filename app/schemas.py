"""Pydantic request/response models for the JSON API (/api/* and /health).

Scoped to the machine-facing JSON routes only - the HTML-form routes that
back the dashboard (e.g. POST /video/{id}/watched) intentionally keep plain
Form(...) parameters, since they're driven by <form> elements, not JSON
clients, and a Pydantic body there would be fighting the framework rather
than using it. This is the boundary FastAPI itself draws: Body/Form are
different parameter kinds for a reason.
"""
from datetime import datetime

from pydantic import BaseModel, Field


# ---------------- shared ----------------
class JobStatus(BaseModel):
    active: bool
    running: bool | None = None
    mode: str | None = None
    workers: int | None = None
    queued: int | None = None
    finished: int | None = None
    failed: int | None = None
    spent: float | None = None
    cap: float | None = None
    reason: str | None = None
    rate_per_min: float | None = None
    eta_seconds: int | None = None
    throttled_for: int | None = None
    stopping: bool | None = None


class ActionSettings(BaseModel):
    skill_enabled: bool
    project_enabled: bool
    job_enabled: bool
    agent_paused: bool


# ---------------- /health ----------------
class HealthResponse(BaseModel):
    ok: bool = True


# ---------------- /api/progress ----------------
class ProgressCounts(BaseModel):
    pending: int = 0
    downloading: int = 0
    downloaded: int = 0
    transcribing: int = 0
    transcribed: int = 0
    analyzing: int = 0
    done: int = 0
    submitted: int = 0
    error: int = 0


class ProgressResponse(BaseModel):
    total: int
    counts: ProgressCounts
    running: bool
    current_video_id: int | None = None


# ---------------- /api/bulk ----------------
class BulkCounts(BaseModel):
    total: int
    done: int
    collected: int
    submitted: int
    pending: int
    errors: int
    working: int


class BulkStatusResponse(BaseModel):
    counts: BulkCounts
    job: JobStatus


# ---------------- /api/ingest/links ----------------
class IngestLinkEntry(BaseModel):
    tiktok_url: str
    saved_date: datetime | None = None
    user_note: str | None = None


class IngestLinksResponse(BaseModel):
    seen: int
    added: int


# ---------------- /api/sync/inbox ----------------
class SyncInboxResponse(BaseModel):
    scraped: int
    added: int
    processing_started: bool
    job: JobStatus


class SyncErrorResponse(BaseModel):
    error: str


# ---------------- /api/actions/queue ----------------
class QueuedAction(BaseModel):
    action_id: int
    video_id: int
    action_type: str
    brief: str | None = None
    tiktok_url: str | None = None
    category: str | None = None
    summary: str | None = None
    author: str | None = None


class ActionsQueueResponse(BaseModel):
    queue: list[QueuedAction]
    settings: ActionSettings


# ---------------- /api/actions/{id} ----------------
class ActionUpdateRequest(BaseModel):
    status: str
    result: dict | str | None = None
    error_message: str | None = Field(default=None, max_length=500)


class ActionUpdateResponse(BaseModel):
    ok: bool = True
