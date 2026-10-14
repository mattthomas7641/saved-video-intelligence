"""The shared-video fast lane (pipeline.process_shared_video), the Telegram result
message, and the /agent page, with collect/analyze/research stubbed out."""
import json

import pytest
from fastapi.testclient import TestClient

from app import agent, gemini_client, pipeline, prefetch
from app import telegram_bot as tb
from app.analyze import AnalysisResult
from app.config import save_action_settings, save_agent_settings
from app.main import app
from app.models import Action, ActionStatus, ActionType, Status, Video

ANALYSIS = {"category": "Recipe / Cooking", "summary": "A cheesesteak spot in South Philly.", "tags": [],
            "worth_rewatching_score": 4, "worth_rewatching_reason": "x", "has_promo_code": False,
            "has_dated_offer": False, "mentions_link_in_bio": False, "key_facts": [],
            "is_actionable": True, "action_type": "place", "action_brief": "Dalessandro's, Philly"}


def _findings(**kw):
    return agent.Findings(verdict="worth_it", headline="Go on a weekday before 6pm",
                          telegram_text="Dalessandro's, Roxborough. Cash only.",
                          report_markdown="**Order** the whiz wit.", ideas=["Pair with a Wissahickon hike"],
                          place={"name": "Dalessandro's", "city": "Philadelphia", "hours": "11-10 daily"},
                          cost_usd=0.21, **kw)


@pytest.fixture
def transcribed(session):
    v = Video(tiktok_url="https://www.tiktok.com/@a/video/9", status=Status.TRANSCRIBED, source="telegram",
              transcript="best cheesesteak in philly", author="phillyeats")
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


@pytest.fixture(autouse=True)
def no_network_prefetch(monkeypatch):
    monkeypatch.setattr(prefetch, "gather", lambda video: {"repos": [], "pages": []})


@pytest.fixture
def stub_analysis(monkeypatch):
    """Gemini is the default analyzer for shared items; Claude must never be called."""
    def grok(video):
        result = AnalysisResult(**ANALYSIS)
        result.cost_usd = 0.003
        return result
    monkeypatch.setattr(gemini_client, "analyze_video", grok)
    monkeypatch.setattr(pipeline.analyze_mod, "analyze_video", lambda video: pytest.fail("Claude was called"))


def test_shared_video_is_analyzed_routed_and_researched(session, transcribed, stub_analysis, monkeypatch):
    seen = {}
    def fake_run(video, action_type, brief, budget, on_progress, **kw):
        seen.update(type=action_type, brief=brief, budget=budget, **kw)
        return _findings()
    monkeypatch.setattr(agent, "run", fake_run)
    progress = []
    action = pipeline.process_shared_video(session, transcribed, progress.append, chat_id=42, message_id=7)
    assert seen["type"] == "place" and seen["brief"] == "Dalessandro's, Philly"
    assert action.status == ActionStatus.DRAFTED and action.verdict == "worth_it"
    assert action.cost_usd == pytest.approx(0.213)  # research + Gemini analysis
    assert seen["web"] is True and seen["provider"] == "gemini"  # Gemini search is free: always allowed
    assert action.telegram_chat_id == 42 and json.loads(action.result)["place"]["name"] == "Dalessandro's"
    assert progress[0] == "Reading it…"  # already transcribed: nothing to watch


def test_budget_passed_to_agent_subtracts_todays_spend(session, transcribed, stub_analysis, monkeypatch):
    save_agent_settings(daily_cap_usd=2.0)
    other = Video(tiktok_url="https://www.tiktok.com/@b/video/1", status=Status.DONE)
    session.add(other)
    session.commit()
    session.add(Action(video_id=other.id, action_type=ActionType.REPO, cost_usd=0.5))
    session.commit()
    budgets = []
    monkeypatch.setattr(agent, "run", lambda v, t, b, budget, on_progress, **kw: budgets.append(budget) or _findings())
    pipeline.process_shared_video(session, transcribed)
    assert budgets == [pytest.approx(1.5 - 0.003)]  # cap - today's spend - this video's analysis


def test_cap_reached_leaves_action_needing_input(session, transcribed, stub_analysis, monkeypatch):
    def over(*a, **k):
        raise agent.CapReached("Today's research budget is used up.")
    monkeypatch.setattr(agent, "run", over)
    action = pipeline.process_shared_video(session, transcribed)
    assert action.status == ActionStatus.NEEDS_INPUT and "daily cap" in action.error_message


def test_paused_agent_analyzes_but_does_not_research(session, transcribed, stub_analysis, monkeypatch):
    save_action_settings(agent_paused=True)
    monkeypatch.setattr(agent, "run", lambda *a, **k: pytest.fail("agent should not run while paused"))
    action = pipeline.process_shared_video(session, transcribed)
    assert action.status == ActionStatus.QUEUED and "paused" in action.error_message


