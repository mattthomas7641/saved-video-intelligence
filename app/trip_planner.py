"""Trip planning with several agents: specialists in parallel, then a coordinator.

"Long weekend in Miami in February, I have 142k Chase points" splits naturally
into independent jobs, so each gets its own agent with only the tools it needs:

    flights   cash fares and booking links          kiwi__search_flight, flight_prices, Google Search
    points    award seats and cents per point       seats.aero tools (only if a balance is on file)
    stay      where to stay and what it costs       Google Search, Google Maps
    do        what to do, incl. places you saved    Google Search, Google Maps, search_my_stuff

They run at the same time (a thread each); a coordinator then merges their
briefs into one plan with a total, the cash-vs-points call and the links.
Narrow tool sets keep each prompt small, and the slowest specialist, not the
sum of them, sets the wait.

`plan(..., mode="single")` runs the same job as one agent holding every tool:
the baseline that `python -m app.trip_eval` measures the parallel version
against (quality, cost, time).
"""
import contextvars
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app import tracing

log = logging.getLogger(__name__)

SPECIALIST_STEPS = 6
BRIEF_CHARS = 2500

_COMMON = """You are one specialist on a small team planning a trip for one person. Do only your part, using your tools; a coordinator combines everyone's work. Be concrete: real names, prices in USD, dates, links. If something can't be found, say so briefly. Reply with a compact brief (under 250 words), no greeting."""

SPECIALISTS = {
    "flights": ("Flights (cash): find the 2-3 best options for the dates (price, airline, stops, times) and the "
                "booking link for each. If dates are flexible, compare a couple of nearby dates.",
                ["kiwi__search_flight", "flight_prices"], True),
    "points": ("Points and miles: using the balances in the profile, find award options for the same trip on the "
               "programs those points transfer to (Chase Ultimate Rewards is 'chaseus'), with points + taxes, and "
               "say which program to transfer to. Note transfers are one-way: confirm the seat first.",
               ["seats__list_transfer_program_mileage_programs", "seats__get_flights", "seats__get_flight_details",
                "seats__search_airports"], False),
    "stay": ("Where to stay: the best 1-2 neighbourhoods for this person and trip, and 2-3 specific hotels or "
             "rentals with typical nightly prices for those dates.", [], True),
    "do": ("What to do: 4-6 specific things to do and places to eat that fit this person, with neighbourhoods "
           "and any booking needed. Check search_my_stuff first: include places they already saved there.",
           ["search_my_stuff"], True),
}

_COORDINATOR = """You are coordinating a trip plan for one person. Merge your specialists' briefs into one plan for a phone: the recommended flight (and the points option if there is one: cash vs points with cents per point, and which to choose), where to stay, a short day-by-day or list of things to do, and an estimated total in USD. Keep every booking link. Don't invent prices the specialists didn't give. Under 1,500 characters, **bold** for key facts, no tables."""


@dataclass
class Plan:
    text: str
    mode: str
    cost_usd: float = 0.0
    seconds: float = 0.0
    briefs: dict = field(default_factory=dict)


def _tool_decls(names: list[str], search: bool) -> list[dict]:
    from app import agent, assistant, flights, mcp_client

    pool = {t["name"]: t for t in assistant._TOOLS}
    if flights.enabled():
        t = agent._FLIGHTS_TOOL
        pool[t["name"]] = {"type": "function", "name": t["name"], "description": t["description"],
                           "parameters": t["input_schema"]}
    pool.update({t["name"]: t for t in mcp_client.tools()})
    builtins = [{"type": "google_search"}, {"type": "google_maps"}] if search else []
    return builtins + [pool[n] for n in names if n in pool]


def _has_points() -> bool:
    from app.config import get_profile

    return any("point" in k.lower() or "miles" in k.lower() for k in get_profile())


def _run_agent(client, system: str, text: str, tools: list[dict], run_tool, label: str) -> tuple[str, float]:
    from app import assistant, gemini_client

    params = {"model": gemini_client._model("research"), "system_instruction": system, "tools": tools,
              "generation_config": {"thinking_level": "low"}}
    started = time.monotonic()
    interaction, cost = assistant.run_loop(client, text, params, run_tool, max_steps=SPECIALIST_STEPS)
    out = (getattr(interaction, "output_text", "") or "").strip()
    tracing.tool_call(f"agent:{label}", {"chars": len(out)}, ok=bool(out), ms=int((time.monotonic() - started) * 1000))
    return out[:BRIEF_CHARS], cost


def plan(request: str, mode: str = "parallel", client=None, run_tool=None) -> Plan:
    """A full trip plan for `request`. mode 'parallel' (specialists + coordinator) or 'single'."""
    from app import assistant, gemini_client

    client = client or gemini_client._client()
    run_tool = run_tool or (lambda name, args: assistant._run_tool(name, args, []))
    context = assistant._context()
    started = time.monotonic()
    with tracing.run("trip_plan", provider="gemini") as trace:
        trace.model = f"{gemini_client._model('research')} ({mode})"
        if mode == "single":
            jobs = "\n".join(f"- {name}: {job}" for name, (job, _, _) in SPECIALISTS.items()
                             if name != "points" or _has_points())
            names = sorted({n for name, (_, ns, _) in SPECIALISTS.items() for n in ns})
            text, cost = _run_agent(client, _COORDINATOR + "\n\nCover all of these yourself:\n" + jobs + "\n\n" + context,
                                    request, _tool_decls(names, True), run_tool, "single")
            return Plan(text or "I couldn't put a plan together.", mode, round(cost, 5),
                        round(time.monotonic() - started, 1))
        team = {k: v for k, v in SPECIALISTS.items() if k != "points" or _has_points()}
        with ThreadPoolExecutor(max_workers=len(team), thread_name_prefix="trip-agent") as pool:
            futures = {name: pool.submit(contextvars.copy_context().run, _run_agent, client,
                                         f"{_COMMON}\n\nYour part: {job}\n\n{context}", request,
                                         _tool_decls(tools, search), run_tool, name)
                       for name, (job, tools, search) in team.items()}
            briefs, cost = {}, 0.0
            for name, f in futures.items():
                try:
                    briefs[name], c = f.result()
                    cost += c
                except Exception as e:  # noqa: BLE001 - one specialist failing leaves a gap, not a crash
                    log.warning("Trip specialist %s failed: %s", name, type(e).__name__)
                    briefs[name] = "(this specialist couldn't finish)"
        merged = "\n\n".join(f"## {name}\n{brief}" for name, brief in briefs.items())
        text, c = _run_agent(client, _COORDINATOR + "\n\n" + context, f"Request: {request}\n\nBriefs:\n{merged}",
                             [], run_tool, "coordinator")
        cost += c
    return Plan(text or "I couldn't put a plan together.", mode, round(cost, 5),
                round(time.monotonic() - started, 1), briefs)
