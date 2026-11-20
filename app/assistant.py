"""The assistant: ask Sidequest anything, in plain words.

Links and screenshots still go to the research pipeline (app/pipeline.py). Any
other message goes here: one general agent with tools, not a router of
special cases. It can search the web and maps, look up live fares, search
everything you've sent, read your to-do list, set reminders, propose calendar
events, start price and fare watches, and hand anything that deserves a full
report to the research pipeline. It never spends money or acts outside
Sidequest on its own: those become proposals you tap (the same Task rows and
approval buttons as the rest of the app).

A conversation is chained on Gemini's side with previous_interaction_id, so
follow-ups ("and in April?") need no history stored here; after a quiet
spell it starts fresh. Each message is one traced run inside the daily cap.
"""
import html
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app import tracing

log = logging.getLogger(__name__)

MAX_STEPS = 8
IDLE = timedelta(minutes=45)       # a longer gap starts a new conversation
REPLY_CHARS = 3500

_sessions: dict[str, tuple[str, datetime]] = {}   # chat -> (last interaction id, when)
_lock = threading.Lock()


@dataclass
class Reply:
    text: str
    buttons: list = field(default_factory=list)   # Telegram inline keyboard rows
    cost_usd: float = 0.0


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


_S = {"type": "string"}
_TOOLS = [
    _fn("search_my_stuff", "Search everything the user has sent Sidequest (videos, links, places, repos, trips) "
        "and its reports. Use it whenever they ask about something they saved or sent before.",
        {"query": _S}, ["query"]),
    _fn("list_todo", "The user's open sidequests (researched, not done yet) and what's waiting on them.", {}, []),
    _fn("remind_me", "Send the user a Telegram reminder at a time. Free, no approval needed.",
        {"when": {"type": "string", "description": "Local time, YYYY-MM-DDTHH:MM"}, "text": _S}, ["when", "text"]),
    _fn("propose_calendar_event", "Propose a calendar event; the user taps to add it to Google Calendar. "
        "Use for plans, reservations, trips and events they want to remember.",
        {"title": _S, "start": {"type": "string", "description": "Local time, YYYY-MM-DDTHH:MM"},
         "minutes": {"type": "integer"}, "location": _S, "details": _S}, ["title", "start"]),
    _fn("watch_fare", "Watch a flight fare and message the user when it drops below a price. Checks every few "
        "days until a week before departure.",
        {"origin": _S, "destination": _S, "outbound_date": _S, "return_date": _S, "below": {"type": "number"}},
        ["origin", "destination", "outbound_date", "below"]),
    _fn("check_back_later", "Look at something again after some days (a launch, a restock, a price, a news "
        "story) and message the user only if it changed.",
        {"topic": _S, "watch_for": _S, "after_days": {"type": "integer"}}, ["topic", "watch_for", "after_days"]),
    _fn("update_profile", "Remember a fact about the user for every future answer: a points or miles balance, a "
        "credit card they hold, a loyalty number, home airport, a preference (e.g. 'aisle seat', 'no red-eyes').",
        {"key": {"type": "string", "description": "Short label, e.g. 'Chase Ultimate Rewards points', 'cards'"},
         "value": {"type": "string", "description": "The value; empty to forget it"}}, ["key", "value"]),
    _fn("plan_trip", "Plan a whole trip: flights (and points if they have a balance), where to stay and what to "
        "do, in one plan with links and a total. Use for 'plan a trip / weekend / vacation' requests; pass "
        "everything known (where, when, budget, who, flexibility). About 3 cents, under a minute.",
        {"request": {"type": "string"}}, ["request"]),
    _fn("team_up", "Put a team of specialist agents on a request with several independent parts, working in "
        "parallel, then merge their findings: comparing products or options, planning an event or a week, "
        "researching a decision from several angles. Pass the whole request with everything known. Don't use it "
        "for simple questions or single lookups. About 2-6 cents.",
        {"request": {"type": "string"}}, ["request"]),
    _fn("research_in_depth", "Hand something to the full research pipeline (a link, a product, a place, a repo): "
        "it gets a complete report on the home page and in Telegram in a few minutes. Costs a few cents.",
        {"link_or_text": _S, "note": _S}, ["link_or_text"]),
]

