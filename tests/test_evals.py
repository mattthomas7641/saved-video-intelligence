"""app/evals.py: snapshots, scoring, and runs - with the pipeline's model calls faked."""
import json

import pytest
from fastapi.testclient import TestClient

from app import agent, evals, prefetch, tracing
from app.analyze import AnalysisResult
from app.main import app
from app.models import Action, ActionStatus, ActionType, Status, Video


def _shared(session, verdict="worth_it", category="Tech / AI / Coding", fit="durable-agent-harness"):
    v = Video(tiktok_url=f"https://www.tiktok.com/@a/video/{verdict}{category}", status=Status.DONE,
              source="telegram", caption="agent tips", transcript="use checkpoints", category=category)
    session.add(v)
    session.commit()
    report = {"headline": "Use checkpoints", "telegram_text": "t", "report_markdown": "r",
              "project_fit": [{"project": fit, "fit": "worth_it", "value": "fun"}] if fit else []}
    a = Action(video_id=v.id, action_type=ActionType.SKILL, status=ActionStatus.DRAFTED, verdict=verdict,
               result=json.dumps(report))
    session.add(a)
    session.commit()
    return a


def test_snapshot_builds_cases_from_researched_shares_once(session):
    _shared(session)
    _shared(session, verdict="skip", category="Recipe / Cooking", fit=None)
    assert evals.snapshot(session) == 2
    assert evals.snapshot(session) == 0
    cases = evals.load_cases()
    tech = next(c for c in cases if c["expected"]["verdict"] == "worth_it")
    assert tech["expected"] == {"action_type": "skill", "is_tech": True, "verdict": "worth_it",
                                "projects": ["durable-agent-harness"]}
    assert tech["inputs"]["transcript"] == "use checkpoints" and tech["reviewed"] is False


def test_update_case_marks_reviewed(session):
    _shared(session)
    evals.snapshot(session)
    cid = evals.load_cases()[0]["id"]
    assert evals.update_case(cid, verdict="maybe", projects=[], notes="borderline")
    c = evals.load_cases()[0]
    assert c["reviewed"] and c["expected"]["verdict"] == "maybe" and c["notes"] == "borderline"


CASE = {"id": "a1", "expected": {"action_type": "skill", "is_tech": True, "verdict": "worth_it",
                                 "projects": ["gateway"]}}


def _out(**over):
    report = {"headline": "Adopt it", "telegram_text": "short", "report_markdown": "body",
              "project_fit": [{"project": "gateway", "fit": "worth_it", "value": "personal_and_resume"}]}
    report.update(over.pop("report", {}))
    return {"error": None, "action_type": "skill", "is_tech": True, "verdict": "worth_it", "report": report, **over}


def test_score_all_pass():
    s = evals.score(CASE, _out(), {"gateway"})
    assert all(s.values()), s


@pytest.mark.parametrize("out,failed", [
    (_out(verdict="maybe"), {"verdict"}),
    (_out(verdict="skip"), {"verdict", "verdict_within_one"}),
    (_out(report={"telegram_text": "Comment GUIDE and I'll DM you the repo"}), {"ignores_cta"}),
    (_out(report={"report_markdown": "Skip their link in bio funnel"}), {"ignores_cta"}),
    (_out(report={"project_fit": [{"project": "made-up", "fit": "worth_it", "value": "fun"}]}),
     {"fits_valid", "projects_found"}),
    (_out(report={"project_fit": [{"project": "gateway", "fit": "worth_it"}]}), {"fits_valid"}),
    (_out(report={"headline": "x" * 200}), {"format_ok"}),
    (_out(report={"report_markdown": "Fast [1.2] enough"}), {"format_ok"}),
    (_out(action_type="repo", is_tech=False), {"type", "tech"}),
])
def test_score_failures(out, failed):
    s = evals.score(CASE, out, {"gateway"})
    assert {k for k, v in s.items() if not v} == failed


def test_summarise():
    results = [{"scores": {m: True for m in evals.METRICS}, "cost_usd": 0.04, "seconds": 20.0},
               {"scores": {**{m: True for m in evals.METRICS}, "verdict": False}, "cost_usd": 0.02, "seconds": 40.0}]
    s = evals.summarise(results)
    assert s["accuracy"]["verdict"] == 0.5 and s["accuracy"]["type"] == 1.0
    assert s["cost_per_case"] == 0.03 and s["latency_p50"] == 40.0


def _fake_pipeline(monkeypatch, cost=0.03, verdict="worth_it"):
    from app import gemini_client
    def analyze(video):
        tracing.model_call("gemini-3.8-flash", 0.01)
        return AnalysisResult(category="Tech / AI / Coding", summary="s", tags=[], worth_rewatching_score=3,
                              worth_rewatching_reason="r", has_promo_code=False, has_dated_offer=False,
                              mentions_link_in_bio=False, key_facts=[], is_actionable=True, action_type="skill",
                              action_brief="b")
    def research(*a, **k):
        tracing.model_call("gemini-3.8-flash", cost - 0.01)
        return agent.Findings(verdict=verdict, headline="Use checkpoints", telegram_text="t", report_markdown="r",
                              project_fit=[{"project": "durable-agent-harness", "fit": "worth_it", "value": "fun"}])
    monkeypatch.setattr(gemini_client, "analyze_video", analyze)
    monkeypatch.setattr(agent, "run", research)
    monkeypatch.setattr(prefetch, "gather", lambda v: {"repos": [], "pages": []})
    monkeypatch.setattr("app.projects.project_roots", lambda *a: {"durable-agent-harness": None})


def test_run_eval_scores_costs_and_saves(session, monkeypatch):
    _fake_pipeline(monkeypatch)
    _shared(session)
    evals.snapshot(session)
    s = evals.run_eval(session, max_usd=1.0)
    assert s["cases"] == 1 and s["accuracy"]["verdict"] == 1.0 and s["accuracy"]["projects_found"] == 1.0
    assert s["cost_total"] == pytest.approx(0.03)
    assert evals.runs(1)[0]["cases"] == 1 and evals.latest_results()[0]["id"].startswith("a")
    assert tracing.spend_today(session) == 0.0  # eval spend is ledgered but outside the daily cap


def test_run_eval_stops_at_the_cap(session, monkeypatch):
    _fake_pipeline(monkeypatch, cost=0.5)
    for i in range(3):
        _shared(session, verdict=["worth_it", "maybe", "skip"][i])
    evals.snapshot(session)
    s = evals.run_eval(session, max_usd=0.6)
    assert s["cases"] == 2  # the second case crossed $0.60; the third never started


def test_a_failing_case_is_a_result(session, monkeypatch):
    _fake_pipeline(monkeypatch)
    monkeypatch.setattr(agent, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")))
    _shared(session)
    evals.snapshot(session)
    s = evals.run_eval(session, max_usd=1.0)
    assert s["accuracy"]["ok"] == 0.0 and "model down" in s["results"][0]["error"]


def test_evals_page_and_run_button(session, monkeypatch):
    _shared(session)
    client = TestClient(app)
    assert "No cases yet" in client.get("/evals").text
    client.post("/evals/snapshot")
    page = client.get("/evals").text
    assert "to review" in page and "Run evals" in page and "spends real money" in page
    started = []
    monkeypatch.setattr(evals, "start_background", lambda cap, reviewed_only: started.append(cap) or True)
    client.post("/evals/run", data={"max_usd": "500"})
    assert started == [10.0]  # hard ceiling per run
