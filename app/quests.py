"""The quest engine: what happens after the first report.

A report can lead to follow-through: a calendar slot at a food spot, an issue
in the project something fits, a check back on a story next week. Each one is
a `Task` row, so it's durable: a restart, the Mac sleeping or a month's wait
doesn't lose it, and every step is visible on the report.

    proposed --approve--> running --> done | failed      (calendar, github_issue)
             --decline--> declined
    scheduled --(run_at)--> running --> done | scheduled (retry) | failed   (follow_up)
              --cancel--> cancelled

Anything that acts on the outside world waits for your approval: the
notification's buttons, or the buttons on the report. Checking back on a story
only reads the web and costs about a cent, so it's scheduled by itself (cancel
it any time, or turn follow-ups off in Settings). It counts against the daily
cap like everything else, and waits for tomorrow when the cap is used up.

The scheduler is one thread waking every minute. It claims a due task with a
conditional UPDATE (scheduled -> running), so a task never runs twice, and at
startup anything a crash left "running" goes back to waiting.
"""
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import update
from sqlmodel import Session, select

from app import tracing
from app.models import Task

log = logging.getLogger(__name__)

FINAL = ("done", "failed", "declined", "cancelled")
SCHEDULED_KINDS = ("follow_up", "reminder", "flight_status", "fare_watch")   # run on a timer, no approval
MAX_ATTEMPTS = 3
TICK_S = 60
LATER_CHECK_DAYS = 30          # a story checked after a week gets one more look a month on

_started = False


# ---------------- planning: a finished report -> tasks ----------------

def plan(session: Session, action, report: dict, now: datetime | None = None) -> list[Task]:
    """Turn a finished report's follow-through into tasks. A re-run replaces the tasks
    an earlier run left waiting (proposals and pending check-backs), never finished ones."""
    from app import calendar_busy, projects
    from app.config import PUBLIC_BASE_URL, get_quest_settings

    cancel_open(session, action.id)
    now = now or datetime.utcnow()
    tasks: list[Task] = []
    place = report.get("place") or {}
    link = f"{PUBLIC_BASE_URL}/#a{action.id}"
    for slot in report.get("suggested_times") or []:
        start = calendar_busy.parse_local(slot.get("start", ""))
        if start is None or start < datetime.now(start.tzinfo):
            continue
        end = start + timedelta(minutes=int(slot.get("minutes") or 90))
        details = "\n".join(x for x in [report.get("headline", ""), place.get("reservation_url", ""),
                                        f"From Sidequest: {link}"] if x)
        tasks.append(Task(action_id=action.id, kind="calendar", status="proposed",
                          title=f"{start:%a %b %-d, %-I:%M %p}" + (f" · {slot['why']}" if slot.get("why") else ""),
                          payload=json.dumps({"summary": place.get("name") or report.get("headline", "")[:80],
                                              "start": start.isoformat(), "end": end.isoformat(),
                                              "location": place.get("address") or place.get("name") or "",
                                              "details": details})))
    issue = report.get("github_issue")
    if issue:
        repo = projects.github_repo_of(issue["project"])
        if repo:
            tasks.append(Task(action_id=action.id, kind="github_issue", status="proposed",
                              title=f"Open an issue in {repo}: {issue['title']}",
                              payload=json.dumps({**issue, "repo": repo, "source": link})))
        else:
            log.info("No GitHub remote for project %r; not proposing an issue", issue["project"])
    tasks += _trip_tasks(action.id, report.get("trip"), link, now)
    fare = report.get("fare_check") or {}
    if fare.get("watch_below") and fare.get("outbound_date") and get_quest_settings()["follow_ups"]:
        tasks.append(Task(action_id=action.id, kind="fare_watch", status="scheduled",
                          run_at=now + timedelta(days=FARE_WATCH_EVERY_DAYS),
                          title=f"Watch {fare['origin']}→{fare['destination']} for under ${fare['watch_below']:.0f}",
                          payload=json.dumps({**fare, "history": []})))
    follow = report.get("follow_up")
    if follow and get_quest_settings()["follow_ups"]:
        days = int(follow["after_days"])
        tasks.append(Task(action_id=action.id, kind="follow_up", status="scheduled",
                          run_at=now + timedelta(days=days),
                          title=f"Check back: {follow['watch_for']}"[:200],
                          payload=json.dumps({"watch_for": follow["watch_for"],
                                              "headline": report.get("headline", ""),
                                              "summary": report.get("telegram_text", ""),
                                              "reported_on": f"{now:%Y-%m-%d}",
                                              "later": [LATER_CHECK_DAYS] if days < 21 else []})))
    for t in tasks:
        session.add(t)
    session.commit()
    for t in tasks:
        session.refresh(t)
    return tasks


