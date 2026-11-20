"""Sidequest as an MCP server, so other agents (Claude Code, Claude Desktop, any MCP
client) can ask what you've sent, read a report, see what's coming up, and send
something new in.

Two ways to connect:
- stdio, local only, nothing exposed: the client starts
  `docker compose exec -T app python -m app.mcp_server`. For Claude Code:
      claude mcp add sidequest -- docker compose -f "<repo>/docker-compose.yml" exec -T app python -m app.mcp_server
- HTTP at /mcp/ on the running app, with the bearer token from Settings, for a
  client that needs a URL (e.g. over Tailscale). The app's Host check applies too.

The tools are read-only except `send_to_sidequest`, which goes through the same
private ntfy inbox as the iOS Shortcut, so the running app researches it (a few
cents, inside the daily cap). Approving follow-through stays with you: no tool
can open an issue or touch your calendar.
"""
import hmac
import json
import logging

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlmodel import select

log = logging.getLogger(__name__)

server = MCPServer(
    name="sidequest",
    title="Sidequest",
    instructions=("Sidequest is the user's research inbox: things they shared from their phone (videos, links, "
                  "screenshots), each researched into a report with a verdict and next steps. Use search_sidequests "
                  "to find what they've sent about a topic before suggesting something, and get_report for the "
                  "details. send_to_sidequest queues new research (costs a few cents): only use it when asked."),
)

_STATUS = {"todo": ("drafted",), "done": ("done",), "not_for_me": ("dismissed",),
           "stuck": ("failed", "needs_input"), "all": None}


def _brief(action, video, report: dict | None) -> dict:
    return {"id": action.id, "type": action.action_type.value, "status": action.status.value,
            "verdict": action.verdict, "headline": (report or {}).get("headline") or video.summary or video.caption,
            "next_step": (report or {}).get("telegram_text", ""), "sent": f"{action.created_at:%Y-%m-%d}",
            "source": video.tiktok_url if (video.tiktok_url or "").startswith("http") else None}


@server.tool()
def list_sidequests(status: str = "todo", limit: int = 20) -> list[dict]:
    """Things the user sent, newest first. status: todo (researched, waiting on them), done,
    not_for_me, stuck (couldn't finish) or all."""
    from app.agent import load_report
    from app.db import get_session
    from app.models import Action, Video

    with get_session() as session:
        q = (select(Action, Video).join(Video, Video.id == Action.video_id).where(Video.source == "telegram")
             .order_by(Action.updated_at.desc()).limit(max(1, min(int(limit), 100))))
        statuses = _STATUS.get(status, _STATUS["todo"])
        if statuses:
            q = q.where(Action.status.in_(statuses))
        return [_brief(a, v, load_report(a.result)) for a, v in session.exec(q).all()]


@server.tool()
def get_report(sidequest_id: int) -> dict:
    """The full report for one sidequest: verdict, write-up, place details, project fit, sources,
    and its next steps (times to pick, issues awaiting approval, scheduled check-backs)."""
    from app import quests
    from app.agent import load_report
    from app.db import get_session
    from app.models import Action, Video

    with get_session() as session:
        action = session.get(Action, int(sidequest_id))
        if action is None:
            return {"error": f"No sidequest {sidequest_id}."}
        video = session.get(Video, action.video_id)
        report = load_report(action.result) or {}
        steps = [{"kind": t.kind, "status": t.status, "title": t.title,
                  "when": f"{t.run_at:%Y-%m-%d}" if t.run_at else None,
                  "result": json.loads(t.result) if t.result else None}
                 for t in quests.for_actions(session, [action.id]).get(action.id, [])]
        return {**_brief(action, video, report), "report_markdown": report.get("report_markdown", ""),
                "place": report.get("place"), "project_fit": report.get("project_fit", []),
                "new_project": report.get("new_project"), "links": report.get("links", []),
                "related": report.get("related", []), "next_steps": steps,
                "error": action.error_message}


@server.tool()
def search_sidequests(query: str, limit: int = 5) -> list[dict]:
    """Past reports related to a topic, best match first (semantic search over the user's
    reports; falls back to keyword match if embeddings aren't available)."""
    from app import memory
    from app.agent import load_report
    from app.db import get_session
    from app.models import Action, Video

    limit = max(1, min(int(limit), 20))
    with get_session() as session:
        if memory.enabled():
            try:
                hits = memory.recall(session, query, k=limit)
                return [{"id": h["action_id"], "headline": h["headline"], "verdict": h["verdict"],
                         "status": h["status"], "sent": h["date"], "similarity": h["similarity"]} for h in hits]
            except Exception:  # noqa: BLE001 - fall through to keywords
                log.exception("Semantic search failed; using keywords")
        words = [w.lower() for w in query.split() if len(w) > 2]
        out = []
        for a, v in session.exec(select(Action, Video).join(Video, Video.id == Action.video_id)
                                 .where(Action.result.is_not(None)).order_by(Action.updated_at.desc())).all():
            text = f"{a.result} {v.summary or ''}".lower()
            score = sum(text.count(w) for w in words)
            if score:
                out.append((score, _brief(a, v, load_report(a.result))))
        return [b for _, b in sorted(out, key=lambda x: -x[0])[:limit]]


@server.tool()
def upcoming(limit: int = 10) -> list[dict]:
    """Scheduled check-backs (stories, launches, price drops the agent will look at again) and
    steps waiting on the user's approval."""
    from app import quests
    from app.db import get_session
    from app.models import Task

    with get_session() as session:
        waiting = session.exec(select(Task).where(Task.status == "proposed").order_by(Task.id.desc())
                               .limit(int(limit))).all()
        return ([{"sidequest_id": t.action_id, "kind": t.kind, "title": t.title, "status": "waiting_on_user"}
                 for t in waiting]
                + [{"sidequest_id": t.action_id, "kind": t.kind, "title": t.title, "status": "scheduled",
                    "when": f"{t.run_at:%Y-%m-%d}"} for t in quests.upcoming(session, int(limit))])


@server.tool()
def send_to_sidequest(link_or_text: str, note: str = "") -> dict:
    """Queue something for research, exactly as if the user shared it from their phone: a link
    (video, repo, article, product, place) or plain text. Costs a few cents; the result
    arrives as a phone notification and on the Sidequest home page. Only use when asked."""
    from app.config import get_ntfy_settings

    s = get_ntfy_settings()
    if not (s["inbox_topic"] and s["token"]):
        return {"error": "Send to Sidequest isn't set up yet (Settings in the Sidequest app)."}
    body = {"v": 1, "token": s["token"], "url": link_or_text.strip()[:4000], "note": note.strip()[:500]}
    try:
        httpx.post(f"{s['server']}/{s['inbox_topic']}", content=json.dumps(body), timeout=15).raise_for_status()
    except httpx.HTTPError as e:
        return {"error": f"Couldn't reach the inbox: {type(e).__name__}"}
    return {"queued": True, "message": "Sent. The result arrives as a notification in a few minutes."}


# ---------------- HTTP transport (mounted at /mcp/ by app/main.py) ----------------

def http_app():
    """The streamable-HTTP app, behind a bearer token. Host/Origin checks are the main app's
    GuardMiddleware's job, so the SDK's own DNS-rebinding check is off."""
    inner = server.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return _BearerGuard(inner)


class _BearerGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from app.config import get_mcp_token

        if scope["type"] == "http":
            token = get_mcp_token()
            header = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if not token or not hmac.compare_digest(header, f"Bearer {token}"):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"MCP token required (Sidequest Settings)."})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)  # stdout is the protocol channel: keep logs on stderr and quiet
    server.run("stdio")
