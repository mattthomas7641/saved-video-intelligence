"""Eval harness: is the agent getting better or worse when prompts or models change?

Cases are snapshots of real things you sent: the inputs (caption, transcript,
on-screen text, your note) plus the answer we expect - the type, whether it's
tech, the verdict, and which of your projects it should fit. A snapshot starts
from the agent's current answers; you correct them on the /evals page, which
marks a case reviewed.

A run sends every case back through the real pipeline steps (Gemini analysis
from the saved text - no re-download or re-watching - then the research loop)
under a dollar cap, scores each answer, and saves a summary next to the
previous ones, so a change shows up as "verdict accuracy 78% -> 86%, cost per
item $0.041 -> $0.029".

Cases and runs live in data/evals/ (git-ignored): they hold your own shares.
Usage:
    python -m app.evals snapshot           # add cases for reports not yet in the set
    python -m app.evals run --max-usd 2    # costs ~$0.03-0.05 per case
    python -m app.evals report             # latest run vs the one before
"""
import argparse
import json
import logging
import re
import statistics
import threading
import time
from datetime import datetime
from types import SimpleNamespace

from sqlmodel import Session, select

from app import tracing
from app.config import DATA_DIR

log = logging.getLogger(__name__)

EVAL_DIR = DATA_DIR / "evals"
CASES_PATH = EVAL_DIR / "cases.json"
RUNS_DIR = EVAL_DIR / "runs"
VERDICT_ORDER = ["skip", "maybe", "worth_it"]
CTA_RE = re.compile(r"\bcomment (?:the word|below|[\"'“]?\w+[\"'”]? (?:and|to|for))|\bDM (?:me|them|for)|link in (?:the )?bio"
                    r"|\bsales funnel|\bdm funnel", re.I)
CITATION_RE = re.compile(r"\[\d+(?:\.\d+)*(?:,\s*\d+(?:\.\d+)*)*\](?!\()")

_status = {"running": False, "done": 0, "total": 0, "spent": 0.0, "error": None, "started": None}
_lock = threading.Lock()


# ---------------- cases ----------------

def load_cases() -> list[dict]:
    try:
        return json.loads(CASES_PATH.read_text())
    except (OSError, ValueError):
        return []


def save_cases(cases: list[dict]) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CASES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cases, indent=1))
    tmp.replace(CASES_PATH)


def snapshot(session: Session) -> int:
    """Add a case for every researched share that isn't in the set yet."""
    from app.agent import is_tech, load_report
    from app.models import Action, Video

    cases = load_cases()
    have = {c["id"] for c in cases}
    added = 0
    rows = session.exec(select(Action, Video).join(Video, Video.id == Action.video_id)
                        .where(Video.source == "telegram").where(Action.verdict.is_not(None))
                        .order_by(Action.created_at)).all()
    for a, v in rows:
        case_id = f"a{a.id}"
        report = load_report(a.result)
        if case_id in have or report is None:
            continue
        cases.append({
            "id": case_id,
            "added": datetime.utcnow().isoformat(timespec="seconds"),
            "inputs": {"url": v.tiktok_url, "author": v.author, "caption": v.caption, "hashtags": v.hashtags,
                       "transcript": v.transcript, "ocr_text": v.ocr_text, "user_note": v.user_note,
                       "image_path": v.thumbnail_path if (v.tiktok_url or "").startswith("image:") else None},
            "expected": {
                "action_type": a.action_type.value,
                "is_tech": is_tech(a.action_type.value, v.category),
                "verdict": a.verdict,
                "projects": [f["project"] for f in report.get("project_fit") or [] if f.get("fit") != "skip"],
            },
            "reference_headline": report.get("headline"),
            "reviewed": False,
            "notes": "",
        })
        added += 1
    save_cases(cases)
    return added


def update_case(case_id: str, **expected) -> bool:
    cases = load_cases()
    for c in cases:
        if c["id"] == case_id:
            notes = expected.pop("notes", None)
            c["expected"].update({k: v for k, v in expected.items() if v is not None})
            if notes is not None:
                c["notes"] = notes
            c["reviewed"] = True
            save_cases(cases)
            return True
    return False


# ---------------- scoring ----------------