FARE_WATCH_EVERY_DAYS = 3
CHECK_IN_HOURS = 24
STATUS_HOURS = 3


def _trip_tasks(action_id: int, trip: dict | None, link: str, now: datetime) -> list[Task]:
    """A booked trip: one step to add every flight to your calendar (your tap), plus a check-in
    reminder 24 hours before each flight and a status check 3 hours before (automatic: they
    only read and notify)."""
    from app import calendar_busy

    if not trip:
        return []
    segments, tasks = [], []
    for seg in trip.get("segments") or []:
        dep = calendar_busy.parse_in(seg.get("depart", ""), seg.get("depart_tz"))
        if dep is None:
            continue
        arr = calendar_busy.parse_in(seg.get("arrive", ""), seg.get("arrive_tz") or seg.get("depart_tz"))
        segments.append({**seg, "_dep": dep, "_arr": arr if arr and arr > dep else dep + timedelta(hours=2)})
    if not segments:
        return []
    conf = trip.get("confirmation") or ""
    tasks.append(Task(action_id=action_id, kind="flights_calendar", status="proposed",
                      title=f"Add {len(segments)} flight{'s' if len(segments) != 1 else ''} to your calendar",
                      payload=json.dumps({"confirmation": conf, "hotel": trip.get("hotel", ""), "source": link,
                                          "segments": [{**{k: v for k, v in s.items() if not k.startswith("_")},
                                                        "start": s["_dep"].isoformat(), "end": s["_arr"].isoformat()}
                                                       for s in segments]})))
    utc_now = now
    for s in segments:
        name = f"{s.get('flight') or 'Flight'} {s['from']}→{s['to']}"
        dep_utc = s["_dep"].astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        checkin = dep_utc - timedelta(hours=CHECK_IN_HOURS)
        if checkin > utc_now:
            tasks.append(Task(action_id=action_id, kind="reminder", status="scheduled", run_at=checkin,
                              title=f"Check-in reminder: {name}",
                              payload=json.dumps({"title": f"✈️ Check in now: {name}",
                                                  "message": f"Departs {s['_dep']:%a %b %-d, %-I:%M %p} local."
                                                             + (f" Confirmation {conf}." if conf else "")
                                                             + (f" Terminal {s['terminal']}." if s.get("terminal") else "")})))
        status_at = dep_utc - timedelta(hours=STATUS_HOURS)
        if status_at > utc_now:
            tasks.append(Task(action_id=action_id, kind="flight_status", status="scheduled", run_at=status_at,
                              title=f"Status check: {name}",
                              payload=json.dumps({"flight": s.get("flight", ""), "from": s["from"], "to": s["to"],
                                                  "depart": s["_dep"].isoformat(), "name": name})))
    return tasks


