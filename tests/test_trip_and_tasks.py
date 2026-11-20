"""The multi-agent trip planner and its eval, and the Todoist / Notion tools. All faked."""
import json
import threading
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app import assistant, mcp_client, trip_eval, trip_planner
from app.config import save_profile_fact
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
        if "Do the whole job yourself" in system:
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
    from app import team
    real = team.run_agent

    def flaky(client, system, text, tools, run_tool, label, steps=6):
        if label == "stay":
            raise RuntimeError("boom")
        return real(client, system, text, tools, run_tool, label, steps)
    monkeypatch.setattr(team, "run_agent", flaky)
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
    monkeypatch.setattr(trip_planner, "plan", lambda request, mode="parallel": trip_planner.Plan(f"plan for {request}", mode))
    assert assistant._run_tool("plan_trip", {"request": "Miami"}, [])["plan"] == "plan for Miami"


# ---------------- the general team (app/team.py) ----------------

class PlannerGenAI(RoleGenAI):
    def __init__(self, plan_json):
        super().__init__()
        self.plan_json = plan_json

    def _create(self, **params):
        if "Split the request" in params.get("system_instruction", ""):
            with self.lock:
                self.requests.append(params)
            return _ix(self.plan_json)
        if "Your part:" in params.get("system_instruction", ""):
            with self.lock:
                self.requests.append(params)
            job = params["system_instruction"].split("Your part: ")[1].split("\n")[0]
            return _ix(f"brief: {job}")
        if "Briefs:" in str(params.get("input", "")):
            with self.lock:
                self.requests.append(params)
            return _ix("Merged: get the **ThinkPad** https://x.test")
        return super()._create(**params)


def test_team_plans_runs_in_parallel_and_merges(session):
    from app import team
    plan = json.dumps({"team": True, "subtasks": [
        {"role": "laptops", "job": "Compare 3 coding laptops under $1,200", "tools": ["web"]},
        {"role": "reviews", "job": "Find reliability reviews", "tools": ["web", "bogus"]},
        {"role": "reviews", "job": "Check what I saved about laptops", "tools": ["my_stuff"]}]})
    client = PlannerGenAI(plan)
    r = team.run_team("best laptop for coding under 1200?", client=client)
    assert r.mode == "team" and list(r.briefs) == ["laptops", "reviews", "reviews 2"]
    assert r.subtasks[1]["tools"] == ["web"]                      # unknown groups dropped
    assert "ThinkPad" in r.text
    specialist = [q for q in client.requests if "Check what I saved" in q.get("system_instruction", "")][0]
    names = {t.get("name") or t.get("type") for t in specialist["tools"]}
    assert names == {"search_my_stuff", "list_todo"}              # only its group's tools, no web


def test_planner_says_no_team_runs_one_agent(session):
    from app import team
    client = PlannerGenAI(json.dumps({"team": False}))
    r = team.run_team("what time is it in Tokyo?", client=client)
    assert r.mode == "single" and r.subtasks == []
    assert team.plan_subtasks(PlannerGenAI("not json"), "x", "")[0] == []
    one = json.dumps({"team": True, "subtasks": [{"role": "a", "job": "j", "tools": ["web"]}]})
    assert team.plan_subtasks(PlannerGenAI(one), "x", "")[0] == []   # a team of one isn't a team


def test_specialists_can_only_read():
    from app import team
    ran = []
    guarded = team.read_only(lambda name, args: ran.append(name) or {"ok": True})
    assert "error" in guarded("remind_me", {"when": "x", "text": "y"})
    assert "error" in guarded("todoist_add_task", {"content": "x"})
    assert "error" in guarded("propose_calendar_event", {})
    assert guarded("search_my_stuff", {"query": "x"}) == {"ok": True} and ran == ["search_my_stuff"]


def test_team_up_tool_from_the_assistant(monkeypatch):
    from app import team
    monkeypatch.setattr(team, "run_team", lambda request: team.Result(f"answer for {request}", "team",
                                                                       subtasks=[{"role": "a"}, {"role": "b"}]))
    out = assistant._run_tool("team_up", {"request": "compare gyms"}, [])
    assert out["answer"] == "answer for compare gyms" and out["team"] == ["a", "b"]
