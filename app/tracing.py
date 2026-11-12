"""Run traces and the cost ledger.

Every paid attempt (watching a video, a research loop, writing a project card)
is one `Run` row: its model calls, tool calls and searches in order, with
timings, tokens, cost and how it ended. That row is both:
- the **ledger**: daily spend and each item's cost are sums of runs, so failed
  attempts, re-runs and project cards all count against the daily cap, and
  nothing is overwritten;
- the **trace**: /runs/<action id> shows exactly what the agent did.

Recording uses a context variable, so the provider clients just call
`model_call()` / `tool_call()` wherever they are; outside a run those are no-ops.
"""
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, func, select

log = logging.getLogger(__name__)

MAX_STEPS = 80          # a runaway loop shouldn't make a huge row
ARG_CHARS = 160


@dataclass
class Trace:
    kind: str
    action_id: int | None = None
    video_id: int | None = None
    provider: str | None = None
    model: str | None = None
    steps: list = field(default_factory=list)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    error: str | None = None   # set by callers whose failures don't raise (see fail())
    started: float = field(default_factory=time.monotonic)

    def fail(self, message: str) -> None:
        self.error = message[:500]


_current: ContextVar[Trace | None] = ContextVar("sidequest_trace", default=None)


def current() -> Trace | None:
    return _current.get()


def model_call(model: str, cost_usd: float, input_tokens: int = 0, output_tokens: int = 0,
               searches: int = 0, ms: int | None = None) -> None:
    t = _current.get()
    if t is None:
        return
    t.model = t.model or model
    t.cost_usd += cost_usd or 0.0
    t.input_tokens += input_tokens or 0
    t.output_tokens += output_tokens or 0
    t.searches += searches or 0
    _step(t, {"t": "model", "model": model, "cost": round(cost_usd or 0.0, 5), "in": input_tokens,
              "out": output_tokens, "searches": searches, "ms": ms})


def tool_call(name: str, args: dict, ok: bool, ms: int, note: str = "") -> None:
    t = _current.get()
    if t is None:
        return
    summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in (args or {}).items())[:ARG_CHARS]
    _step(t, {"t": "tool", "name": name, "args": summary, "ok": ok, "ms": ms, "note": note[:ARG_CHARS]})


def _step(t: Trace, step: dict) -> None:
    if len(t.steps) < MAX_STEPS:
        step["at_ms"] = int((time.monotonic() - t.started) * 1000)
        t.steps.append(step)


@contextmanager
def run(kind: str, action_id: int | None = None, video_id: int | None = None, provider: str | None = None):
    """Record one attempt. The Run row is written however the block exits, so a
    failed attempt's spend is still counted (exceptions propagate unchanged)."""
    trace = Trace(kind=kind, action_id=action_id, video_id=video_id, provider=provider)
    token = _current.set(trace)
    started_at = datetime.utcnow()
    status, error = "ok", None
    try:
        yield trace
    except BaseException as e:
        status, error = "failed", f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        _current.reset(token)
        if trace.error and status == "ok":
            status, error = "failed", trace.error
        _save(trace, started_at, status, error)


def _save(trace: Trace, started_at: datetime, status: str, error: str | None) -> None:
    from app.db import get_session
    from app.models import Run

    try:
        with get_session() as session:
            session.add(Run(
                action_id=trace.action_id, video_id=trace.video_id, kind=trace.kind, provider=trace.provider,
                model=trace.model, status=status, error=error, started_at=started_at,
                duration_ms=int((time.monotonic() - trace.started) * 1000), cost_usd=round(trace.cost_usd, 6),
                input_tokens=trace.input_tokens, output_tokens=trace.output_tokens, searches=trace.searches,
                steps=json.dumps(trace.steps),
            ))
            session.commit()
    except Exception:  # noqa: BLE001 - losing a trace must never break the run it describes
        log.exception("Couldn't save the %s run trace", trace.kind)


# ---------------- the ledger ----------------

def local_midnight_utc(tz_name: str | None = None) -> datetime:
    """Start of today in the owner's timezone, as naive UTC (how times are stored)."""
    import os

    tz = ZoneInfo(tz_name or os.environ.get("APP_TIMEZONE", "America/New_York"))
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).replace(tzinfo=None)


# Deliberate spend that shouldn't eat the daily cap for things you send (still in the ledger).
UNCAPPED_KINDS = ("eval",)


def spend_since(session: Session, since: datetime, capped_only: bool = False) -> float:
    from app.models import Run

    q = select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(Run.started_at >= since)
    if capped_only:
        q = q.where(Run.kind.not_in(UNCAPPED_KINDS))
    return float(session.exec(q).one())


def spend_today(session: Session) -> float:
    """What counts against the daily cap today (everything except eval runs)."""
    return spend_since(session, local_midnight_utc(), capped_only=True)


def action_cost(session: Session, action_id: int, video_id: int | None = None) -> float:
    """Everything spent on one item: its runs, plus any run recorded against its video
    before the item had a card (e.g. watching a share from before placeholders)."""
    from sqlalchemy import and_, or_

    from app.models import Run

    match = Run.action_id == action_id
    if video_id is not None:
        match = or_(match, and_(Run.action_id.is_(None), Run.video_id == video_id))
    return float(session.exec(select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(match)).one())


def spend_by_day(session: Session, days: int = 14) -> list[tuple[str, float, int]]:
    """(local date, dollars, runs) for the last `days` days, newest first."""
    import os

    from app.models import Run

    tz = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/New_York"))
    since = local_midnight_utc() - timedelta(days=days - 1)
    totals: dict = {}
    for started_at, cost in session.exec(select(Run.started_at, Run.cost_usd).where(Run.started_at >= since)).all():
        day = started_at.replace(tzinfo=UTC).astimezone(tz).date()
        totals.setdefault(day, [0.0, 0])
        totals[day][0] += cost or 0.0
        totals[day][1] += 1
    return [(d.strftime("%b %-d"), round(v[0], 4), v[1]) for d, v in sorted(totals.items(), reverse=True)]