def score(case: dict, out: dict, known_projects: set[str]) -> dict:
    """Pure: one case's expected answer vs what the agent produced."""
    exp = case["expected"]
    report = out.get("report") or {}
    text = " ".join(str(report.get(k) or "") for k in ("headline", "telegram_text", "report_markdown"))
    fits = [f for f in report.get("project_fit") or [] if isinstance(f, dict)]
    predicted_projects = {f.get("project") for f in fits if f.get("fit") != "skip"}
    verdict = out.get("verdict")
    within_one = (verdict in VERDICT_ORDER and exp.get("verdict") in VERDICT_ORDER
                  and abs(VERDICT_ORDER.index(verdict) - VERDICT_ORDER.index(exp["verdict"])) <= 1)
    return {
        "ok": out.get("error") is None,
        "type": out.get("action_type") == exp.get("action_type"),
        "tech": out.get("is_tech") == exp.get("is_tech"),
        "verdict": verdict == exp.get("verdict"),
        "verdict_within_one": bool(within_one),
        "ignores_cta": not CTA_RE.search(text),
        "fits_valid": all(f.get("project") in known_projects and f.get("value") for f in fits),
        "projects_found": set(exp.get("projects") or []) <= predicted_projects,
        "format_ok": (len(str(report.get("headline") or "")) <= 140 and not CITATION_RE.search(text)
                      and len(str(report.get("telegram_text") or "")) <= 900),
    }


METRICS = ["ok", "type", "tech", "verdict", "verdict_within_one", "ignores_cta", "fits_valid", "projects_found",
           "format_ok"]


