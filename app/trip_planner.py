"""Trip planning: a fixed preset of the multi-agent team (app/team.py).

"Long weekend in Miami in February, I have 142k Chase points" always splits the
same way, so the team is fixed instead of planned:

    flights   cash fares and booking links          tools: flights, web
    points    award seats and cents per point       tools: points (only if a balance is on file)
    stay      where to stay and what it costs       tools: web
    do        what to do, incl. places you saved    tools: web, my_stuff

`plan(..., mode="single")` does the same job as one agent with every tool: the
baseline `python -m app.trip_eval` measures the team against.
"""
from dataclasses import dataclass, field

from app import team

SPECIALISTS = {
    "flights": ("Flights (cash): find the 2-3 best options for the dates (price, airline, stops, times) and the "
                "booking link for each. If dates are flexible, compare a couple of nearby dates.", ["flights", "web"]),
    "points": ("Points and miles: using the balances in the profile, find award options for the same trip on the "
               "programs those points transfer to (Chase Ultimate Rewards is 'chaseus'), with points + taxes, and "
               "say which program to transfer to. Note transfers are one-way: confirm the seat first.", ["points"]),
    "stay": ("Where to stay: the best 1-2 neighbourhoods for this person and trip, and 2-3 specific hotels or "
             "rentals with typical nightly prices for those dates.", ["web"]),
    "do": ("What to do: 4-6 specific things to do and places to eat that fit this person, with neighbourhoods "
           "and any booking needed. Check search_my_stuff first: include places they already saved there.",
           ["web", "my_stuff"]),
}

COORDINATOR = """You are coordinating a trip plan for one person. Merge your specialists' briefs into one plan for a phone: the recommended flight (and the points option if there is one: cash vs points with cents per point, and which to choose), where to stay, a short day-by-day or list of things to do, and an estimated total in USD. Keep every booking link. Don't invent prices the specialists didn't give. Under 1,500 characters, **bold** for key facts, no tables."""


@dataclass
class Plan:
    text: str
    mode: str
    cost_usd: float = 0.0
    seconds: float = 0.0
    briefs: dict = field(default_factory=dict)


def _has_points() -> bool:
    from app.config import get_profile

    return any("point" in k.lower() or "miles" in k.lower() for k in get_profile())


def subtasks() -> list[dict]:
    return [{"role": name, "job": job, "tools": tools} for name, (job, tools) in SPECIALISTS.items()
            if name != "points" or _has_points()]


def plan(request: str, mode: str = "parallel", client=None, run_tool=None) -> Plan:
    """A full trip plan. mode 'parallel' (the specialists + coordinator) or 'single' (one agent)."""
    r = team.run_team(request, subtasks=[] if mode == "single" else subtasks(), client=client, run_tool=run_tool,
                      coordinator=COORDINATOR, kind="trip_plan")
    return Plan(r.text, "single" if r.mode == "single" else "parallel", r.cost_usd, r.seconds, r.briefs)
