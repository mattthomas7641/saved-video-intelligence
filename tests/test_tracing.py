"""app/tracing.py: run traces and the cost ledger behind the daily cap."""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import agent, tracing
from app.main import app
from app.models import Action, ActionStatus, ActionType, Run, Status, Video


def _action(session, **kw):
    v = Video(tiktok_url=f"https://www.tiktok.com/@a/video/{datetime.utcnow().timestamp()}", status=Status.DONE,
              source="telegram")
    session.add(v)
    session.commit()
    a = Action(video_id=v.id, action_type=ActionType.SKILL, status=ActionStatus.DRAFTED, **kw)
    session.add(a)
    session.commit()
    return a


def test_run_records_steps_tokens_and_cost(session):
    a = _action(session)
    with tracing.run("research", action_id=a.id, video_id=a.video_id, provider="gemini"):
        tracing.model_call("gemini-3.8-flash", 0.004, 1200, 300, searches=2, ms=900)
        agent.run_client_tool("read_project_file", {"project": "nope", "path": "README.md"}, {})
        tracing.model_call("gemini-3.8-flash", 0.006, 2000, 500)
    r = session.exec(select(Run)).one()
    assert r.status == "ok" and r.cost_usd == pytest.approx(0.01) and r.searches == 2
    assert r.input_tokens == 3200 and r.output_tokens == 800 and r.model == "gemini-3.8-flash"
    steps = json.loads(r.steps)
    assert [s["t"] for s in steps] == ["model", "tool", "model"]
    assert steps[1]["name"] == "read_project_file" and steps[1]["ok"] is False


def test_failed_run_still_counts_its_spend(session):
    a = _action(session)
    with pytest.raises(RuntimeError):
        with tracing.run("research", action_id=a.id):
            tracing.model_call("gemini-3.8-flash", 0.02)
            raise RuntimeError("connection dropped")
    r = session.exec(select(Run)).one()
    assert r.status == "failed" and "connection dropped" in r.error and r.cost_usd == pytest.approx(0.02)
    assert tracing.spend_today(session) == pytest.approx(0.02)


def test_fail_marks_a_run_failed_without_raising(session):
    with tracing.run("analysis") as run:
        run.fail("Analysis failed.")
    assert session.exec(select(Run)).one().status == "failed"


def test_no_run_no_recording():
    tracing.model_call("x", 1.0)  # outside a run: a no-op, not an error
    assert tracing.current() is None


def test_spend_today_uses_local_midnight(session, monkeypatch):
    midnight = tracing.local_midnight_utc("America/New_York")
    session.add(Run(kind="research", cost_usd=0.5, started_at=midnight - timedelta(minutes=5)))  # yesterday, locally
    session.add(Run(kind="research", cost_usd=0.25, started_at=midnight + timedelta(minutes=5)))
    session.commit()
    monkeypatch.setenv("APP_TIMEZONE", "America/New_York")
    assert tracing.spend_today(session) == pytest.approx(0.25)


def test_reruns_add_up_instead_of_overwriting(session):
    a = _action(session)
    for cost in (0.03, 0.04):
        with tracing.run("research", action_id=a.id):
            tracing.model_call("gemini-3.8-flash", cost)
    assert tracing.action_cost(session, a.id, a.video_id) == pytest.approx(0.07)


def test_project_cards_count_and_stop_at_the_cap(session, tmp_path, monkeypatch):
    from app import projects
    from app.config import save_agent_settings
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1" / "README.md").write_text("# P1")
    calls = []

    def card(name, raw):
        calls.append(name)
        tracing.model_call("gemini-3.8-flash", 0.002)
        return f"card {name}", 0.002
    projects.refresh(session, make_card=card, projects_dir=tmp_path)
    assert calls and session.exec(select(Run).where(Run.kind == "project_card")).first() is not None
    save_agent_settings(daily_cap_usd=0.0001)
    calls.clear()
    (tmp_path / "p1" / "README.md").write_text("# P1 changed")
    projects.refresh(session, make_card=card, projects_dir=tmp_path)
    assert calls == []  # over the cap: README fallback, no spend


def test_trace_page_and_settings_spending(session):
    a = _action(session, result=json.dumps({"headline": "Use it [1]", "telegram_text": "t", "report_markdown": "r"}),
                verdict="worth_it")
    with tracing.run("research", action_id=a.id, provider="gemini"):
        tracing.model_call("gemini-3.8-flash", 0.012, 900, 200, searches=1, ms=1500)
        tracing.tool_call("github_repo", {"repo": "a/b"}, ok=True, ms=40)
    client = TestClient(app)
    page = client.get(f"/runs/{a.id}").text
    assert "What the agent did" in page and "github_repo" in page and "repo=a/b" in page and "Use it" in page
    assert "[1]" not in page
    assert client.get("/runs/999999").status_code == 404
    assert "Spending, last 14 days" in client.get("/settings").text
