"""The multi-agent trip planner and its eval, and the Todoist / Notion tools. All faked."""
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import select

from app import assistant, mcp_client, notion, todoist, trip_eval, trip_planner
from app.config import save_profile_fact, save_quest_settings
from app.models import Run


def _ix(text="", id_="i", steps=()):
    usage = SimpleNamespace(total_input_tokens=1000, total_output_tokens=200, total_thought_tokens=0,
                            total_cached_tokens=0, total_tool_use_tokens=0, input_tokens_by_modality=[])
    return SimpleNamespace(id=id_, steps=list(steps), usage=usage, output_text=text)


class RoleGenAI:
    """Answers by role: each specialist (recognised from its system prompt) and the coordinator."""
    def __init__(self):
        self.requests, self.lock = [], threading.Lock()
        self.interactions = SimpleNamespace(create=self._create)

    def _create(self, **params):
        with self.lock:
            self.requests.append(params)
        system = params.get("system_instruction", "")
        if "Cover all of these yourself" in system:
            return _ix("Single agent plan: $109 flight https://kiwi.com/x, hotel in Wynwood, visit Little Havana.")
        if "Briefs:" in str(params.get("input", "")):
            return _ix("**Plan**: fly Frontier $109 https://kiwi.com/x, stay in Wynwood hotel $180/night, eat at Versailles.")
        for role in ("Flights (cash)", "Points and miles", "Where to stay", "What to do"):
            if role in system:
                return _ix(f"{role} brief")
        return _ix("Single agent plan: $109 flight https://kiwi.com/x, hotel in Wynwood, visit Little Havana.")


@pytest.fixture(autouse=True)
def no_mcp(monkeypatch):
    monkeypatch.setattr(mcp_client, "tools", lambda lister=None: [])


def test_parallel_plan_runs_specialists_then_coordinator(session):
    client = RoleGenAI()
    p = trip_planner.plan("Miami long weekend", client=client)
    assert set(p.briefs) == {"flights", "stay", "do"}            # no points on file: no points agent
    assert p.briefs["stay"] == "Where to stay brief"
    assert "Wynwood" in p.text and p.cost_usd > 0
    coordinator = [r for r in client.requests if "Briefs:" in str(r.get("input", ""))]
    assert len(coordinator) == 1 and "Flights (cash) brief" in coordinator[0]["input"]
    run = session.exec(select(Run).where(Run.kind == "trip_plan")).one()
    agents = [s["name"] for s in json.loads(run.steps) if s.get("t") == "tool"]
    assert sorted(agents) == ["agent:coordinator", "agent:do", "agent:flights", "agent:stay"]
    assert run.cost_usd == pytest.approx(p.cost_usd, abs=1e-5)


def test_points_specialist_joins_when_a_balance_is_on_file(session):
    save_profile_fact("Chase Ultimate Rewards points", "142,000")
    p = trip_planner.plan("Montreal weekend", client=RoleGenAI())
    assert "points" in p.briefs


def test_single_mode_is_one_agent_with_every_tool(session):
    client = RoleGenAI()
    p = trip_planner.plan("Miami long weekend", mode="single", client=client)
    assert p.mode == "single" and len(client.requests) == 1 and "Single agent plan" in p.text
    names = {t.get("name") for t in client.requests[0]["tools"]}
    assert "search_my_stuff" in names


def test_a_failing_specialist_leaves_a_gap_not_a_crash(session, monkeypatch):
    real = trip_planner._run_agent

    def flaky(client, system, text, tools, run_tool, label):
        if label == "stay":
            raise RuntimeError("boom")
        return real(client, system, text, tools, run_tool, label)
    monkeypatch.setattr(trip_planner, "_run_agent", flaky)
    p = trip_planner.plan("Miami", client=RoleGenAI())
    assert p.briefs["stay"] == "(this specialist couldn't finish)" and p.text


def test_trip_eval_scores_both_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(trip_eval, "RUNS_DIR", tmp_path)

    def plan_fn(request, mode):
        text = ("Fly $120 https://kiwi.com/a, hotel downtown, eat at Lou Malnati's, 1.6¢ per point"
                if mode == "parallel" else "Fly $120, nice trip")
        return trip_planner.Plan(text, mode, cost_usd=0.05 if mode == "parallel" else 0.02,
                                 seconds=20 if mode == "parallel" else 35)

    s = trip_eval.run(1.0, plan_fn=plan_fn, judge_fn=lambda r, t: (5 if "hotel" in t else 3, 0.0005),
                      on_case=lambda *_: None)
    assert s["modes"]["parallel"]["checks"] == 1.0 and s["modes"]["single"]["checks"] < 1.0
    assert s["modes"]["parallel"]["grade"] == 5 and s["modes"]["single"]["seconds_p50"] == 35
    table = trip_eval.table(s)
    assert "4 specialists in parallel" in table and "$0.050" in table
    assert list(tmp_path.glob("*.json"))


