"""API-level tests via FastAPI's TestClient."""
from fastapi.testclient import TestClient

from app.main import app


def test_health_needs_no_auth():
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_retired_daily_agent_routes_are_gone():
    """The bearer-token daily-agent API, inbox scraper and bulk pages were removed with Sidequest."""
    client = TestClient(app)
    for method, path in [("get", "/api/actions/queue"), ("post", "/api/sync/inbox"), ("post", "/api/ingest/links"),
                         ("get", "/actions"), ("get", "/jobs"), ("get", "/bulk"), ("get", "/onboarding")]:
        assert getattr(client, method)(path).status_code in (404, 405), path