def calendar_links(task: Task) -> list[tuple[str, str]]:
    """(label, Google Calendar link) for each event a calendar step covers."""
    from app import calendar_busy

    p = json.loads(task.payload or "{}")
    if task.kind == "calendar":
        start, end = calendar_busy.parse_local(p["start"]), calendar_busy.parse_local(p["end"])
        return [(task.title.split(" · ")[0], calendar_busy.google_url(p["summary"], start, end,
                                                                        p.get("location", ""), p.get("details", "")))]
    if task.kind == "flights_calendar":
        out = []
        for seg in p.get("segments") or []:
            start, end = datetime.fromisoformat(seg["start"]), datetime.fromisoformat(seg["end"])
            name = f"✈️ {seg.get('flight') or 'Flight'} {seg['from']}→{seg['to']}"
            details = "\n".join(x for x in [f"Confirmation {p['confirmation']}" if p.get("confirmation") else "",
                                            f"Terminal {seg['terminal']}" if seg.get("terminal") else "",
                                            f"Seat {seg['seat']}" if seg.get("seat") else "",
                                            f"From Sidequest: {p.get('source', '')}"] if x)
            out.append((f"{name} · {start:%a %b %-d}", calendar_busy.google_url(name, start, end, seg["from"], details)))
        return out
    return []


def calendar_ics(task: Task) -> bytes:
    from app import calendar_busy

    p = json.loads(task.payload or "{}")
    if task.kind == "flights_calendar":
        return calendar_busy.ics_many([
            {"uid": f"task-{task.id}-{i}@sidequest", "summary": f"✈️ {seg.get('flight') or 'Flight'} {seg['from']}→{seg['to']}",
             "start": datetime.fromisoformat(seg["start"]), "end": datetime.fromisoformat(seg["end"]),
             "location": seg["from"], "details": f"Confirmation {p.get('confirmation', '')}".strip()}
            for i, seg in enumerate(p.get("segments") or [])])
    return calendar_busy.ics(p["summary"], calendar_busy.parse_local(p["start"]), calendar_busy.parse_local(p["end"]),
                             p.get("location", ""), p.get("details", ""), uid=f"task-{task.id}@sidequest")


def cancel_open(session: Session, action_id: int) -> int:
    rows = session.exec(select(Task).where(Task.action_id == action_id)
                        .where(Task.status.in_(["proposed", "scheduled"]))).all()
    for t in rows:
        _set(t, "cancelled" if t.status == "scheduled" else "declined")
        session.add(t)
    session.commit()
    return len(rows)


def for_actions(session: Session, action_ids: list[int]) -> dict[int, list[Task]]:
    out: dict[int, list[Task]] = {}
    if not action_ids:
        return out
    for t in session.exec(select(Task).where(Task.action_id.in_(action_ids)).order_by(Task.id)).all():
        out.setdefault(t.action_id, []).append(t)
    return out


def upcoming(session: Session, limit: int = 20) -> list[Task]:
    return list(session.exec(select(Task).where(Task.status == "scheduled").order_by(Task.run_at).limit(limit)))


# ---------------- approvals ----------------

def _key() -> bytes:
    from app.config import get_quest_settings, save_quest_settings

    key = get_quest_settings().get("signing_key") or ""
    if not key:
        key = secrets.token_urlsafe(32)
        save_quest_settings(signing_key=key)
    return key.encode()


def signature(task_id: int, verb: str = "approve") -> str:
    """Signs one verb on one task, so a notification button can't be replayed onto another."""
    return hmac.new(_key(), f"{verb}:{int(task_id)}".encode(), "sha256").hexdigest()


def check_signature(task_id: int, sig: str, verb: str = "approve") -> bool:
    return hmac.compare_digest(str(sig or ""), signature(task_id, verb))


def approve(task_id: int, run_async: bool = True) -> tuple[str, str | None]:
    """Approve one proposed task. Returns (what happened, a URL to send you to, if any)."""
    from app import calendar_busy
    from app.db import get_session

    with get_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            return "That step no longer exists.", None
        payload = json.loads(task.payload or "{}")
        if task.kind == "calendar":
            start, end = (calendar_busy.parse_local(payload["start"]), calendar_busy.parse_local(payload["end"]))
            url = calendar_busy.google_url(payload["summary"], start, end, payload.get("location", ""),
                                           payload.get("details", ""))
            if task.status == "proposed":
                _set(task, "done", result={"google_url": url})
                session.add(task)
                for other in session.exec(select(Task).where(Task.action_id == task.action_id)
                                          .where(Task.kind == "calendar").where(Task.status == "proposed")).all():
                    _set(other, "declined")
                    session.add(other)
                session.commit()
            return "Opening Google Calendar to add it.", url
        if task.kind == "flights_calendar":
            links = calendar_links(task)
            if task.status == "proposed":
                _set(task, "done", result={"events": [{"label": label, "google_url": url} for label, url in links]})
                session.add(task)
                session.commit()
            return "Here are your flights: tap each to add it.", None
        if task.status not in ("proposed", "failed"):  # a failed issue can be approved again after a fix
            return f"Already {task.status}.", None
        _set(task, "running")
        session.add(task)
        session.commit()
    if run_async:
        threading.Thread(target=_execute, args=(task_id,), daemon=True, name=f"task-{task_id}").start()
    else:
        _execute(task_id)
    return "On it.", None