def test_trip_eval_stops_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(trip_eval, "RUNS_DIR", tmp_path)
    s = trip_eval.run(0.1, plan_fn=lambda r, mode: trip_planner.Plan("x", mode, cost_usd=0.06, seconds=1),
                      judge_fn=lambda r, t: (3, 0.0), on_case=lambda *_: None)
    assert len(s["results"]) == 2


def test_plan_trip_tool_from_the_assistant(monkeypatch):
    monkeypatch.setattr(trip_planner, "plan", lambda request: trip_planner.Plan(f"plan for {request}", "parallel"))
    assert assistant._run_tool("plan_trip", {"request": "Miami"}, [])["plan"] == "plan for Miami"


# ---------------- Todoist ----------------

def test_todoist_add_list_complete():
    save_quest_settings(todoist_token="tok")
    seen = []

    def handler(r):
        seen.append((r.method, r.url.path, dict(r.url.params), r.headers.get("authorization"),
                     json.loads(r.content) if r.content else None))
        if r.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"id": "88", "content": "Passport photos", "due": {"string": "Fri"}})
        if r.url.path.endswith("/filter"):
            return httpx.Response(200, json={"results": [{"id": "88", "content": "Passport photos", "priority": 1}],
                                             "next_cursor": None})
        return httpx.Response(204)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    added = todoist.add_task("Passport photos", due_string="friday", priority=9, http=http)
    assert added["id"] == "88" and added["url"].endswith("/88")
    assert seen[0][1] == "/api/v1/tasks" and seen[0][3] == "Bearer tok" and seen[0][4]["priority"] == 4
    assert todoist.list_tasks(http=http)["tasks"][0]["content"] == "Passport photos"
    assert seen[1][2]["query"] == "today | overdue"
    assert todoist.complete_task("88", http=http)["ok"] and seen[2][1] == "/api/v1/tasks/88/close"
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert "rejected" in todoist.add_task("x", http=bad)["error"]


def test_todoist_and_notion_tools_only_when_connected():
    names = lambda: {t.get("name") for t in assistant._tools()}  # noqa: E731
    assert "todoist_add_task" not in names() and "notion_save_note" not in names()
    save_quest_settings(todoist_token="t", notion_token="n", notion_page="https://www.notion.so/Notes-" + "a" * 32)
    assert {"todoist_add_task", "notion_save_note", "notion_search_notes"} <= names()


# ---------------- Notion ----------------

def test_notion_page_ids():
    assert notion.page_id("https://www.notion.so/ws/Cafe-Notes-" + "1a2b3c4d" * 4 + "?pvs=4") == \
        "1a2b3c4d-1a2b-3c4d-1a2b-3c4d1a2b3c4d"
    assert notion.page_id("https://example.com/nope") == ""


def test_notion_save_and_search():
    save_quest_settings(notion_token="secret_x", notion_page="https://www.notion.so/Notes-" + "b" * 32)
    seen = []

    def handler(r):
        seen.append((r.url.path, r.headers.get("notion-version"), json.loads(r.content)))
        if r.url.path == "/v1/pages":
            return httpx.Response(200, json={"url": "https://www.notion.so/Packing-list-c"})
        return httpx.Response(200, json={"results": [{"url": "u", "last_edited_time": "2026-11-20T10:00:00Z",
                                                      "properties": {"title": {"type": "title",
                                                                               "title": [{"plain_text": "Packing list"}]}}}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    out = notion.save_note("Packing list", "Passport\n\nCharger", http=http)
    assert out["url"].endswith("Packing-list-c")
    path, version, body = seen[0]
    assert version == notion.VERSION and body["parent"]["page_id"] == "b" * 8 + "-" + "b" * 4 + "-" + "b" * 4 + "-" + "b" * 4 + "-" + "b" * 12
    assert len(body["children"]) == 2
    assert notion.search_notes("packing", http=http)["pages"][0]["title"] == "Packing list"
    missing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert "share it" in notion.save_note("x", "y", http=missing)["error"]
