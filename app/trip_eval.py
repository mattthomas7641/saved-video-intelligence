"""Does the multi-agent trip planner earn its keep? The same trip requests, planned by
one agent with every tool and by parallel specialists + a coordinator, scored on the
same checks, with cost and time.

    python -m app.trip_eval run --max-usd 0.6      # spends money: both modes on every case
    python -m app.trip_eval table

Checks (each plan): a real price, a booking link, somewhere to stay, things to do,
the points math when the request mentions points, and length for a phone; plus a
1-5 quality grade from a cheap judge model (Flash-Lite) reading the request and plan.
Results are saved under data/evals/trips/.
"""
import argparse
import json
import re
import statistics
from datetime import date, datetime, timedelta

from app.config import DATA_DIR

RUNS_DIR = DATA_DIR / "evals" / "trips"


def cases(today: date | None = None) -> list[dict]:
    """Requests with dates relative to today, so they stay bookable (award data covers ~60 days)."""
    t = today or date.today()

    def d(days):
        return (t + timedelta(days=days)).strftime("%B %-d")
    return [
        {"id": "miami-weekend", "points": False,
         "request": f"Long weekend in Miami from Philly, {d(30)} to {d(33)}, under $800 total for one person."},
        {"id": "chicago-food", "points": False,
         "request": f"Two nights in Chicago around {d(40)}, mostly for food. Mid budget."},
        {"id": "montreal-points", "points": True,
         "request": f"Weekend in Montreal leaving {d(45)}, back {d(48)}. I have 142,000 Chase Ultimate Rewards points: "
                    "cash or points?"},
        {"id": "nashville-cheap", "points": False,
         "request": f"Cheapest decent 3-day trip to Nashville around {d(25)}, flexible by a day or two."},
        {"id": "denver-points", "points": True,
         "request": f"Denver for 4 days from {d(50)}, I have 142k Chase points and want to use them if it's worth it."},
    ]


_PRICE = re.compile(r"\$\s?\d")
_LINK = re.compile(r"https?://")
_STAY = re.compile(r"(?i)\b(hotel|stay|airbnb|inn|hostel|neighbou?rhood|rental)\b")
_DO = re.compile(r"(?i)\b(eat|restaurant|visit|museum|tour|bar|cafe|park|show|food|day \d)\b")
_CPP = re.compile(r"(?i)(¢|cents? per point|cpp)")


def checks(case: dict, text: str) -> dict:
    return {"price": bool(_PRICE.search(text)), "link": bool(_LINK.search(text)), "stay": bool(_STAY.search(text)),
            "things_to_do": bool(_DO.search(text)), "points_math": bool(_CPP.search(text)) if case["points"] else True,
            "phone_length": len(text) <= 2500}


_JUDGE = """Grade this trip plan for the request, 1-5: 5 = specific, realistic, bookable, covers flights, stay and things to do, honest about prices; 3 = useful but generic or missing a part; 1 = wrong or unusable. Reply with only the digit."""


def judge(request: str, text: str, client=None) -> tuple[int | None, float]:
    from app import gemini_client

    client = client or gemini_client._client()
    with gemini_client.models(watch=gemini_client.LITE_MODEL):
        model = gemini_client._model("watch")
        ix = gemini_client._create(client, model=model, system_instruction=_JUDGE,
                                   input=f"Request: {request}\n\nPlan:\n{text}", generation_config={"thinking_level": "low"})
    m = re.search(r"[1-5]", ix.output_text or "")
    return (int(m.group()) if m else None), gemini_client.interaction_cost(ix, model)


def run(max_usd: float = 0.6, modes=("single", "parallel"), plan_fn=None, judge_fn=None, on_case=print) -> dict:
    from app import trip_planner

    plan_fn = plan_fn or trip_planner.plan
    judge_fn = judge_fn or judge
    results, spent = [], 0.0
    for case in cases():
        for mode in modes:
            if spent >= max_usd:
                break
            p = plan_fn(case["request"], mode=mode)
            grade, jc = judge_fn(case["request"], p.text)
            spent += p.cost_usd + jc
            results.append({"id": case["id"], "mode": mode, "cost_usd": p.cost_usd, "seconds": p.seconds,
                            "grade": grade, "checks": checks(case, p.text), "text": p.text})
            on_case(f"  {case['id']:16s} {mode:8s} ${p.cost_usd:.3f} {p.seconds:5.1f}s grade {grade}")
    summary = {"at": datetime.utcnow().isoformat(timespec="seconds"), "max_usd": max_usd,
               "modes": {m: summarise([r for r in results if r["mode"] == m]) for m in modes}, "results": results}
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / f"{summary['at'].replace(':', '')}.json").write_text(json.dumps(summary, indent=1))
    return summary


def summarise(rs: list[dict]) -> dict:
    if not rs:
        return {}
    grades = [r["grade"] for r in rs if r["grade"]]
    keys = rs[0]["checks"].keys()
    return {"cases": len(rs), "grade": round(statistics.mean(grades), 2) if grades else None,
            "checks": round(statistics.mean(sum(r["checks"][k] for k in keys) / len(keys) for r in rs), 3),
            "cost_per_plan": round(statistics.mean(r["cost_usd"] for r in rs), 4),
            "seconds_p50": round(statistics.median(r["seconds"] for r in rs), 1)}


def table(summary: dict) -> str:
    rows = ["| Setup | Plans | Judge grade (1-5) | Checks passed | $/plan | Median time |", "|---|---|---|---|---|---|"]
    names = {"single": "One agent, all tools", "parallel": "4 specialists in parallel + coordinator"}
    for mode, s in summary["modes"].items():
        if s:
            rows.append(f"| {names.get(mode, mode)} | {s['cases']} | {s['grade']} | {s['checks'] * 100:.0f}% | "
                        f"${s['cost_per_plan']:.3f} | {s['seconds_p50']} s |")
    return "\n".join(rows)


def latest() -> dict | None:
    files = sorted(RUNS_DIR.glob("*.json"), reverse=True)
    return json.loads(files[0].read_text()) if files else None


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.trip_eval")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="plan every case both ways (spends money)")
    r.add_argument("--max-usd", type=float, default=0.6)
    sub.add_parser("table", help="the latest run as a Markdown table")
    args = parser.parse_args(argv)
    from app.db import init_db
    init_db()
    if args.cmd == "run":
        print(table(run(args.max_usd)))
    else:
        s = latest()
        print(table(s) if s else "No runs yet.")


if __name__ == "__main__":
    main()