def decline(task_id: int) -> None:
    _transition(task_id, ("proposed",), "declined")


def cancel(task_id: int) -> None:
    _transition(task_id, ("scheduled",), "cancelled")


def _transition(task_id: int, allowed: tuple, status: str) -> None:
    from app.db import get_session

    with get_session() as session:
        task = session.get(Task, task_id)
        if task and task.status in allowed:
            _set(task, status)
            session.add(task)
            session.commit()


def _set(task: Task, status: str, result: dict | None = None, error: str | None = None) -> None:
    task.status = status
    if result is not None:
        task.result = json.dumps(result)
    task.error = error[:500] if error else (None if status != "failed" else task.error)
    task.updated_at = datetime.utcnow()


# ---------------- running tasks ----------------

def _execute(task_id: int) -> None:
    """Run one claimed (status 'running') task. Never raises: the outcome is on the task."""
    from app.db import get_session

    try:
        with get_session() as session:
            task = session.get(Task, task_id)
            if task is None or task.status != "running":
                return
            kind = task.kind
        {"github_issue": _open_issue, "follow_up": _follow_up, "reminder": _reminder,
         "flight_status": _flight_status, "fare_watch": _fare_watch}[kind](task_id)
    except Exception as e:  # noqa: BLE001
        from app.netutil import user_message

        log.exception("Task %s failed", task_id)
        _fail_or_retry(task_id, user_message(e, "doing this step"))


def _fail_or_retry(task_id: int, error: str) -> None:
    from app.db import get_session

    with get_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            return
        task.attempts += 1
        if task.kind in ("follow_up", "fare_watch", "flight_status") and task.attempts < MAX_ATTEMPTS:
            task.status, task.run_at = "scheduled", datetime.utcnow() + timedelta(hours=task.attempts)
            task.error = error[:500]
        else:
            _set(task, "failed", error=error)
        task.updated_at = datetime.utcnow()
        session.add(task)
        session.commit()


def _open_issue(task_id: int, http: httpx.Client | None = None) -> None:
    from app.config import GITHUB_TOKEN, get_quest_settings
    from app.db import get_session

    with get_session() as session:
        task = session.get(Task, task_id)
        p = json.loads(task.payload)
    token = get_quest_settings()["github_token"] or GITHUB_TOKEN
    if not token:
        _finish(task_id, "failed", error="No GitHub token for opening issues. Add one in Settings, then approve again.")
        return
    body = f"{p.get('body', '')}\n\n---\n_Opened by [Sidequest]({p.get('source', '')}) after you approved it._"
    client = http or httpx.Client(timeout=20)
    try:
        r = client.post(f"https://api.github.com/repos/{p['repo']}/issues",
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                 "User-Agent": "sidequest"},
                        json={"title": p["title"], "body": body})
    finally:
        if http is None:
            client.close()
    if r.status_code in (401, 403, 404):
        _finish(task_id, "failed", error=f"GitHub refused ({r.status_code}): the token needs Issues write access "
                                         f"to {p['repo']}. Fix it in Settings, then approve again.")
        return
    r.raise_for_status()
    url = r.json().get("html_url", "")
    _finish(task_id, "done", result={"url": url})
    _notify(f"✅ Opened an issue in {p['repo']}", p["title"], click=url)


