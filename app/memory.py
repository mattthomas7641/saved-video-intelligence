"""Memory across shares: so advice builds on what you've sent before.

Each finished report is embedded once (Gemini `gemini-embedding-2`, ~$0.00006 an
item) and stored with its headline, verdict and project fit. Before researching
something new, the most similar past reports are recalled and handed to the
agent as "things you sent before", so it can connect them ("third agent-harness
video this month"), avoid repeating advice, and note when something new
supersedes an older recommendation. The home page links the related reports.

Small by design: a few hundred vectors live in SQLite and are compared in
Python (cosine similarity). A vector database would be overkill here.
Memory is best-effort: if embedding fails, research carries on without it.
"""
import json
import logging
import math
import time
from datetime import datetime

from sqlmodel import Session, select

from app import tracing
from app.config import get_gemini_key

log = logging.getLogger(__name__)

MODEL = "gemini-embedding-2"
DIMS = 768
PRICE_PER_M = 0.20          # $ per million input tokens
TOP_K = 3
MIN_SIMILARITY = 0.62       # below this, "related" is mostly noise
TEXT_CHARS = 1500


def enabled() -> bool:
    return bool(get_gemini_key())


def _client():
    from google import genai
    from google.genai import types

    return genai.Client(api_key=get_gemini_key(), http_options=types.HttpOptions(timeout=30_000))


def embed(texts: list[str], query: bool = False, client=None) -> list[list[float]]:
    """One vector per text. gemini-embedding-2 takes the task as a text prefix."""
    from google.genai import types

    client = client or _client()
    prefixed = [f"task: search result | query: {t}" if query else f"title: none | text: {t}" for t in texts]
    started = time.monotonic()
    resp = client.models.embed_content(model=MODEL, contents=prefixed,
                                       config=types.EmbedContentConfig(output_dimensionality=DIMS))
    tokens = sum(len(t) for t in prefixed) // 4  # the response has no token count; ~4 chars a token
    tracing.model_call(MODEL, tokens * PRICE_PER_M / 1_000_000, tokens, 0, ms=int((time.monotonic() - started) * 1000))
    return [list(e.values) for e in resp.embeddings]


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def document_text(report: dict, action_type: str, summary: str | None) -> str:
    fits = [f"{f.get('project')} ({f.get('fit')})" for f in report.get("project_fit") or [] if isinstance(f, dict)]
    parts = [f"{action_type}: {report.get('headline', '')}", summary or "", report.get("telegram_text", "")]
    if fits:
        parts.append("Fits: " + ", ".join(fits))
    return "\n".join(p for p in parts if p)[:TEXT_CHARS]


def remember(session: Session, action, video, report: dict, client=None) -> bool:
    """Embed and store one finished report (replacing any earlier memory of it)."""
    from app.models import Memory

    text = document_text(report, action.action_type.value, video.summary if video else None)
    vector = _normalise(embed([text], client=client)[0])
    row = session.exec(select(Memory).where(Memory.action_id == action.id)).first() or Memory(action_id=action.id)
    row.video_id, row.text, row.vector, row.model = action.video_id, text, json.dumps(vector), MODEL
    row.created_at = datetime.utcnow()
    session.add(row)
    session.commit()
    return True


def recall(session: Session, query_text: str, exclude_action_id: int | None = None, client=None,
           k: int = TOP_K) -> list[dict]:
    """The past reports most similar to `query_text`, best first, above MIN_SIMILARITY."""
    from app.models import Action, Memory

    rows = session.exec(select(Memory).where(Memory.action_id != (exclude_action_id or -1))).all()
    if not rows or not query_text.strip():
        return []
    q = _normalise(embed([query_text[:TEXT_CHARS]], query=True, client=client)[0])
    scored = []
    for r in rows:
        try:
            v = json.loads(r.vector)
        except (TypeError, ValueError):
            continue
        if len(v) == len(q):
            scored.append((sum(a * b for a, b in zip(q, v, strict=True)), r))
    scored.sort(key=lambda s: s[0], reverse=True)
    out = []
    for sim, r in scored[:k]:
        if sim < MIN_SIMILARITY:
            break
        action = session.get(Action, r.action_id)
        if action is None:
            continue
        try:
            report = json.loads(action.result or "{}")
        except ValueError:
            report = {}
        out.append({"action_id": action.id, "headline": report.get("headline") or r.text.split("\n", 1)[0],
                    "verdict": action.verdict, "status": action.status.value,
                    "date": (action.created_at or datetime.utcnow()).strftime("%b %-d"),
                    "similarity": round(sim, 3),
                    "fits": [f.get("project") for f in report.get("project_fit") or []
                             if isinstance(f, dict) and f.get("fit") != "skip"][:2]})
    return out


def context_block(related: list[dict]) -> str:
    if not related:
        return ""
    lines = ["Things you sent before that look related (build on these; don't repeat their advice):"]
    for r in related:
        fits = f" · fits {', '.join(r['fits'])}" if r.get("fits") else ""
        done = " · you marked it done" if r.get("status") == "done" else ""
        lines.append(f"- {r['date']}: {r['headline']} [{r.get('verdict') or 'no verdict'}{fits}{done}]")
    return "\n".join(lines)


def backfill(session: Session, client=None) -> int:
    """Embed every finished report that has no memory yet. Returns how many were added."""
    from app.models import Action, ActionStatus, Memory, Video

    have = set(session.exec(select(Memory.action_id)).all())
    todo = [a for a in session.exec(select(Action).where(Action.result.is_not(None))
                                    .where(Action.status.in_([ActionStatus.DRAFTED, ActionStatus.DONE,
                                                              ActionStatus.DISMISSED]))).all()
            if a.id not in have]
    added = 0
    with tracing.run("memory"):
        for a in todo:
            try:
                report = json.loads(a.result or "{}")
                if isinstance(report, dict) and report.get("headline"):
                    remember(session, a, session.get(Video, a.video_id), report, client=client)
                    added += 1
            except Exception:  # noqa: BLE001 - one bad report must not stop the rest
                log.exception("Couldn't build memory for action %s", a.id)
    return added
