"""The web app's pages: Tracked (home), Log, a page per quest, Ledger.

Telegram is where things go in and quick answers come back; these pages are where
follow-through lives. Every item is a quest with a catalog number (Q-042) that is
the same here, in Telegram and in its trace.

- Tracked: what's waiting on a tap (calendar slots, an issue to open, flights to
  add, something stuck), acted on in place; the waypoints coming up; what's new.
- Log: everything sent, filterable and searchable (free keyword search, no model).
- Quest: one item's full report, its next steps, what the agent did, and actions.
- Ledger: spend per day against the cap, recent runs (each a trace), eval results.
"""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app import projects, quests, tracing
from app.config import get_agent_settings
from app.db import get_db
from app.models import Action, ActionStatus, Run, Task, Video

router = APIRouter()

TYPE_LABELS = {"repo": "Repos", "research": "Research", "place": "Place", "travel": "Travel", "project": "Project",
               "skill": "Skill", "job": "Job", "other": "Other"}
VIEWS = [("open", "Open"), ("nothing", "Nothing to do"), ("done", "Done"), ("skipped", "Not for me"), ("all", "All")]
WAYPOINT_KINDS = {"follow_up": "Check back", "reminder": "Reminder", "flight_status": "Flight status",
                  "fare_watch": "Fare watch"}


def _templates():
    from app.main import templates
    return templates


# ---------------- loading ----------------

def load_items(session: Session) -> list[dict]:
    """Every item sent, newest activity first, with its report read safely and its state."""
    from app.agent import load_report

    rows = session.exec(select(Action, Video).join(Video, Video.id == Action.video_id)
                        .where(Video.source == "telegram").order_by(Action.updated_at.desc())).all()
    return [_item(a, v, load_report(a.result) if a.result and a.verdict else None) for a, v in rows]


def _item(a: Action, v: Video, report: dict | None) -> dict:
    parked = a.status == ActionStatus.QUEUED and bool(a.error_message)   # paused on purpose, or a repeat
    if a.status == ActionStatus.DONE:
        state = "done"
    elif a.status == ActionStatus.DISMISSED:
        state = "skipped"
    elif report and a.status == ActionStatus.DRAFTED:
        state = "nothing" if a.verdict == "skip" else "open"
    elif a.status in (ActionStatus.IN_PROGRESS, ActionStatus.QUEUED) and not parked:
        state = "working"
    else:
        state = "stuck"
    why = a.error_message
    if state == "stuck" and not why and a.status == ActionStatus.DRAFTED:
        why = "The saved report can't be read. Retry to research it again."
    title = (report or {}).get("headline") or v.summary or v.caption or v.tiktok_url
    return {"action": a, "video": v, "report": report, "state": state, "why": why, "title": title,
            "type": a.action_type.value}


def _proposals(session: Session) -> dict[int, list[Task]]:
    out: dict[int, list[Task]] = {}
    for t in session.exec(select(Task).where(Task.status.in_(["proposed", "failed"]))
                          .where(Task.kind.not_in(quests.SCHEDULED_KINDS)).order_by(Task.id)).all():
        out.setdefault(t.action_id, []).append(t)
    return out


def tracked(session: Session, items: list[dict]) -> list[dict]:
    """What's waiting on a tap: proposals (per quest, newest first), then stuck items. Asks
    made in chat (no quest) come along with their own task title."""
    by_id = {i["action"].id: i for i in items}
    out = []
    for action_id, tasks in sorted(_proposals(session).items(), key=lambda kv: -max(t.id for t in kv[1])):
        item = by_id.get(action_id)
        if item and item["state"] in ("done", "skipped"):
            continue
        out.append({"item": item, "tasks": tasks, "objective": _objective(tasks)})
    out += [{"item": i, "tasks": [], "objective": "Repeat?" if (i["why"] or "").startswith("Looks like a repeat")
             else "Couldn't finish"} for i in items if i["state"] == "stuck"]
    return out


def _objective(tasks: list[Task]) -> str:
    kinds = {t.kind for t in tasks}
    if "flights_calendar" in kinds:
        return "Add your flights to the calendar"
    if "calendar" in kinds:
        return "Pick a time"
    if any(t.status == "failed" for t in tasks):
        return "Couldn't open the issue"
    return "Approve the issue"


def waypoints(session: Session, limit: int = 12) -> list[dict]:
    rows = session.exec(select(Task).where(Task.status == "scheduled").order_by(Task.run_at).limit(limit)).all()
    out = []
    for t in rows:
        try:
            p = json.loads(t.payload or "{}")
        except ValueError:
            p = {}
        out.append({"task": t, "kind": WAYPOINT_KINDS.get(t.kind, t.kind),
                    "text": p.get("watch_for") or p.get("name") or t.title, "last": (p.get("history") or [{}])[-1]})
    return out


