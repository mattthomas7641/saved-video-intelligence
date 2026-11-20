"""app/mcp_server.py: Sidequest's MCP tools, called directly and over the real
streamable-HTTP endpoint (behind its bearer token). No network: the inbox post is faked."""
import json

import httpx
from fastapi.testclient import TestClient

from app import mcp_server
from app.config import new_mcp_token, save_ntfy_settings
from app.models import Action, ActionStatus, ActionType, Status, Task, Video


def _item(session, headline="Try the batching trick", status=ActionStatus.DRAFTED):
    v = Video(tiktok_url=f"https://www.tiktok.com/@a/video/{abs(hash(headline))}", status=Status.DONE, source="telegram",
              summary="dynamic batching for vLLM")
    session.add(v)
    session.commit()
    a = Action(video_id=v.id, action_type=ActionType.SKILL, status=status, verdict="worth_it",
               result=json.dumps({"headline": headline, "telegram_text": "Add it to the gateway",
                                  "report_markdown": "Batching cuts cost 40%.", "project_fit": []}))
    session.add(a)
    session.commit()
    return a


def test_list_and_get_report(session):
    a = _item(session)
    _item(session, "Old one", ActionStatus.DONE)
    session.add(Task(action_id=a.id, kind="follow_up", status="scheduled", title="Check back: release"))
    session.commit()
    todo = mcp_server.list_sidequests("todo")
    assert [x["headline"] for x in todo] == ["Try the batching trick"]
    assert len(mcp_server.list_sidequests("all")) == 2
    report = mcp_server.get_report(a.id)
    assert report["report_markdown"] == "Batching cuts cost 40%." and report["next_steps"][0]["kind"] == "follow_up"
    assert "error" in mcp_server.get_report(999)


def test_keyword_search_without_embeddings(session):
    _item(session)
    hits = mcp_server.search_sidequests("batching gateway")
    assert hits and hits[0]["headline"] == "Try the batching trick"
    assert mcp_server.search_sidequests("pickleball") == []


def test_send_goes_through_the_private_inbox(monkeypatch):
    assert "error" in mcp_server.send_to_sidequest("https://github.com/a/b")   # not set up yet
    save_ntfy_settings(inbox_topic="in-topic", results_topic="out", token="tok")
    posted = {}

    def fake_post(url, content, timeout):
        posted["url"], posted["body"] = url, json.loads(content)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(mcp_server.httpx, "post", fake_post)
    out = mcp_server.send_to_sidequest("https://github.com/a/b", note="for the gateway")
    assert out["queued"]
    assert posted["url"].endswith("/in-topic") and posted["body"]["token"] == "tok"
    assert posted["body"]["note"] == "for the gateway"


def _rpc(client, method, params=None, token=None, id_=1):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp/", headers=headers,
                       content=json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}))


def test_http_endpoint_needs_the_token_and_speaks_mcp(session):
    from app.main import app

    _item(session)
    with TestClient(app) as client:
        assert _rpc(client, "tools/list").status_code == 401           # no token created yet
        token = new_mcp_token()
        assert _rpc(client, "tools/list", token="wrong").status_code == 401
        init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}}, token=token)
        assert init.status_code == 200, init.text
        tools = _rpc(client, "tools/list", token=token, id_=2)
        names = {t["name"] for t in tools.json()["result"]["tools"]}
        assert {"list_sidequests", "get_report", "search_sidequests", "upcoming", "send_to_sidequest"} <= names
        call = _rpc(client, "tools/call", {"name": "list_sidequests", "arguments": {"status": "todo"}},
                    token=token, id_=3)
        assert "Try the batching trick" in call.text


def test_settings_shows_the_token_once():
    from app.main import app

    client = TestClient(app)
    r = client.post("/settings/mcp", follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert "shown once" in page and "claude mcp add sidequest" in page
    assert "shown once" not in client.get("/settings").text


# ---------------- OpenTelemetry export (app/otel.py) ----------------

def test_run_exports_genai_spans():
    from datetime import datetime

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app import otel, tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with tracing.run("research", action_id=7, provider="gemini") as t:
        tracing.model_call("gemini-3.8-flash", 0.01, 1000, 200, searches=2, ms=900)
        tracing.tool_call("github_repo", {"repo": "a/b"}, ok=False, ms=120, note="not found")
    otel.export_run(t, datetime.utcnow(), "ok", None, tracer=provider.get_tracer("test"))
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"invoke_agent research", "chat gemini-3.8-flash", "execute_tool github_repo"}
    root, chat, tool = spans["invoke_agent research"], spans["chat gemini-3.8-flash"], spans["execute_tool github_repo"]
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["sidequest.action_id"] == 7
    assert chat.attributes["gen_ai.usage.input_tokens"] == 1000 and chat.attributes["sidequest.searches"] == 2
    assert chat.parent.span_id == root.context.span_id
    assert tool.attributes["gen_ai.tool.name"] == "github_repo" and not tool.status.is_ok


def test_export_is_off_without_an_endpoint(monkeypatch):
    from app import otel

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(otel, "_tracer", None)
    monkeypatch.setattr(otel, "_disabled", False)
    assert otel._get_tracer() is None
