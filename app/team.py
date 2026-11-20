"""Multi-agent work for any request that splits into independent parts.

    planner (Flash-Lite) ──> 2-5 subtasks, each with a role and the tool groups it needs
         └─> specialists run in parallel, each with ONLY its groups' read-only tools
               └─> coordinator merges their briefs into one answer

"Compare these three laptops for coding under $1,200", "plan a birthday dinner
for 6 in Fishtown next Saturday", "what should my week look like?" each become a
small team built for that request. A request that doesn't split runs as one
agent instead (the planner says so), so a team is never paid for when it isn't
needed. Trips are a fixed preset of the same machinery (app/trip_planner.py).

Specialists can only read: search, maps, fares, award seats, your saved items,
your tasks and notes. The allowlist is enforced where tools run, not in the
prompt, so nothing a web page says can make a specialist act. Anything that
acts (calendar, reminders, Todoist writes) stays with the assistant and your tap.
"""
import contextvars
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app import tracing

log = logging.getLogger(__name__)

MAX_SUBTASKS = 5
SPECIALIST_STEPS = 6
BRIEF_CHARS = 2500

# Read-only tool groups a specialist can be given. "web" is Google Search + Maps (built in).
GROUPS = {
    "web": [],
    "flights": ["kiwi__search_flight", "flight_prices"],
    "points": ["seats__list_transfer_program_mileage_programs", "seats__get_flights", "seats__get_flight_details",
               "seats__search_airports"],
    "my_stuff": ["search_my_stuff", "list_todo"],
    "tasks": ["todoist_list_tasks"],
    "notes": ["notion_search_notes"],
}
READ_ONLY = {name for names in GROUPS.values() for name in names}

COMMON = """You are one specialist on a small team answering a request for one person. Do only your part, using your tools; a coordinator combines everyone's work. Be concrete: real names, prices in USD, dates, links. If something can't be found, say so briefly. Reply with a compact brief (under 250 words), no greeting."""

COORDINATOR = """You are coordinating a team's work for one person. Merge the specialists' briefs into one answer for a phone: lead with the recommendation or plan, then the key facts and links. Keep every link. Don't invent facts or prices the specialists didn't give; say what's missing. Under 1,500 characters, **bold** for key facts, no tables."""

_PLANNER = """Split the request into independent parts that specialists can research AT THE SAME TIME, or say it doesn't need a team. Reply with JSON only:
{"team": true, "subtasks": [{"role": "short name", "job": "what to find out, specifically", "tools": ["web", ...]}]}
or {"team": false}
Rules: 2-5 subtasks; each must be doable without the others' results; give each only the tool groups it needs. Tool groups: web (Google Search and Maps: anything current), flights (cash fares), points (award seats and card transfer partners), my_stuff (things the user saved and their to-dos), tasks (their Todoist), notes (their Notion). Use "team": false for simple questions, single lookups, or anything that's one step after another."""


@dataclass
class Result:
    text: str
    mode: str                       # "team" or "single"
    cost_usd: float = 0.0
    seconds: float = 0.0
    briefs: dict = field(default_factory=dict)
    subtasks: list = field(default_factory=list)


def tool_decls(groups: list[str]) -> list[dict]:
    """Function declarations for a specialist: Search/Maps if it has 'web', plus its groups' tools
    that are actually available right now."""
    from app import agent, assistant, flights, mcp_client

    pool = {t["name"]: t for t in assistant._TOOLS + assistant._TODOIST_TOOLS + assistant._NOTION_TOOLS}
    if flights.enabled():
        t = agent._FLIGHTS_TOOL
        pool[t["name"]] = {"type": "function", "name": t["name"], "description": t["description"],
                           "parameters": t["input_schema"]}
    if {"flights", "points"} & set(groups):
        pool.update({t["name"]: t for t in mcp_client.tools()})
    wanted = [n for g in groups for n in GROUPS.get(g, [])]
    builtins = [{"type": "google_search"}, {"type": "google_maps"}] if "web" in groups else []
    return builtins + [pool[n] for n in dict.fromkeys(wanted) if n in pool]


def read_only(run_tool):
    """A tool runner that refuses anything outside the read-only groups."""
    def guarded(name: str, args: dict) -> dict:
        if name not in READ_ONLY:
            return {"error": f"{name} isn't available to specialists (read-only)."}
        return run_tool(name, args)
    return guarded


