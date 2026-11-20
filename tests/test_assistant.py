"""The assistant (app/assistant.py) and the MCP client (app/mcp_client.py), with a fake
Gemini client and fake MCP servers: no network, no spend."""
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app import assistant, mcp_client, quests
from app.config import get_profile, save_profile_fact
from app.models import Task


def _ix(steps=(), text="", id_="i1"):
    usage = SimpleNamespace(total_input_tokens=1000, total_output_tokens=100, total_thought_tokens=0,
                            total_cached_tokens=0, total_tool_use_tokens=0, input_tokens_by_modality=[])
    return SimpleNamespace(id=id_, steps=list(steps), usage=usage, output_text=text)


def _call(name, args, id_="c1"):
    return SimpleNamespace(type="function_call", name=name, arguments=args, id=id_)


class FakeGenAI:
    def __init__(self, responses):
        self.queue, self.requests = list(responses), []
        self.interactions = SimpleNamespace(create=self._create)

    def _create(self, **params):
        self.requests.append(params)
        return self.queue.pop(0)


@pytest.fixture(autouse=True)
def no_mcp(monkeypatch):
    monkeypatch.setattr(mcp_client, "tools", lambda lister=None: [])
    assistant._sessions.clear()


def test_answers_with_a_tool_and_chains_the_conversation(session):
    client = FakeGenAI([_ix([_call("remind_me", {"when": "2099-01-01T09:00", "text": "Call the dentist"})], id_="a"),
                        _ix(text="Done, I'll remind you **Jan 1 at 9am**.", id_="b"),
                        _ix(text="Sure.", id_="c")])
    reply = assistant.ask("42", "remind me to call the dentist on Jan 1 at 9", client=client)
    assert "remind you" in reply.text
    [t] = session.exec(select(Task)).all()
    assert t.kind == "reminder" and t.status == "scheduled" and "dentist" in t.title
    assert client.requests[1]["previous_interaction_id"] == "a"
    assistant.ask("42", "thanks", client=client)
    assert client.requests[2]["previous_interaction_id"] == "b"   # same conversation
    assistant.reset("42")
    client.queue.append(_ix(text="hi"))
    assistant.ask("42", "new topic", client=client)
    assert "previous_interaction_id" not in client.requests[3]


def test_calendar_proposal_becomes_a_button_not_an_event(session):
    client = FakeGenAI([_ix([_call("propose_calendar_event", {"title": "Dinner at Zahav", "start": "2099-03-04T19:00"})]),
                        _ix(text="Here you go.")])
    reply = assistant.ask("42", "put dinner at zahav on my calendar", client=client)
    [t] = session.exec(select(Task)).all()
    assert t.status == "proposed" and reply.buttons[0][0]["callback_data"] == f"approve:{t.id}"
    _, url = quests.approve(t.id)
    assert url.startswith("https://calendar.google.com/")


def test_profile_facts_are_kept_but_never_card_numbers(session):
    client = FakeGenAI([_ix([_call("update_profile", {"key": "Chase Ultimate Rewards points", "value": "142,000"})]),
                        _ix(text="Saved.")])
    assistant.ask("42", "I have 142k chase points", client=client)
    assert get_profile() == {"Chase Ultimate Rewards points": "142,000"}
    assert "Chase Ultimate Rewards points: 142,000" in assistant._context()
    with pytest.raises(ValueError):
        save_profile_fact("card", "4111 1111 1111 1111")
    with pytest.raises(ValueError):
        save_profile_fact("chase password", "hunter2")


def test_cap_reached_answers_without_calling_gemini(session, monkeypatch):
    from app import tracing
    monkeypatch.setattr(tracing, "spend_today", lambda s: 99.0)
    client = FakeGenAI([])
    assert "cap" in assistant.ask("42", "flights to tokyo", client=client).text and client.requests == []


def test_mcp_tools_are_allowlisted_and_schemas_cleaned(monkeypatch):
    monkeypatch.undo()   # use the real mcp_client.tools here (the autouse fixture stubs it)
    mcp_client._cache.update(at=0.0, tools=[])
    listed = [SimpleNamespace(name="get_flights", description="Award flights",
                              input_schema={"type": "object", "title": "x", "additionalProperties": False,
                                            "properties": {"flyFrom": {"type": "string", "title": "F"},
                                                           "adults": {"type": ["integer", "null"], "default": 1},
                                                           "cabin": {"enum": [None, "M", "C"], "type": ["string", "null"]}},
                                            "required": ["flyFrom", "nope"]}),
              SimpleNamespace(name="search_help_docs", description="d", input_schema={"type": "object"})]
    reloaded = mcp_client
    try:
        tools = reloaded.tools(lister=lambda url: listed if "seats" in url else [])
        assert [t["name"] for t in tools] == ["seats__get_flights"]   # help-docs tool not allowlisted; Kiwi not raw
        params = tools[0]["parameters"]
        assert params["properties"]["adults"] == {"type": "integer"}
        assert params["properties"]["cabin"]["enum"] == ["M", "C"]
        assert params["required"] == ["flyFrom"] and "additionalProperties" not in params
        seen = {}
        out = reloaded.call("kiwi__search_flight", {"flyFrom": "PHL"}, internal=True,
                            caller=lambda url, tool, args: seen.update(url=url, tool=tool) or "3 flights, $412 cheapest")
        assert seen == {"url": "https://mcp.kiwi.com", "tool": "search-flight"} and "412" in out["result"]
        assert "error" in reloaded.call("kiwi__feedback_to_devs", {"text": "x"}, internal=True)
        assert "error" in reloaded.call("evil__send_money", {})
    finally:
        reloaded._cache.update(at=0.0, tools=[])


def test_telegram_html_formatting():
    out = assistant.telegram_html("**$412** on <United> [book](https://kiwi.com/x)\n## Notes")
    assert "<b>$412</b>" in out and "&lt;United&gt;" in out and '<a href="https://kiwi.com/x">book</a>' in out
    assert "##" not in out


def test_telegram_answer_sends_reply_with_buttons(monkeypatch):
    from app import telegram_bot

    sent = []
    tg = SimpleNamespace(call=lambda *a, **k: None,
                         send=lambda chat, text, reply_to=None, buttons=None: sent.append((text, buttons)) or 1)
    telegram_bot.answer(42, "q", 5, tg, ask=lambda chat, text: assistant.Reply("**Yes**", [[{"text": "b"}]]))
    assert sent == [("<b>Yes</b>", [[{"text": "b"}]])]
    telegram_bot.answer(42, "q", 5, tg, ask=lambda chat, text: (_ for _ in ()).throw(RuntimeError("x")))
    assert "couldn't answer" in sent[-1][0]


def test_mcp_call_adds_defaults_and_retries(monkeypatch):
    monkeypatch.undo()
    calls = []

    def flaky(url, tool, args):
        calls.append(args)
        if len(calls) == 1:
            raise BaseExceptionGroup("tg", [ConnectionError("dropped")])
        return "ok"

    out = mcp_client.call("kiwi__search_flight", {"flyFrom": "PHL"}, caller=flaky, internal=True)
    assert out == {"result": "ok"} and calls[1]["currency"] == "USD" and len(calls) == 2
    out = mcp_client.call("kiwi__search_flight", {"currency": "EUR"}, caller=lambda u, t, a: a["currency"],
                          internal=True)
    assert out == {"result": "EUR"}   # an explicit choice wins
