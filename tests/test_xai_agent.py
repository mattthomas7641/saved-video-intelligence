"""app/xai_client.py: Grok analysis, cards and the research loop, with a fake
OpenAI-SDK client replaying Responses API objects (no network, no spend)."""
import json
from types import SimpleNamespace

import pytest

from app import agent, xai_client
from app.analyze import FatalAnalysisError


class V:
    tiktok_url = "https://www.tiktok.com/@dev/video/1"
    author = "dev"
    caption = "this batching trick"
    summary = "Dynamic batching for LLM serving."
    transcript = "batch requests"
    ocr_text = ""
    hashtags = ""
    user_note = None


def _usage(i=2000, cached=0, o=300):
    return SimpleNamespace(input_tokens=i, output_tokens=o, input_tokens_details=SimpleNamespace(cached_tokens=cached))


def _resp(output, id_="r1", **u):
    return SimpleNamespace(id=id_, output=output, usage=_usage(**u), output_text="")


def _call(name, args, call_id="c1"):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args), call_id=call_id)


FINDINGS = {"verdict": "worth_it", "headline": "Use it in the gateway", "telegram_text": "Fits the gateway.",
            "report_markdown": "Details.", "project_fit": [
                {"project": "llm-inference-gateway", "fit": "worth_it", "where": "gateway/batcher.py",
                 "architecture_impact": "Replaces the fixed-window batcher", "effort": "S"}]}


class FakeOpenAI:
    def __init__(self, responses):
        self.responses_q = list(responses)
        self.requests = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **params):
        self.requests.append(params)
        return self.responses_q.pop(0)


def test_research_runs_tools_chains_responses_and_parses_fit(monkeypatch):
    monkeypatch.setattr(agent, "read_project_file", lambda p, path: {"content": "def batch(): ..."})
    client = FakeOpenAI([
        _resp([_call("read_project_file", {"project": "llm-inference-gateway", "path": "gateway/batcher.py"})]),
        _resp([_call("record_findings", FINDINGS, "c2")], id_="r2"),
    ])
    f = xai_client.research(V(), "skill", "batching", 1.0, client=client, web=False)
    assert f.verdict == "worth_it" and f.project_fit[0]["project"] == "llm-inference-gateway"
    second = client.requests[1]
    assert second["previous_response_id"] == "r1"
    out = second["input"][0]
    assert out["type"] == "function_call_output" and out["call_id"] == "c1" and "def batch" in out["output"]
    tool_types = [t.get("type") for t in client.requests[0]["tools"]]
    assert "web_search" not in tool_types and "x_search" not in tool_types


def test_research_tools_follow_the_jev_plan():
    client = FakeOpenAI([_resp([_call("record_findings", FINDINGS)])])
    xai_client.research(V(), "research", None, 1.0, client=client, web=True, x_search=True)
    types = [t.get("type") for t in client.requests[0]["tools"]]
    assert types[:2] == ["web_search", "x_search"]
    names = [t.get("name") for t in client.requests[0]["tools"] if t["type"] == "function"]
    assert names == ["github_repo", "read_project_file", "record_findings"]


def test_cost_counts_tokens_cache_and_searches():
    resp = SimpleNamespace(usage=_usage(i=1_000_000, cached=400_000, o=100_000),
                           output=[SimpleNamespace(type="web_search_call"), SimpleNamespace(type="web_search_call")])
    # grok-4.3: 600k fresh * 1.25 + 400k cached * 0.20 + 100k out * 2.50, per M; + 2 searches * $0.005
    assert xai_client.response_cost(resp, "grok-4.3") == pytest.approx(0.75 + 0.08 + 0.25 + 0.01)


def test_nudges_once_then_fails():
    msg = SimpleNamespace(type="message")
    client = FakeOpenAI([_resp([msg]), _resp([msg], id_="r2")])
    with pytest.raises(agent.AgentError):
        xai_client.research(V(), "other", None, 1.0, client=client, web=False)
    assert client.requests[1]["tool_choice"] == "required"


def test_budget_stops_the_loop(monkeypatch):
    monkeypatch.setattr(agent, "github_repo", lambda repo: {"stars": 1})
    client = FakeOpenAI([_resp([_call("github_repo", {"repo": "a/b"})], i=2_000_000)])  # ~$2.50
    with pytest.raises(agent.CapReached):
        xai_client.research(V(), "repo", None, 1.0, client=client, web=False)


def test_analyze_video_uses_record_analysis_and_grok_pricing():
    args = {"category": "Tech / AI / Coding", "summary": "s", "tags": [], "worth_rewatching_score": 3,
            "worth_rewatching_reason": "r", "has_promo_code": False, "has_dated_offer": False,
            "mentions_link_in_bio": False, "key_facts": [], "is_actionable": True, "action_type": "skill",
            "action_brief": "batching"}
    client = FakeOpenAI([_resp([_call("record_analysis", args)])])
    result = xai_client.analyze_video(V(), client=client)
    assert result.action_type == "skill" and result.cost_usd < 0.01
    assert client.requests[0]["tool_choice"] == "required"
    assert client.requests[0]["tools"][0]["name"] == "record_analysis"


def test_project_card_returns_text_and_cost():
    resp = _resp([])
    resp.output_text = "Purpose: routes LLM calls."
    card, cost = xai_client.project_card("gateway", "# Gateway", client=FakeOpenAI([resp]))
    assert card.startswith("Purpose") and cost > 0


def test_missing_key_is_fatal():
    with pytest.raises(FatalAnalysisError):
        xai_client.research(V(), "skill", None, 1.0, web=False)


def test_agent_run_dispatches_to_grok_when_chosen(monkeypatch):
    seen = {}
    monkeypatch.setattr(xai_client, "research", lambda *a, **k: seen.update(k) or "ok")
    assert agent.run(V(), "skill", None, 1.0, web=False, provider="xai") == "ok" and seen["web"] is False
