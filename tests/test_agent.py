"""app/agent.py: request building, the tool loop, the GitHub tool and the spend cap.

The Anthropic client is a fake that replays scripted responses, and GitHub is an
httpx.MockTransport, so none of this touches the network or spends anything.
"""
from types import SimpleNamespace

import httpx
import pytest

from app import agent
from app.config import save_agent_settings


class FakeVideo:
    tiktok_url = "https://www.tiktok.com/@dev/video/123"
    author = "dev"
    caption = "10 repos for building an OS"
    summary = "Lists repos for OS development."
    transcript = "check out limine and redox"
    ocr_text = ""
    user_note = "useful for my rust kernel?"


def _usage(i=1000, o=200, searches=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=searches))


def _msg(blocks, stop="tool_use", **usage):
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=_usage(**usage))


def _tool(name, input_, id_="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=input_, id=id_)


FINDINGS = {"verdict": "worth_it", "headline": "Limine is the bootloader to use", "telegram_text": "Use Limine.",
            "report_markdown": "## Why\nIt's maintained.", "ideas": ["Boot your kernel with Limine"],
            "repos": [{"name": "limine-bootloader/limine", "fit": "worth_it", "stars": 2500}]}


class FakeClient:
    """Replays responses in order and records every request it was sent."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.requests.append({**params, "messages": list(params["messages"])})
        return self.responses.pop(0)


def test_build_request_has_web_tools_github_and_findings():
    params = agent.build_request(FakeVideo(), "repo", "limine, redox", model="claude-opus-5")
    names = [t["name"] for t in params["tools"]]
    assert names == ["web_search", "web_fetch", "github_repo", "record_findings"]
    assert params["tools"][0]["type"] == "web_search_20260209"
    assert params["fallbacks"] == "default"
    content = params["messages"][0]["content"]
    assert "useful for my rust kernel?" in content and "Type: repo" in content


def test_build_request_uses_basic_web_tools_and_no_fallbacks_on_older_models():
    params = agent.build_request(FakeVideo(), "research", None, model="claude-haiku-4-5")
    assert params["tools"][0]["type"] == "web_search_20250305"
    assert "fallbacks" not in params


def test_build_request_includes_about_me():
    save_agent_settings(about_me="Building a hobby OS in Rust")
    content = agent.build_request(FakeVideo(), "repo", None)["messages"][0]["content"]
    assert "Building a hobby OS in Rust" in content


def test_run_executes_github_tool_then_returns_findings(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "github_repo", lambda repo: calls.append(repo) or {"stars": 2500})
    client = FakeClient([
        _msg([_tool("github_repo", {"repo": "limine-bootloader/limine"})]),
        _msg([_tool("record_findings", FINDINGS, id_="t2")], searches=2),
    ])
    findings = agent.run(FakeVideo(), "repo", "limine", budget_usd=5.0, client=client)
    assert calls == ["limine-bootloader/limine"]
    assert findings.verdict == "worth_it" and findings.repos[0]["name"] == "limine-bootloader/limine"
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "t1"
    assert findings.cost_usd > 0.02  # two searches at $0.01 plus tokens


def test_run_resumes_after_pause_turn_without_adding_a_user_message():
    client = FakeClient([
        _msg([SimpleNamespace(type="server_tool_use", name="web_search", input={}, id="s1")], stop="pause_turn"),
        _msg([_tool("record_findings", FINDINGS)]),
    ])
    agent.run(FakeVideo(), "research", None, budget_usd=5.0, client=client)
    assert client.requests[1]["messages"][-1]["role"] == "assistant"


def test_run_nudges_once_when_model_stops_without_a_report():
    client = FakeClient([
        _msg([SimpleNamespace(type="text", text="Here's what I found...")], stop="end_turn"),
        _msg([_tool("record_findings", FINDINGS)]),
    ])
    findings = agent.run(FakeVideo(), "research", None, budget_usd=5.0, client=client)
    assert "record_findings" in client.requests[1]["messages"][-1]["content"]
    assert findings.headline


def test_run_fails_after_a_second_reportless_stop():
    text = SimpleNamespace(type="text", text="done")
    client = FakeClient([_msg([text], stop="end_turn"), _msg([text], stop="end_turn")])
    with pytest.raises(agent.AgentError):
        agent.run(FakeVideo(), "research", None, budget_usd=5.0, client=client)


def test_run_raises_on_refusal():
    client = FakeClient([_msg([], stop="refusal")])
    with pytest.raises(agent.AgentError):
        agent.run(FakeVideo(), "research", None, budget_usd=5.0, client=client)


def test_run_refuses_to_start_with_no_budget_left():
    with pytest.raises(agent.CapReached):
        agent.run(FakeVideo(), "research", None, budget_usd=0, client=FakeClient([]))


def test_run_stops_mid_loop_when_budget_runs_out(monkeypatch):
    monkeypatch.setattr(agent, "github_repo", lambda repo: {"stars": 1})
    client = FakeClient([_msg([_tool("github_repo", {"repo": "a/b"})], i=2_000_000)])  # ~$10 of input
    with pytest.raises(agent.CapReached):
        agent.run(FakeVideo(), "repo", None, budget_usd=1.0, client=client)


def test_findings_with_unknown_verdict_fall_back_to_maybe():
    f = agent._findings_from({**FINDINGS, "verdict": "amazing"}, 0.1)
    assert f.verdict == "maybe"


def _github(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_github_repo_returns_live_facts():
    def handler(req):
        if req.url.path.endswith("/readme"):
            return httpx.Response(200, text="# Limine\nA modern bootloader.")
        return httpx.Response(200, json={"full_name": "limine-bootloader/limine", "stargazers_count": 2500,
                                         "pushed_at": "2026-10-01T00:00:00Z", "archived": False,
                                         "license": {"spdx_id": "BSD-2-Clause"}})
    out = agent.github_repo("https://github.com/limine-bootloader/limine", http=_github(handler))
    assert out["stars"] == 2500 and out["last_push"] == "2026-10-01" and out["license"] == "BSD-2-Clause"
    assert out["readme_start"].startswith("# Limine")


def test_github_repo_reports_missing_repo_as_error_not_exception():
    out = agent.github_repo("nobody/nothing", http=_github(lambda req: httpx.Response(404)))
    assert "not found" in out["error"]


def test_github_repo_rejects_bare_names():
    assert "owner/name" in agent.github_repo("limine")["error"]