def _follow_up(task_id: int, check=None) -> None:
    from app import gemini_client
    from app.config import get_agent_settings, get_gemini_key, get_quest_settings
    from app.db import get_session

    with get_session() as session:
        task = session.get(Task, task_id)
        payload, action_id = json.loads(task.payload), task.action_id
        if tracing.spend_today(session) >= get_agent_settings()["daily_cap_usd"]:
            task.status, task.run_at = "scheduled", _tomorrow_morning()  # cap used up: try tomorrow
            session.add(task)
            session.commit()
            return
    if check is None and not get_gemini_key():
        _finish(task_id, "failed", error="Follow-ups need a Gemini key (Settings).")
        return
    tier = "flex" if get_quest_settings()["flex_background"] else "standard"
    with (tracing.run("follow_up", action_id=action_id, provider="gemini"), gemini_client.tier(tier),
          gemini_client.models(watch=gemini_client.LITE_MODEL)):
        out = (check or gemini_client.follow_up)(payload)
    _finish(task_id, "done", result=out)
    if out.get("changed"):
        lines = [out.get("summary", "")] + [f"- {x.get('title') or x['url']}: {x['url']}" for x in out.get("links", [])]
        _notify(f"🔁 Update: {out.get('headline', '')}", "\n".join(lines), click=_report_url(action_id))
    later = payload.get("later") or []
    if later and out.get("keep_watching", True):
        with get_session() as session:
            session.add(Task(action_id=action_id, kind="follow_up", status="scheduled",
                             run_at=datetime.utcnow() + timedelta(days=int(later[0])),
                             title=f"Check back again: {payload['watch_for']}"[:200],
                             payload=json.dumps({**payload, "later": later[1:], "reported_on": f"{datetime.utcnow():%Y-%m-%d}",
                                                 "summary": out.get("summary") or payload.get("summary", "")})))
            session.commit()


def _reminder(task_id: int) -> None:
    """A scheduled heads-up (check in for a flight). Free: it only sends a message."""
    from app.db import get_session

    with get_session() as session:
        p = json.loads(session.get(Task, task_id).payload)
    _notify(p["title"], p.get("message", ""))
    _finish(task_id, "done")


def _over_cap() -> bool:
    from app.config import get_agent_settings
    from app.db import get_session

    with get_session() as session:
        return tracing.spend_today(session) >= get_agent_settings()["daily_cap_usd"]


def _flight_status(task_id: int, check=None) -> None:
    """Three hours before a flight: look up its status (delays, gate, cancellation) and send it."""
    from app import gemini_client
    from app.config import get_gemini_key, get_quest_settings
    from app.db import get_session

    with get_session() as session:
        task = session.get(Task, task_id)
        p, action_id = json.loads(task.payload), task.action_id
    if _over_cap() or (check is None and not get_gemini_key()):
        _finish(task_id, "failed", error="Skipped: the daily cap was used up or there's no Gemini key.")
        return
    query = {"headline": f"Flight {p['name']}", "reported_on": p["depart"][:10],
             "summary": f"Scheduled to depart {p['from']} at {p['depart']} (local) for {p['to']}.",
             "watch_for": f"today's status of flight {p.get('flight') or p['name']}: on time, delayed (by how long), "
                          f"cancelled, departure gate and terminal"}
    tier = "flex" if get_quest_settings()["flex_background"] else "standard"
    with (tracing.run("follow_up", action_id=action_id, provider="gemini"), gemini_client.tier(tier),
          gemini_client.models(watch=gemini_client.LITE_MODEL)):
        out = (check or gemini_client.follow_up)(query)
    _finish(task_id, "done", result=out)
    _notify(f"✈️ {p['name']}: {out.get('headline') or 'status'}", out.get("summary", ""))


