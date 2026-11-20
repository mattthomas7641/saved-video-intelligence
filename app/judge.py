"""Head-to-head judging for the evals: which of two answers to the same request is better?

An absolute 1-5 grade saturated (every trip plan got a 5), so it couldn't separate
setups. A pairwise judge is harder to fool: it sees both answers, picks one, and is
asked twice with the order swapped. A win counts only when both rounds agree; if the
verdict flips with the order, that's position bias and it's scored as a tie.

    python -m app.judge trips    # one agent vs the team, on the saved trip-eval plans
    python -m app.judge models   # the two newest model-comparison runs, report vs report

Both re-judge answers already saved by earlier runs, so they cost only the judge
calls (about a cent for the trip set).
"""
import argparse
import json
import re

_PROMPT = """You are judging two answers to the same request for one specific person. Pick the more useful one: correct and specific (real names, prices, dates, links), complete for what was asked, honest about uncertainty, and easy to act on from a phone. Length is not a virtue. Reply with exactly one of: A, B, TIE."""


def _ask(client, request: str, first: str, second: str) -> tuple[str, float]:
    from app import gemini_client

    model = gemini_client._model("research")   # the stronger model judges, not Flash-Lite
    ix = gemini_client._create(client, model=model, system_instruction=_PROMPT,
                               input=f"Request:\n{request}\n\nAnswer A:\n{first}\n\nAnswer B:\n{second}",
                               generation_config={"thinking_level": "low"})
    m = re.search(r"\b(A|B|TIE)\b", (ix.output_text or "").upper())
    return (m.group(1) if m else "TIE"), gemini_client.interaction_cost(ix, model)


def pairwise(request: str, x: str, y: str, client=None, ask=None) -> tuple[str, float]:
    """'x', 'y' or 'tie', and the judge's cost. Asked twice, order swapped."""
    from app import gemini_client

    ask = ask or _ask
    client = client or (gemini_client._client() if ask is _ask else None)
    first, c1 = ask(client, request, x, y)        # x is A
    second, c2 = ask(client, request, y, x)       # x is B
    x_wins = (first == "A") + (second == "B")
    y_wins = (first == "B") + (second == "A")
    verdict = "x" if x_wins == 2 else "y" if y_wins == 2 else "tie"
    return verdict, c1 + c2


def compare(pairs: list[tuple[str, str, str]], names: tuple[str, str], ask=None) -> dict:
    """pairs: (request, answer from setup 1, answer from setup 2)."""
    tally = {names[0]: 0, names[1]: 0, "tie": 0}
    cost = 0.0
    for request, a, b in pairs:
        verdict, c = pairwise(request, a, b, ask=ask)
        cost += c
        tally[names[0] if verdict == "x" else names[1] if verdict == "y" else "tie"] += 1
    return {"pairs": len(pairs), "wins": tally, "judge_cost": round(cost, 4)}


def trip_pairs() -> list[tuple[str, str, str]]:
    from app import trip_eval

    run = trip_eval.latest() or {}
    requests = {c["id"]: c["request"] for c in trip_eval.cases()}
    by = {}
    for r in run.get("results", []):
        by.setdefault(r["id"], {})[r["mode"]] = r["text"]
    return [(requests.get(i, i), m["single"], m["parallel"]) for i, m in by.items() if {"single", "parallel"} <= set(m)]


def model_pairs() -> tuple[list[tuple[str, str, str]], tuple[str, str]]:
    from app import evals

    files = sorted(evals.RUNS_DIR.glob("*.json"))[-2:]
    if len(files) < 2:
        return [], ("", "")
    old, new = (json.loads(f.read_text()) for f in files)
    cases = {c["id"]: c for c in evals.load_cases()}
    newer = {r["id"]: r for r in new.get("results", []) if r.get("report")}

    def text(r):
        rep = r["report"]
        return f"{rep.get('headline', '')}\n{rep.get('telegram_text', '')}\n\n{rep.get('report_markdown', '')}"[:6000]

    pairs = []
    for r in old.get("results", []):
        if r.get("report") and r["id"] in newer:
            inputs = cases.get(r["id"], {}).get("inputs", {})
            request = (f"Research this item the user shared and say what's worth doing about it:\n"
                       f"{inputs.get('url') or ''}\n{(inputs.get('caption') or '')[:500]}\n"
                       f"{(inputs.get('transcript') or '')[:1500]}")
            pairs.append((request, text(r), text(newer[r["id"]])))
    return pairs, (old.get("config") or "older run", new.get("config") or "newer run")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.judge")
    parser.add_argument("which", choices=["trips", "models"])
    args = parser.parse_args(argv)
    from app.db import init_db
    init_db()
    if args.which == "trips":
        out = compare(trip_pairs(), ("one agent", "team"))
    else:
        pairs, names = model_pairs()
        out = compare(pairs, names)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