_PROMPT = """You are Sidequest, one person's personal assistant, reached over Telegram. They can ask you anything: plan, find, compare, look up, remember, remind, watch prices, organise. Be the assistant a sharp, well-connected friend would be: specific, practical, honest about uncertainty, and brief.

How to work:
- Use your tools instead of guessing: Google Search and Maps for anything current (hours, prices, news, availability), flight_prices for fares, search_my_stuff for anything they saved or sent before, list_todo for their open items.
- Do the work, then answer. For a plan, give the plan; for a choice, recommend one and say why; for flights, give the best 2-3 options with prices and a link to finish on Google Flights or the airline.
- Never spend money, book, buy, send messages or change anything outside Sidequest yourself. Propose it: calendar events with propose_calendar_event (they tap to add), fare and price watches, reminders. To book a flight, give them the exact option and the link to book it.
- Money, points and credit cards: give options and the math (cents per point, cash price vs points), not financial advice. Never ask for passwords or card numbers.
- If something deserves a full write-up (a link they paste, a product, a repo), use research_in_depth.
- Flights: search cash fares with flight_prices (each option has a booking link); compare a few dates if they're flexible; show the best 2-3 with price, stops, times and the booking link. Booking itself happens on the link: say so plainly.
- Points and miles: use the balances in "Profile". Find which programs a card transfers to with seats__list_transfer_program_mileage_programs (Chase Ultimate Rewards is "chaseus") and award space with seats__get_flights (cached, up to ~60 days out). Compare against the cash fare and give cents per point = (cash price − award taxes) ÷ points; above ~1.5¢ is usually good for Chase points, and the Chase Travel portal is the fallback to compare. Warn that transfers are one-way and instant: confirm the award seat on the airline's site before transferring. If you don't know their balance, ask and save it with update_profile.
- Reply for a phone: under 1,200 characters unless they asked for detail, short lines, **bold** for key facts, no tables, no headers."""


def _context() -> str:
    from app import calendar_busy
    from app.config import get_agent_settings, get_profile, get_quest_settings

    now = datetime.now(calendar_busy.tz())
    about = get_agent_settings()["about_me"].strip() or "(not filled in)"
    home = get_quest_settings()["home_airport"].strip().upper()
    profile = get_profile()
    return (f"Now: {now:%A %Y-%m-%d %H:%M} ({calendar_busy.tz().key})\nAbout the user: {about}"
            + (f"\nHome airport: {home}" if home else "")
            + ("\nProfile: " + "; ".join(f"{k}: {v}" for k, v in profile.items()) if profile else ""))


def _tools() -> list[dict]:
    from app import agent, flights, mcp_client

    builtins = [{"type": "google_search"}, {"type": "google_maps"}]
    extra = [agent._FLIGHTS_TOOL] if flights.enabled() else []
    return (builtins + _TOOLS + [{"type": "function", "name": t["name"], "description": t["description"],
                                  "parameters": t["input_schema"]} for t in extra] + mcp_client.tools())


def ask(chat: str, text: str, client=None, run_tool=None) -> Reply:
    """One message in, one reply out (with buttons for anything it proposed)."""
    from app import gemini_client
    from app.config import get_agent_settings
    from app.db import get_session

    with get_session() as session:
        if tracing.spend_today(session) >= get_agent_settings()["daily_cap_usd"]:
            return Reply("Today's spending cap is used up, so I can't look things up until tomorrow. "
                         "You can raise it in Settings.")
    client = client or gemini_client._client()
    run_tool = run_tool or _run_tool
    model = gemini_client._model("research")
    with _lock:
        prev = _sessions.get(chat)
    params = {"model": model, "system_instruction": _PROMPT + "\n\n" + _context(), "tools": _tools(),
              "generation_config": {"thinking_level": "low"}}
    if prev and datetime.utcnow() - prev[1] < IDLE:
        params["previous_interaction_id"] = prev[0]
    buttons: list = []
    with tracing.run("assistant", provider="gemini"):
        interaction, cost = run_loop(client, text, params, lambda name, args: run_tool(name, args, buttons))
    with _lock:
        _sessions[chat] = (interaction.id, datetime.utcnow())
    answer = (getattr(interaction, "output_text", "") or "").strip() or "Done."
    return Reply(answer[:REPLY_CHARS], buttons, round(cost, 5))


