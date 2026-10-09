"""The shared-video fast lane (pipeline.process_shared_video), the Telegram result
message, and the /agent page, with collect/analyze/research stubbed out."""
import json

import pytest
from fastapi.testclient import TestClient

from app import agent, pipeline
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


@pytest.fixture
def stub_analysis(monkeypatch):
    monkeypatch.setattr(pipeline.analyze_mod, "analyze_video", lambda video: AnalysisResult(**ANALYSIS))


def test_shared_video_is_analyzed_routed_and_researched(session, transcribed, stub_analysis, monkeypatch):
    seen = {}
    def fake_run(video, action_type, brief, budget, on_progress):
        seen.update(type=action_type, brief=brief, budget=budget)
        return _findings()
    monkeypatch.setattr(agent, "run", fake_run)
    progress = []
    action = pipeline.process_shared_video(session, transcribed, progress.append, chat_id=42, message_id=7)
    assert seen["type"] == "place" and seen["brief"] == "Dalessandro's, Philly"
    assert action.status == ActionStatus.DRAFTED and action.verdict == "worth_it" and action.cost_usd == 0.21
    assert action.telegram_chat_id == 42 and json.loads(action.result)["place"]["name"] == "Dalessandro's"
    assert progress[0] == "Reading the video…"


def test_budget_passed_to_agent_subtracts_todays_spend(session, transcribed, stub_analysis, monkeypatch):
    save_agent_settings(daily_cap_usd=2.0)
    other = Video(tiktok_url="https://www.tiktok.com/@b/video/1", status=Status.DONE)
    session.add(other)
    session.commit()
    session.add(Action(video_id=other.id, action_type=ActionType.REPO, cost_usd=0.5))
    session.commit()
    budgets = []
    monkeypatch.setattr(agent, "run", lambda v, t, b, budget, on_progress: budgets.append(budget) or _findings())
    pipeline.process_shared_video(session, transcribed)
    assert budgets == [pytest.approx(1.5)]


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
    monkeypatch.setattr(pipeline.analyze_mod, "analyze_video",
                        lambda video: AnalysisResult(**{**ANALYSIS, "is_actionable": False}))
    monkeypatch.setattr(agent, "run", lambda *a, **k: _findings())
    action = pipeline.process_shared_video(session, transcribed)
    assert action.action_type == ActionType.OTHER and action.status == ActionStatus.DRAFTED


def test_format_result_for_a_finished_report(session, transcribed, stub_analysis, monkeypatch):
    monkeypatch.setattr(agent, "run", lambda *a, **k: _findings())
    action = pipeline.process_shared_video(session, transcribed)
    text = tb.format_result(action, transcribed)
    assert "<b>Go on a weekday before 6pm</b>" in text and "Worth it" in text and "Place" in text
    assert "Pair with a Wissahickon hike" in text


def test_format_result_escapes_html(session, transcribed):
    action = Action(id=1, video_id=transcribed.id, action_type=ActionType.OTHER, status=ActionStatus.FAILED,
                    error_message="<script>")
    assert "&lt;script&gt;" in tb.format_result(action, transcribed)


def test_agent_page_renders_reports_and_filters(session, transcribed, stub_analysis, monkeypatch):
    monkeypatch.setattr(agent, "run", lambda *a, **k: _findings())
    pipeline.process_shared_video(session, transcribed)
    client = TestClient(app)
    page = client.get("/agent").text
    assert "Go on a weekday before 6pm" in page and "Dalessandro" in page and "<strong>Order</strong>" in page
    assert "Go on a weekday" not in client.get("/agent?type=repo").text
    assert "Go on a weekday" in client.get("/agent?verdict=worth_it").text


def test_agent_page_markdown_cannot_inject_html(session, transcribed, stub_analysis, monkeypatch):
    evil = _findings()
    evil.report_markdown = '<img src=x onerror=alert(1)> [click](javascript:alert(1))'
    monkeypatch.setattr(agent, "run", lambda *a, **k: evil)
    pipeline.process_shared_video(session, transcribed)
    page = TestClient(app).get("/agent").text
    assert "<img src=x" not in page and "javascript:alert" not in page


def test_agent_page_empty_state():
    assert "Nothing shared yet" in TestClient(app).get("/agent").text


def test_settings_page_shows_pairing_code():
    from app.config import new_pairing_code, save_telegram_settings
    save_telegram_settings(bot_token="123:abc", bot_username="mybot")
    code = new_pairing_code()
    page = TestClient(app).get("/settings").text
    assert f"/start {code}" in page and "@mybot" in page