def run_agent(client, system: str, text: str, tools: list[dict], run_tool, label: str,
              steps: int = SPECIALIST_STEPS) -> tuple[str, float]:
    from app import assistant, gemini_client

    params = {"model": gemini_client._model("research"), "system_instruction": system, "tools": tools,
              "generation_config": {"thinking_level": "low"}}
    started = time.monotonic()
    interaction, cost = assistant.run_loop(client, text, params, run_tool, max_steps=steps)
    out = (getattr(interaction, "output_text", "") or "").strip()
    tracing.tool_call(f"agent:{label}", {"chars": len(out)}, ok=bool(out), ms=int((time.monotonic() - started) * 1000))
    return out[:BRIEF_CHARS], cost


def plan_subtasks(client, request: str, context: str) -> tuple[list[dict], float]:
    """The planner's split, cleaned: [] means 'no team needed'."""
    from app import gemini_client

    with gemini_client.models(watch=gemini_client.LITE_MODEL):
        model = gemini_client._model("watch")
        ix = gemini_client._create(client, model=model, system_instruction=_PLANNER + "\n\n" + context,
                                   input=request, generation_config={"thinking_level": "low"})
    cost = gemini_client.interaction_cost(ix, model)
    m = re.search(r"\{.*\}", ix.output_text or "", re.S)
    try:
        data = json.loads(m.group()) if m else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not data.get("team"):
        return [], cost
    subtasks = []
    for s in data.get("subtasks") or []:
        if not isinstance(s, dict) or not str(s.get("job", "")).strip():
            continue
        groups = [g for g in s.get("tools") or [] if g in GROUPS] or ["web"]
        subtasks.append({"role": re.sub(r"[^\w -]", "", str(s.get("role") or "specialist"))[:30] or "specialist",
                         "job": str(s["job"])[:600], "tools": groups})
    return (subtasks[:MAX_SUBTASKS] if len(subtasks) >= 2 else []), cost


def run_team(request: str, subtasks: list[dict] | None = None, client=None, run_tool=None,
             coordinator: str = COORDINATOR, kind: str = "team") -> Result:
    """Plan (unless `subtasks` is given), run the specialists in parallel, merge. Falls back to
    one agent with the union of tools when the planner says a team isn't needed."""
    from app import assistant, gemini_client

    client = client or gemini_client._client()
    run_tool = read_only(run_tool or (lambda name, args: assistant._run_tool(name, args, [])))
    context = assistant._context()
    started = time.monotonic()
    with tracing.run(kind, provider="gemini") as trace:
        cost = 0.0
        if subtasks is None:
            subtasks, c = plan_subtasks(client, request, context)
            cost += c
            tracing.tool_call("agent:planner", {"subtasks": len(subtasks)}, ok=True, ms=0)
        if not subtasks:
            trace.model = f"{gemini_client._model('research')} (single)"
            text, c = run_agent(client, coordinator + "\n\nDo the whole job yourself.\n\n" + context, request,
                                tool_decls(list(GROUPS)), run_tool, "single")
            return Result(text or "I couldn't put that together.", "single", round(cost + c, 5),
                          round(time.monotonic() - started, 1))
        trace.model = f"{gemini_client._model('research')} (team of {len(subtasks)})"
        names = _unique([s["role"] for s in subtasks])
        with ThreadPoolExecutor(max_workers=len(subtasks), thread_name_prefix="specialist") as pool:
            futures = {name: pool.submit(contextvars.copy_context().run, run_agent, client,
                                         f"{COMMON}\n\nYour part: {s['job']}\n\n{context}", request,
                                         tool_decls(s["tools"]), run_tool, name)
                       for name, s in zip(names, subtasks, strict=True)}
            briefs = {}
            for name, f in futures.items():
                try:
                    briefs[name], c = f.result()
                    cost += c
                except Exception as e:  # noqa: BLE001 - one specialist failing leaves a gap, not a crash
                    log.warning("Specialist %s failed: %s", name, type(e).__name__)
                    briefs[name] = "(this specialist couldn't finish)"
        merged = "\n\n".join(f"## {name}\n{brief}" for name, brief in briefs.items())
        text, c = run_agent(client, coordinator + "\n\n" + context, f"Request: {request}\n\nBriefs:\n{merged}",
                            [], run_tool, "coordinator")
        cost += c
    return Result(text or "I couldn't put that together.", "team", round(cost, 5),
                  round(time.monotonic() - started, 1), briefs, subtasks)


def _unique(names: list[str]) -> list[str]:
    seen: dict = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} {seen[n]}")
    return out