def run_loop(client, text, params: dict, run_tool, max_steps: int = MAX_STEPS) -> tuple:
    """One agent's tool loop on the Interactions API: call, run any function calls, send
    the results back (chained by previous_interaction_id), until it answers in text.
    Returns (final interaction, cost). Shared by the assistant and the trip planner's agents."""
    from app import gemini_client

    model = params["model"]
    interaction = gemini_client._create(client, input=text, **params)
    cost = gemini_client.interaction_cost(interaction, model)
    for _ in range(max_steps):
        calls = gemini_client._steps(interaction, "function_call")
        if not calls:
            break
        results = []
        for c in calls:
            started = time.monotonic()
            out = run_tool(c.name, dict(c.arguments or {}))
            tracing.tool_call(c.name, dict(c.arguments or {}), ok="error" not in out,
                              ms=int((time.monotonic() - started) * 1000))
            results.append({"type": "function_result", "name": c.name, "call_id": c.id,
                            "result": [{"type": "text", "text": json.dumps(out)[:12000]}]})
        interaction = gemini_client._create(client, input=results, previous_interaction_id=interaction.id,
                                            **{k: v for k, v in params.items() if k != "previous_interaction_id"})
        cost += gemini_client.interaction_cost(interaction, model)
    return interaction, cost


def reset(chat: str) -> None:
    with _lock:
        _sessions.pop(chat, None)


# ---------------- tools ----------------

def _local_to_utc(value: str) -> datetime | None:
    from zoneinfo import ZoneInfo

    from app import calendar_busy

    d = calendar_busy.parse_local(value)
    return d.astimezone(ZoneInfo("UTC")).replace(tzinfo=None) if d else None