def _nav_counts(items: list[dict], waiting: int) -> dict:
    return {"tracked": waiting, "log": sum(1 for i in items if i["state"] in ("open", "nothing", "done", "skipped"))}


def _common(session: Session) -> dict:
    from app.config import get_action_settings, get_gemini_key, get_telegram_settings

    tg = get_telegram_settings()
    return {"spent_today": tracing.spend_today(session), "cap": get_agent_settings()["daily_cap_usd"],
            "paused": get_action_settings()["agent_paused"], "type_labels": TYPE_LABELS,
            "has_key": bool(get_gemini_key()), "telegram_ready": bool(tg["bot_token"] and tg["chat_id"]),
            "bot": tg.get("bot_username") or ""}


# ---------------- pages ----------------

@router.get("/")
def home(request: Request, session: Session = Depends(get_db)):
    items = load_items(session)
    track = tracked(session, items)
    ways = waypoints(session)
    recent = [i for i in items if i["state"] in ("open", "nothing")][:8]
    working = [i for i in items if i["state"] == "working"]
    return _templates().TemplateResponse(request, "home.html", {
        **_common(session), "tracked": track, "waypoints": ways, "recent": recent, "working": working,
        "activity": tracing.activity(session, [i["action"].id for i in recent]),
        "nav": "tracked", "counts": _nav_counts(items, len(track)), "total": len(items),
    })


@router.get("/agent")
def old_home():
    """Old address of the home page; links in past notifications still land somewhere useful."""
    return RedirectResponse("/", status_code=307)


@router.get("/log")
def log(request: Request, session: Session = Depends(get_db), view: str = "open", type: str = "", q: str = ""):
    items = load_items(session)
    view = view if view in dict(VIEWS) else "open"
    shown = [i for i in items if i["state"] in ("open", "nothing", "done", "skipped")]
    view_counts = {k: sum(1 for i in shown if i["state"] == k) for k, _ in VIEWS if k != "all"}
    view_counts["all"] = len(shown)
    if view != "all":
        shown = [i for i in shown if i["state"] == view]
    types = sorted({i["type"] for i in shown})
    if type:
        shown = [i for i in shown if i["type"] == type]
    words = [w for w in q.lower().split() if len(w) > 1]
    if words:
        def text(i):
            r = i["report"] or {}
            v = i["video"]
            return " ".join(str(x or "") for x in [r.get("headline"), r.get("telegram_text"), r.get("report_markdown"),
                                                   v.summary, v.caption, v.tiktok_url, v.user_note]).lower()
        shown = [i for i in shown if all(w in text(i) for w in words)]
    track = tracked(session, items)
    return _templates().TemplateResponse(request, "log.html", {
        **_common(session), "items": shown, "views": VIEWS, "view": view, "view_counts": view_counts,
        "types": types, "type": type, "q": q, "activity": tracing.activity(session, [i["action"].id for i in shown]),
        "nav": "log", "counts": _nav_counts(items, len(track)),
    })


@router.get("/q/{action_id}")
def quest(request: Request, action_id: int, session: Session = Depends(get_db)):
    from app.agent import load_report

    a = session.get(Action, action_id)
    if a is None:
        raise HTTPException(status_code=404, detail="No such quest.")
    v = session.get(Video, a.video_id)
    item = _item(a, v, load_report(a.result) if a.result and a.verdict else None)
    items = load_items(session)
    r = item["report"] or {}
    return _templates().TemplateResponse(request, "quest.html", {
        **_common(session), "i": item, "r": r,
        "tasks": quests.for_actions(session, [a.id]).get(a.id, []),
        "activity": tracing.activity(session, [a.id]).get(a.id),
        "project_status": {p.name: p.status for p in projects.cards(session)},
        "saved_nearby": quests.saved_places_in(session, (r.get("trip") or {}).get("destination_city", ""), a.id),
        "nav": "log", "counts": _nav_counts(items, len(tracked(session, items))),
    })


@router.get("/ledger")
def ledger(request: Request, session: Session = Depends(get_db)):
    from app import evals

    items = load_items(session)
    days = tracing.spend_by_day(session, 14)
    month = tracing.spend_since(session, datetime.utcnow() - timedelta(days=30))
    runs = session.exec(select(Run).order_by(Run.started_at.desc()).limit(25)).all()
    eval_runs = evals.runs(4)
    return _templates().TemplateResponse(request, "ledger.html", {
        **_common(session), "days": days, "max_day": max([d[1] for d in days] or [0]) or 1,
        "month": month, "runs": runs, "eval_runs": eval_runs, "metric_labels": evals.METRIC_SHORT,
        "nav": "ledger", "counts": _nav_counts(items, len(tracked(session, items))),
    })