def test_missing_action_falls_back_to_other(session, transcribed, monkeypatch):
    monkeypatch.setattr(gemini_client, "analyze_video",
                        lambda video: AnalysisResult(**{**ANALYSIS, "is_actionable": False}))
    monkeypatch.setattr(agent, "run", lambda *a, **k: _findings())
    action = pipeline.process_shared_video(session, transcribed)
    assert action.action_type == ActionType.OTHER and action.status == ActionStatus.DRAFTED


def test_format_result_for_a_finished_report(session, transcribed, stub_analysis, monkeypatch):
    monkeypatch.setattr(agent, "run", lambda *a, **k: _findings())
    action = pipeline.process_shared_video(session, transcribed)
    text = tb.format_result(action, transcribed)
    assert "<b>Go on a weekday before 6pm</b>" in text and "Do it" in text and "Place" in text
    assert "Pair with a Wissahickon hike" in text


def test_format_result_escapes_html(session, transcribed):
    action = Action(id=1, video_id=transcribed.id, action_type=ActionType.OTHER, status=ActionStatus.FAILED,
                    error_message="<script>")
    assert "&lt;script&gt;" in tb.format_result(action, transcribed)


def test_home_renders_reports_by_topic_and_tab(session, transcribed, stub_analysis, monkeypatch):
    monkeypatch.setattr(agent, "run", lambda *a, **k: _findings())
    action = pipeline.process_shared_video(session, transcribed)
    client = TestClient(app)
    page = client.get("/").text
    assert "Go on a weekday before 6pm" in page and "Dalessandro" in page and "<strong>Order</strong>" in page
    assert "Food &amp; Home" in page  # Recipe / Cooking files under the Food & Home topic
    assert "Go on a weekday" in client.get("/?group=home").text
    assert "Go on a weekday" not in client.get("/?group=tech").text
    assert "Go on a weekday" not in client.get("/?tab=done").text
    client.post(f"/agent/{action.id}/done", data={"next": "/"})
    assert "Go on a weekday" in client.get("/?tab=done").text and "Go on a weekday" not in client.get("/").text
    client.post(f"/agent/{action.id}/dismiss", data={"next": "/"})
    assert "Go on a weekday" in client.get("/?tab=skipped").text
    client.post(f"/agent/{action.id}/reopen", data={"next": "/"})
    assert "Go on a weekday" in client.get("/").text
    assert "Go on a weekday" in client.get("/agent").text  # old address still works


def test_agent_page_markdown_cannot_inject_html(session, transcribed, stub_analysis, monkeypatch):
    evil = _findings()
    evil.report_markdown = '<img src=x onerror=alert(1)> [click](javascript:alert(1))'
    monkeypatch.setattr(agent, "run", lambda *a, **k: evil)
    pipeline.process_shared_video(session, transcribed)
    page = TestClient(app).get("/agent").text
    assert "<img src=x" not in page and "javascript:alert" not in page


def test_agent_page_empty_state():
    assert "No sidequests yet" in TestClient(app).get("/").text


def test_settings_page_shows_pairing_code():
    from app.config import new_pairing_code, save_telegram_settings
    save_telegram_settings(bot_token="123:abc", bot_username="mybot")
    code = new_pairing_code()
    page = TestClient(app).get("/settings").text
    assert f"/start {code}" in page and "@mybot" in page


def test_agent_page_shows_project_fit_and_deeper_button(session, transcribed, stub_analysis, monkeypatch):
    f = _findings()
    f.project_fit = [{"project": "llm-inference-gateway", "fit": "worth_it", "where": "gateway/batcher.py",
                      "architecture_impact": "Replaces the fixed-window batcher", "effort": "S"}]
    f.new_project_idea = "A Philly food map"
    monkeypatch.setattr(agent, "run", lambda *a, **k: f)
    pipeline.process_shared_video(session, transcribed)
    page = TestClient(app).get("/agent").text
    assert "Fits your projects" in page and "Replaces the fixed-window batcher" in page
    assert "A Philly food map" in page and "Research deeper" in page


def test_deep_rerun_route_forces_web(session, transcribed, stub_analysis, monkeypatch):
    calls = []
    monkeypatch.setattr("app.intake.rerun", lambda action_id, deep=False: calls.append((action_id, deep)) or True)
    TestClient(app).post("/agent/7/rerun", data={"deep": "true"})
    assert calls == [(7, True)]


def test_settings_page_shows_send_to_agent_setup():
    from app.config import new_ntfy_setup
    s = new_ntfy_setup()
    page = TestClient(app).get("/settings").text
    assert s["inbox_topic"] in page and s["token"] in page and "Your projects" in page