def _run_tool(name: str, args: dict, buttons: list) -> dict:
    """Run one tool. Returns a small JSON-able result; problems come back as {"error": ...}."""
    from app import flights, quests
    from app.db import get_session
    from app.models import Task

    try:
        if name == "search_my_stuff":
            from app.mcp_server import search_sidequests
            return {"results": search_sidequests(str(args.get("query", "")), 6)}
        if name == "list_todo":
            from app.mcp_server import list_sidequests, upcoming
            return {"todo": list_sidequests("todo", 15), "upcoming": upcoming(10)}
        if name == "flight_prices":
            return flights.search(str(args.get("origin", "")), str(args.get("destination", "")),
                                  str(args.get("outbound_date", "")), args.get("return_date") or None)
        if name == "remind_me":
            when = _local_to_utc(str(args.get("when", "")))
            if when is None or when < datetime.utcnow():
                return {"error": "That time is in the past or not YYYY-MM-DDTHH:MM local."}
            text = str(args.get("text", ""))[:300]
            with get_session() as session:
                session.add(Task(action_id=0, kind="reminder", status="scheduled", run_at=when,
                                 title=f"Reminder: {text}"[:200],
                                 payload=json.dumps({"title": f"⏰ {text}", "message": ""})))
                session.commit()
            return {"ok": True, "scheduled_for": args.get("when")}
        if name == "propose_calendar_event":
            from app import calendar_busy
            start = calendar_busy.parse_local(str(args.get("start", "")))
            if start is None:
                return {"error": "start must be YYYY-MM-DDTHH:MM local."}
            minutes = int(args.get("minutes") or 60)
            with get_session() as session:
                task = Task(action_id=0, kind="calendar", status="proposed",
                            title=f"{start:%a %b %-d, %-I:%M %p} · {str(args.get('title', ''))[:80]}",
                            payload=json.dumps({"summary": str(args.get("title", ""))[:200], "start": start.isoformat(),
                                                "end": (start + timedelta(minutes=minutes)).isoformat(),
                                                "location": str(args.get("location", ""))[:300],
                                                "details": str(args.get("details", ""))[:1500]}))
                session.add(task)
                session.commit()
                session.refresh(task)
            buttons.append([{"text": f"📅 Add: {task.title}"[:60], "callback_data": f"approve:{task.id}"}])
            return {"ok": True, "note": "A button to add it was attached to your reply."}
        if name == "watch_fare":
            fare = {"origin": str(args.get("origin", "")).upper()[:3],
                    "destination": str(args.get("destination", "")).upper()[:3],
                    "outbound_date": str(args.get("outbound_date", ""))[:10],
                    "return_date": str(args.get("return_date") or "")[:10], "verdict": "",
                    "watch_below": float(args.get("below") or 0), "history": []}
            if not fare["watch_below"]:
                return {"error": "Give a target price."}
            with get_session() as session:
                session.add(Task(action_id=0, kind="fare_watch", status="scheduled",
                                 run_at=datetime.utcnow() + timedelta(days=quests.FARE_WATCH_EVERY_DAYS),
                                 title=f"Watch {fare['origin']}→{fare['destination']} for under ${fare['watch_below']:.0f}",
                                 payload=json.dumps(fare)))
                session.commit()
            return {"ok": True, "checks_every_days": quests.FARE_WATCH_EVERY_DAYS,
                    "live_fares": flights.enabled()}
        if name == "check_back_later":
            days = min(max(int(args.get("after_days") or 7), 1), 90)
            with get_session() as session:
                session.add(Task(action_id=0, kind="follow_up", status="scheduled",
                                 run_at=datetime.utcnow() + timedelta(days=days),
                                 title=f"Check back: {args.get('watch_for', '')}"[:200],
                                 payload=json.dumps({"watch_for": str(args.get("watch_for", ""))[:300],
                                                     "headline": str(args.get("topic", ""))[:200], "summary": "",
                                                     "reported_on": f"{datetime.utcnow():%Y-%m-%d}", "later": []})))
                session.commit()
            return {"ok": True, "in_days": days}
        if name == "update_profile":
            from app.config import save_profile_fact
            save_profile_fact(str(args.get("key", "")), str(args.get("value", "")))
            return {"ok": True}
        from app import mcp_client
        if mcp_client.handles(name):
            return mcp_client.call(name, args)
        if name == "team_up":
            from app import team
            result = team.run_team(str(args.get("request", "")))
            return {"answer": result.text, "team": [s["role"] for s in result.subtasks] or ["single agent"],
                    "note": "Send this answer to the user as it is (light edits only)."}
        if name == "plan_trip":
            from app import trip_planner
            # The team: a pairwise judge preferred its plans 3-1 (1 tie) over one agent's, worth
            # the extra ~1.5 cents and ~20 s for a trip (README, "Measured").
            result = trip_planner.plan(str(args.get("request", "")), mode="parallel")
            return {"plan": result.text, "note": "Send this plan to the user as it is (light edits only)."}
        if name == "research_in_depth":
            return _research(str(args.get("link_or_text", "")), str(args.get("note", "")))
        return {"error": f"Unknown tool {name}."}
    except Exception as e:  # noqa: BLE001 - a tool failing must not end the conversation
        log.exception("Assistant tool %s failed", name)
        return {"error": f"{name} failed ({type(e).__name__})."}


def _research(link_or_text: str, note: str) -> dict:
    from app import intake

    url, extra = intake.extract_link(link_or_text)
    video_id = intake.ingest_shared_link(url, note or extra) if url else intake.ingest_shared_text(link_or_text)
    queued = intake.submit(video_id, intake.reporter_for(_telegram_chat()), force=False)
    return {"queued": bool(queued), "note": "The full report will arrive as its own message."}


def _telegram_chat() -> int | None:
    from app.config import get_telegram_settings

    return get_telegram_settings()["chat_id"]


# ---------------- formatting for Telegram ----------------

def telegram_html(text: str) -> str:
    """The model's light Markdown as Telegram HTML: escaped, with **bold**, *italic* and [links](url)."""
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', out)
    out = re.sub(r"(?m)^#{1,6}\s*", "", out)
    return out
