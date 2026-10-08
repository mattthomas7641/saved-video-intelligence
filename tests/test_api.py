"""API-level tests via FastAPI's TestClient - the agent-facing /api/* routes
specifically, since those are new, authenticated, and meant to be called
unattended (vs. the existing form-POST dashboard routes, which stay
deliberately unauthenticated and are exercised manually/via the UI)."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_agent_token
from app.db import get_session
from app.main import app
from app.models import Action, ActionStatus, ActionType, Video


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {get_agent_token()}"}


def test_health_needs_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_actions_queue_rejects_missing_token(client):
    resp = client.get("/api/actions/queue")
    assert resp.status_code == 401


def test_actions_queue_rejects_wrong_token(client):
    resp = client.get("/api/actions/queue", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_actions_queue_accepts_correct_token(client, auth_headers):
    resp = client.get("/api/actions/queue", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"queue": [], "settings": resp.json()["settings"]}


def test_ingest_links_dedups_against_existing_videos(client, auth_headers):
    session = get_session()
    session.add(Video(tiktok_url="https://www.tiktok.com/@x/video/1"))
    session.commit()
    session.close()

    resp = client.post("/api/ingest/links", headers=auth_headers, json=[
        {"tiktok_url": "https://www.tiktok.com/@x/video/1", "saved_date": None},  # already known
        {"tiktok_url": "https://www.tiktok.com/@x/video/2", "saved_date": None},  # new
    ])
    assert resp.status_code == 200
    assert resp.json() == {"seen": 2, "added": 1}


def test_ingest_links_skips_blank_urls(client, auth_headers):
    resp = client.post("/api/ingest/links", headers=auth_headers, json=[{"tiktok_url": "", "saved_date": None}])
    assert resp.json() == {"seen": 0, "added": 0}


def test_action_update_rejects_invalid_status(client, auth_headers):
    resp = client.post("/api/actions/1", headers=auth_headers, json={"status": "not-a-real-status"})
    assert resp.status_code == 400


def test_action_update_404s_on_missing_action(client, auth_headers):
    resp = client.post("/api/actions/999999", headers=auth_headers, json={"status": "done"})
    assert resp.status_code == 404


def test_action_update_persists_status_and_result(client, auth_headers):
    session = get_session()
    v = Video(tiktok_url="https://www.tiktok.com/@x/video/3")
    session.add(v)
    session.commit()
    session.refresh(v)
    a = Action(video_id=v.id, action_type=ActionType.PROJECT, status=ActionStatus.QUEUED, brief="build it")
    session.add(a)
    session.commit()
    session.refresh(a)
    action_id = a.id
    session.close()

    resp = client.post(f"/api/actions/{action_id}", headers=auth_headers,
                        json={"status": "drafted", "result": {"pr_url": "https://example.com/pr/1"}})
    assert resp.status_code == 200

    session = get_session()
    updated = session.get(Action, action_id)
    assert updated.status == ActionStatus.DRAFTED
    assert "pr/1" in updated.result
    session.close()


def test_sync_inbox_without_trusted_sender_returns_clear_error(client, auth_headers):
    """No TRUSTED_SENDER configured in the test data dir, so this must fail
    with a clear, actionable error - not silently act on messages from
    whoever happens to DM the bot account."""
    resp = client.post("/api/sync/inbox", headers=auth_headers)
    assert resp.status_code == 409
    assert "handle" in resp.json()["error"].lower()


def test_sync_inbox_without_login_session_returns_clear_error(client, auth_headers):
    """Trusted sender configured, but no data/tiktok_auth_state.json exists
    in the test data dir - must fail with a clear, actionable error, not a
    crash or a silent empty success that would look like 'no new saves'."""
    from app.config import save_trusted_sender
    save_trusted_sender("realaccount")
    resp = client.post("/api/sync/inbox", headers=auth_headers)
    assert resp.status_code == 409
    assert "login" in resp.json()["error"].lower()