def _fare_watch(task_id: int, search=None) -> None:
    """Every few days until shortly before the trip: has the fare dropped below the target?
    Uses live fares when a fare source is set up (one lookup), otherwise a web search."""
    from app import flights, gemini_client
    from app.config import get_gemini_key, get_quest_settings
    from app.db import get_session

    with get_session() as session:
        task = session.get(Task, task_id)
        p, action_id = json.loads(task.payload), task.action_id
    target, route = float(p["watch_below"]), f"{p['origin']}→{p['destination']}"
    if _over_cap():
        with get_session() as session:
            t = session.get(Task, task_id)
            t.status, t.run_at = "scheduled", _tomorrow_morning()
            session.add(t)
            session.commit()
        return
    lowest = None
    if search or flights.enabled():
        found = (search or flights.search)(p["origin"], p["destination"], p["outbound_date"], p.get("return_date") or None)
        lowest = found.get("lowest") if isinstance(found, dict) else None
    elif get_gemini_key():
        tier = "flex" if get_quest_settings()["flex_background"] else "standard"
        with (tracing.run("follow_up", action_id=action_id, provider="gemini"), gemini_client.tier(tier),
              gemini_client.models(watch=gemini_client.LITE_MODEL)):
            out = gemini_client.follow_up({"headline": f"Fare watch {route}", "reported_on": f"{datetime.utcnow():%Y-%m-%d}",
                                           "summary": p.get("verdict", ""),
                                           "watch_for": f"a fare under ${target:.0f} for {route} on {p['outbound_date']}"
                                                        + (f" returning {p['return_date']}" if p.get("return_date") else "")})
        lowest = target if out.get("changed") else None
    history = (p.get("history") or [])[-20:] + [{"date": f"{datetime.utcnow():%Y-%m-%d}", "lowest": lowest}]
    if lowest is not None and lowest <= target:
        _finish(task_id, "done", result={"hit": True, "lowest": lowest, "history": history})
        _notify(f"💸 Fare drop: {route} now ${lowest:.0f}", f"Under your ${target:.0f} target for {p['outbound_date']}. "
                "Fares move fast: book soon if you're going.")
        return
    try:
        depart = datetime.fromisoformat(p["outbound_date"])
    except ValueError:
        depart = datetime.utcnow()
    next_run = datetime.utcnow() + timedelta(days=FARE_WATCH_EVERY_DAYS)
    with get_session() as session:
        t = session.get(Task, task_id)
        t.payload = json.dumps({**p, "history": history})
        if next_run > depart - timedelta(days=7):   # too close to the trip to keep waiting
            _set(t, "done", result={"hit": False, "lowest": lowest, "history": history})
        else:
            t.status, t.run_at, t.attempts = "scheduled", next_run, 0
            t.updated_at = datetime.utcnow()
        session.add(t)
        session.commit()


def saved_places_in(session: Session, city: str, exclude_action_id: int | None = None) -> list[dict]:
    """Place reports you've saved in a city (for a trip there)."""
    from app.agent import load_report
    from app.models import Action, ActionType

    city = (city or "").strip().lower()
    if len(city) < 3:
        return []
    out = []
    for a in session.exec(select(Action).where(Action.action_type == ActionType.PLACE)
                          .where(Action.result.is_not(None))).all():
        r = load_report(a.result) or {}
        place = r.get("place") or {}
        if a.id != exclude_action_id and city in f"{place.get('city', '')} {place.get('address', '')}".lower():
            out.append({"action_id": a.id, "name": place.get("name") or r.get("headline", "")})
    return out[:8]


def _finish(task_id: int, status: str, result: dict | None = None, error: str | None = None) -> None:
    from app.db import get_session

    with get_session() as session:
        task = session.get(Task, task_id)
        if task:
            _set(task, status, result=result, error=error)
            session.add(task)
            session.commit()


