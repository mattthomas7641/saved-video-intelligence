"""The Sidequest home page: verdict-aware tabs, report clean-up, the status poll, dates."""
import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import _render_markdown, _short_date, _strip_citations, app
from app.models import Action, ActionStatus, ActionType, Status, Video


def _item(session, verdict, status=ActionStatus.DRAFTED, headline="h"):
    v = Video(tiktok_url=f"https://www.tiktok.com/@a/video/{headline}", status=Status.DONE, source="telegram",
              category="Tech / AI / Coding")
    session.add(v)
    session.commit()
    a = Action(video_id=v.id, action_type=ActionType.SKILL, status=status, verdict=verdict,
               result=json.dumps({"headline": headline, "telegram_text": "t", "report_markdown": "r"}))
    session.add(a)
    session.commit()
    return a


def test_nothing_to_do_items_leave_the_to_do_list(session):
    _item(session, "worth_it", headline="DoThisOne")
    _item(session, "skip", headline="NothingHere")
    client = TestClient(app)
    todo, fyi = client.get("/").text, client.get("/?tab=fyi").text
    assert "DoThisOne" in todo and "NothingHere" not in todo
    assert "NothingHere" in fyi and "DoThisOne" not in fyi


def test_citations_are_stripped_and_headings_demoted():
    assert _strip_citations("Limine is maintained [1.2.3] and fast [4, 5].") == "Limine is maintained and fast."
    assert _strip_citations("see [docs](https://x)") == "see [docs](https://x)"  # links survive
    html = str(_render_markdown("### Why [2]\nBecause."))
    assert "<h5>Why</h5>" in html and "[2]" not in html


def test_status_endpoint_tracks_work(session):
    client = TestClient(app)
    idle = client.get("/api/quests/status").json()
    assert idle["working"] == 0
    _item(session, None, status=ActionStatus.IN_PROGRESS, headline="Busy")
    busy = client.get("/api/quests/status").json()
    assert busy["working"] == 1 and busy["stamp"] != idle["stamp"]


def test_page_only_polls_while_something_is_working(session):
    client = TestClient(app)
    assert "/api/quests/status" not in client.get("/").text
    _item(session, None, status=ActionStatus.IN_PROGRESS, headline="Busy")
    page = client.get("/").text
    assert "/api/quests/status" in page and 'http-equiv="refresh"' not in page


def test_dates_are_local_and_friendly():
    now = datetime.utcnow()
    assert _short_date(now) == "Today"
    assert _short_date(now - timedelta(days=1)) in ("Yesterday", "Today")  # around local midnight
    assert _short_date(datetime(2020, 3, 4, 12)) == "Mar 4, 2020"
    assert _short_date(None) == "—"


def test_research_deeper_asks_before_spending(session):
    _item(session, "worth_it", headline="Spendy")
    assert "confirm('Research this again" in TestClient(app).get("/").text


def test_update_notice_starts_hidden_and_fyi_items_can_be_closed(session):
    _item(session, "skip", headline="Quiet")
    _item(session, None, status=ActionStatus.IN_PROGRESS, headline="Busy")
    page = TestClient(app).get("/?tab=fyi").text
    assert 'id="updates" hidden' in page
    assert "/done" in page and "Not for me" in page and "Back to To do" not in page