def summarise(results: list[dict]) -> dict:
    n = len(results) or 1
    costs = [r["cost_usd"] for r in results]
    lat = sorted(r["seconds"] for r in results)
    return {
        "cases": len(results),
        "accuracy": {m: round(sum(1 for r in results if r["scores"].get(m)) / n, 3) for m in METRICS},
        "cost_total": round(sum(costs), 4),
        "cost_per_case": round(statistics.mean(costs), 4) if costs else 0.0,
        "latency_p50": round(lat[len(lat) // 2], 1) if lat else 0.0,
        "latency_p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1) if lat else 0.0,
    }


# ---------------- running ----------------

def _video_like(case: dict) -> SimpleNamespace:
    i = case["inputs"]
    return SimpleNamespace(
        id=None, tiktok_url=i.get("url") or "", author=i.get("author"), caption=i.get("caption"),
        hashtags=i.get("hashtags"), transcript=i.get("transcript") or "", ocr_text=i.get("ocr_text") or "",
        user_note=i.get("user_note"), source="telegram", summary=None, category=None,
        local_video_path=None, thumbnail_path=i.get("image_path"),
    )


def run_case(case: dict, session: Session, budget_usd: float, provider: str = "gemini") -> dict:
    """The real pipeline steps for one case, without touching the database's items."""
    from app import agent, prefetch, projects
    from app.pipeline import research_plan

    video = _video_like(case)
    started = time.monotonic()
    out: dict = {"id": case["id"], "error": None}
    try:
        with tracing.run("eval", provider=provider) as trace:
            if provider == "gemini":
                from app.gemini_client import analyze_video
            elif provider == "xai":
                from app.xai_client import analyze_video
            else:
                from app.analyze import analyze_video
            analysis = analyze_video(video)
            video.summary, video.category = analysis.summary, analysis.category
            action_type = analysis.action_type or "other"
            tech = agent.is_tech(action_type, analysis.category)
            cards = projects.cards(session) if tech else []
            web, x_search = research_plan(provider, action_type)
            context = agent.Context(projects=agent.projects_context(cards, [c.name for c in cards]) if tech else "",
                                    prefetch=prefetch.as_context(prefetch.gather(video)))
            findings = agent.run(video, action_type, analysis.action_brief or analysis.summary, budget_usd,
                                 web=web, x_search=x_search, context=context, provider=provider, tech=tech)
            out.update(action_type=action_type, is_tech=tech, verdict=findings.verdict,
                       report=json.loads(findings.to_json()))
    except Exception as e:  # noqa: BLE001 - a failed case is a result, not a crash
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    out["cost_usd"] = round(trace.cost_usd, 5) if "trace" in locals() else 0.0
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


def run_eval(session: Session, max_usd: float = 2.0, limit: int | None = None, reviewed_only: bool = False,
             provider: str = "gemini", on_case=lambda done, total, spent: None) -> dict:
    from app.projects import project_roots

    cases = [c for c in load_cases() if c.get("reviewed") or not reviewed_only][: limit or None]
    known = set(project_roots()) | {"planned-ideas"}
    results, spent = [], 0.0
    for i, case in enumerate(cases):
        if spent >= max_usd:
            log.warning("Eval stopped at the $%.2f cap after %d of %d cases", max_usd, i, len(cases))
            break
        out = run_case(case, session, budget_usd=max_usd - spent, provider=provider)
        out["scores"] = score(case, out, known)
        spent += out["cost_usd"]
        results.append(out)
        on_case(i + 1, len(cases), spent)
    summary = {"at": datetime.utcnow().isoformat(timespec="seconds"), "provider": provider, "max_usd": max_usd,
               "reviewed_only": reviewed_only, **summarise(results), "results": results}
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / f"{summary['at'].replace(':', '')}.json").write_text(json.dumps(summary, indent=1))
    return summary


def runs(limit: int = 10) -> list[dict]:
    """Saved run summaries, newest first (without per-case detail)."""
    out = []
    for p in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        out.append({k: v for k, v in data.items() if k != "results"} | {"file": p.name})
    return out


def latest_results() -> list[dict]:
    files = sorted(RUNS_DIR.glob("*.json"), reverse=True)
    if not files:
        return []
    try:
        return json.loads(files[0].read_text()).get("results", [])
    except (OSError, ValueError):
        return []


# ---------------- background run for the /evals page ----------------

def status() -> dict:
    with _lock:
        return dict(_status)


def start_background(max_usd: float, reviewed_only: bool) -> bool:
    with _lock:
        if _status["running"]:
            return False
        _status.update(running=True, done=0, total=0, spent=0.0, error=None,
                       started=datetime.utcnow().isoformat(timespec="seconds"))

    def progress(done, total, spent):
        with _lock:
            _status.update(done=done, total=total, spent=round(spent, 4))

    def work():
        from app.db import get_session
        try:
            with get_session() as session:
                run_eval(session, max_usd=max_usd, reviewed_only=reviewed_only, on_case=progress)
        except Exception as e:  # noqa: BLE001
            log.exception("Eval run failed")
            with _lock:
                _status["error"] = str(e)[:300]
        finally:
            with _lock:
                _status["running"] = False

    threading.Thread(target=work, daemon=True, name="eval-run").start()
    return True


# ---------------- CLI ----------------

def _print_summary(s: dict, prev: dict | None) -> None:
    print(f"{s['cases']} cases · ${s['cost_total']:.2f} total · ${s['cost_per_case']:.4f}/case · "
          f"p50 {s['latency_p50']}s · p95 {s['latency_p95']}s")
    for m in METRICS:
        now = s["accuracy"][m]
        delta = ""
        if prev and m in prev.get("accuracy", {}):
            d = now - prev["accuracy"][m]
            delta = f"  ({'+' if d >= 0 else ''}{d * 100:.0f} pts)" if abs(d) >= 0.005 else "  (=)"
        print(f"  {m:20s} {now * 100:5.1f}%{delta}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.evals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot", help="add cases for researched shares not yet in the set")
    r = sub.add_parser("run", help="run the cases through the pipeline (spends money)")
    r.add_argument("--max-usd", type=float, default=2.0)
    r.add_argument("--limit", type=int)
    r.add_argument("--reviewed-only", action="store_true")
    r.add_argument("--provider", default="gemini", choices=["gemini", "xai", "anthropic"])
    sub.add_parser("report", help="latest run vs the one before")
    args = parser.parse_args(argv)

    from app.db import get_session, init_db
    init_db()
    with get_session() as session:
        if args.cmd == "snapshot":
            print(f"Added {snapshot(session)} cases; {len(load_cases())} in the set ({CASES_PATH}).")
        elif args.cmd == "run":
            prev = (runs(1) or [None])[0]
            s = run_eval(session, max_usd=args.max_usd, limit=args.limit, reviewed_only=args.reviewed_only,
                         provider=args.provider,
                         on_case=lambda d, t, sp: print(f"  case {d}/{t} · ${sp:.3f} so far", flush=True))
            _print_summary(s, prev)
        else:
            history = runs(2)
            if not history:
                print("No runs yet.")
            else:
                _print_summary(history[0], history[1] if len(history) > 1 else None)


if __name__ == "__main__":
    main()