def _tomorrow_morning() -> datetime:
    """9am local tomorrow, as naive UTC."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/New_York"))
    local = (datetime.now(tz) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _report_url(action_id: int) -> str | None:
    from app.config import PUBLIC_BASE_URL

    local = "127.0.0.1" in PUBLIC_BASE_URL or "localhost" in PUBLIC_BASE_URL
    return None if local else f"{PUBLIC_BASE_URL}/#a{action_id}"


def _notify(title: str, message: str, click: str | None = None) -> None:
    """To whichever doorway is set up: Telegram, the Shortcut's ntfy channel, or both."""
    import html

    from app import ntfy_intake, telegram_bot

    try:
        if telegram_bot.ready():
            link = f'\n\n<a href="{html.escape(click)}">Open</a>' if click else ""
            telegram_bot.notify(f"<b>{html.escape(title)}</b>\n{html.escape(message)}{link}")
        if ntfy_intake.ready():
            ntfy_intake.notify(title, message, click=click)
    except Exception:  # noqa: BLE001 - the result is on the report either way
        log.exception("Couldn't send the task notification")


# ---------------- the scheduler ----------------

def run_due(now: datetime | None = None) -> int:
    """Claim and run every task whose time has come. Returns how many ran."""
    from app.db import get_session

    now = now or datetime.utcnow()
    with get_session() as session:
        due = [t.id for t in session.exec(select(Task).where(Task.status == "scheduled")
                                          .where(Task.run_at <= now)).all()]
    ran = 0
    for task_id in due:
        with get_session() as session:
            claimed = session.exec(update(Task).where(Task.id == task_id).where(Task.status == "scheduled")
                                   .values(status="running", updated_at=datetime.utcnow())).rowcount
            session.commit()
        if claimed:
            _execute(task_id)
            ran += 1
    return ran


def recover() -> int:
    """At startup nothing is running: put back whatever a crash interrupted. Check-backs
    wait again; an approved issue goes back to 'proposed' so it's never opened twice
    without you seeing it."""
    from app.db import get_session

    with get_session() as session:
        rows = session.exec(select(Task).where(Task.status == "running")).all()
        for t in rows:
            if t.kind in SCHEDULED_KINDS:
                t.status, t.run_at = "scheduled", datetime.utcnow()
            else:
                t.status, t.error = "proposed", "Interrupted by a restart. Approve it again."
            session.add(t)
        session.commit()
        return len(rows)


def _loop() -> None:
    while True:
        try:
            run_due()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("Scheduler tick failed")
        time.sleep(TICK_S)


def start() -> None:
    global _started
    if _started or os.environ.get("SVI_DISABLE_TELEGRAM"):  # no background threads in tests
        return
    _started = True
    try:
        recover()
    except Exception:  # noqa: BLE001
        log.exception("Task recovery failed")
    threading.Thread(target=_loop, daemon=True, name="quest-scheduler").start()


# ---------------- notification buttons ----------------

def notification_actions(tasks: list[Task]) -> list[dict]:
    """ntfy buttons for a result: pick a calendar slot, or approve an issue."""
    from app import calendar_busy
    from app.config import PUBLIC_BASE_URL, get_ntfy_settings

    reachable = "127.0.0.1" not in PUBLIC_BASE_URL and "localhost" not in PUBLIC_BASE_URL
    s = get_ntfy_settings()
    out = []
    for t in tasks:
        if t.status != "proposed":
            continue
        if t.kind == "calendar" and sum(1 for a in out if a.get("_kind") == "calendar") < 2:
            p = json.loads(t.payload)
            start = calendar_busy.parse_local(p["start"])
            label = f"📅 {start:%a %-I:%M%p}".replace(":00", "")
            url = (f"{PUBLIC_BASE_URL}/tasks/{t.id}/approve?sig={signature(t.id)}" if reachable else
                   calendar_busy.google_url(p["summary"], start, calendar_busy.parse_local(p["end"]),
                                            p.get("location", ""), p.get("details", "")))
            out.append({"_kind": "calendar", "action": "view", "label": label, "url": url})
        elif t.kind == "github_issue" and s["inbox_topic"]:
            out.append({"_kind": "issue", "action": "http", "label": "Open the issue", "method": "POST",
                        "url": f"{s['server']}/{s['inbox_topic']}", "clear": True,
                        "body": json.dumps({"v": 1, "approve": t.id, "sig": signature(t.id)})})
    for a in out:
        a.pop("_kind")
    return out[:2]
