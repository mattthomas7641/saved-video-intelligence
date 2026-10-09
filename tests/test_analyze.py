"""app/analyze.py: prompt building, cost math, and response parsing.

The live analyze_video() call is tested with a mocked Anthropic client -
no network access or API spend needed to verify the parsing/validation
logic is correct. This is the pattern to extend if more Claude-calling
code is added later.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import analyze
from app.config import ANALYSIS_MODEL


class FakeVideo:
    def __init__(self, **kw):
        self.author = kw.get("author", "someone")
        self.caption = kw.get("caption", "a caption")
        self.hashtags = kw.get("hashtags", "")
        self.transcript = kw.get("transcript", "")
        self.ocr_text = kw.get("ocr_text", "")


def _fake_tool_use_message(input_dict, input_tokens=1500, output_tokens=300):
    block = SimpleNamespace(type="tool_use", name="record_analysis", input=input_dict)
    return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens))


BASE_FIELDS = {
    "category": "Tech / AI / Coding", "summary": "A thing happens.", "tags": ["ai"],
    "worth_rewatching_score": 3, "worth_rewatching_reason": "fine", "has_promo_code": False,
    "has_dated_offer": False, "mentions_link_in_bio": False, "key_facts": [], "is_actionable": False,
}


def test_build_params_includes_model_and_forces_the_tool():
    params = analyze.build_params(FakeVideo())
    assert params["model"] == ANALYSIS_MODEL
    assert params["tool_choice"] == {"type": "tool", "name": "record_analysis"}
    assert params["tools"][0]["name"] == "record_analysis"


def test_build_params_includes_user_note_when_present():
    v = FakeVideo()
    v.user_note = "build this agent for me"
    params = analyze.build_params(v)
    assert "build this agent for me" in params["messages"][0]["content"]


def test_build_params_omits_note_block_when_absent():
    params = analyze.build_params(FakeVideo())  # FakeVideo has no user_note attribute at all
    assert "Note from the person who saved this" not in params["messages"][0]["content"]


def test_build_params_clips_very_long_text():
    long_transcript = "x" * (analyze.MAX_TEXT_CHARS + 500)
    params = analyze.build_params(FakeVideo(transcript=long_transcript))
    content = params["messages"][0]["content"]
    assert len(content) < len(long_transcript) + 1000  # clipped, not passed through whole


def test_parse_message_extracts_tool_input_and_usage():
    msg = _fake_tool_use_message({**BASE_FIELDS}, input_tokens=1000, output_tokens=200)
    result = analyze.parse_message(msg)
    assert result.category == "Tech / AI / Coding"
    assert result.input_tokens == 1000
    assert result.output_tokens == 200


def test_parse_message_raises_without_a_tool_use_block():
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text="oops")])
    with pytest.raises(RuntimeError):
        analyze.parse_message(msg)


def test_analysis_result_normalizes_actionability():
    """is_actionable should never be True without a usable action_type - a
    malformed/partial tool call shouldn't silently create a bogus Action."""
    result = analyze.AnalysisResult(**{**BASE_FIELDS, "is_actionable": True})  # no action_type given
    assert result.is_actionable is False
    assert result.action_type is None

    result2 = analyze.AnalysisResult(**{**BASE_FIELDS, "is_actionable": True, "action_type": "skill",
                                         "action_brief": "do the thing"})
    assert result2.is_actionable is True
    assert result2.action_type == "skill"


def test_analysis_result_folds_off_list_category():
    result = analyze.AnalysisResult(**{**BASE_FIELDS, "category": "Sports / Fantasy Football"})
    assert result.category == "Sports / Gaming"


@pytest.mark.parametrize("input_tokens,output_tokens,batch,expected", [
    (1_000_000, 0, False, 1.0),   # 1M input tokens at $1/MTok (haiku default)
    (0, 1_000_000, False, 5.0),   # 1M output tokens at $5/MTok
    (1_000_000, 1_000_000, True, 3.0),  # batch = half price of (1 + 5)
])
def test_cost_usd_haiku_pricing(input_tokens, output_tokens, batch, expected):
    assert analyze.cost_usd(input_tokens, output_tokens, batch=batch) == pytest.approx(expected)


def test_analyze_video_raises_fatal_error_without_api_key(monkeypatch):
    monkeypatch.setattr(analyze, "get_api_key", lambda: "")
    with pytest.raises(analyze.FatalAnalysisError):
        analyze.analyze_video(FakeVideo())


def test_analyze_video_happy_path_with_mocked_client(monkeypatch):
    monkeypatch.setattr(analyze, "get_api_key", lambda: "sk-ant-fake")
    fake_response = _fake_tool_use_message({**BASE_FIELDS, "category": "Recipe / Cooking"})
    with patch.object(analyze.Anthropic, "__init__", lambda self, **kw: None), \
         patch.object(analyze.Anthropic, "messages", create=True) as mock_messages:
        mock_messages.create.return_value = fake_response
        result = analyze.analyze_video(FakeVideo())
    assert result.category == "Recipe / Cooking"


def test_analyze_video_treats_credit_error_as_fatal(monkeypatch):
    import anthropic as anthropic_pkg

    monkeypatch.setattr(analyze, "get_api_key", lambda: "sk-ant-fake")

    def _raise(*a, **kw):
        fake_request = SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages")
        fake_response = SimpleNamespace(status_code=400, headers={}, request=fake_request)
        raise anthropic_pkg.BadRequestError("Your credit balance is too low", response=fake_response, body=None)

    with patch.object(analyze.Anthropic, "__init__", lambda self, **kw: None), \
         patch.object(analyze.Anthropic, "messages", create=True) as mock_messages:
        mock_messages.create.side_effect = _raise
        with pytest.raises(analyze.FatalAnalysisError):
            analyze.analyze_video(FakeVideo())


def test_analyze_video_routes_through_gateway_when_configured(monkeypatch):
    monkeypatch.setattr(analyze, "get_api_key", lambda: "sk-ant-fake")
    monkeypatch.setattr(analyze, "GATEWAY_URL", "http://127.0.0.1:8000")
    inits = []
    fake_response = _fake_tool_use_message(BASE_FIELDS)
    with patch.object(analyze.Anthropic, "__init__", lambda self, **kw: inits.append(kw)), \
         patch.object(analyze.Anthropic, "messages", create=True) as mock_messages:
        mock_messages.create.return_value = fake_response
        analyze.analyze_video(FakeVideo())
    assert inits == [{"api_key": "sk-ant-fake", "base_url": "http://127.0.0.1:8000",
                      "default_headers": {"x-task": "video-tagging"}}]


def test_analyze_video_falls_back_to_anthropic_when_gateway_is_down(monkeypatch):
    import anthropic as anthropic_pkg
    import httpx

    monkeypatch.setattr(analyze, "get_api_key", lambda: "sk-ant-fake")
    monkeypatch.setattr(analyze, "GATEWAY_URL", "http://127.0.0.1:8000")
    inits = []
    fake_response = _fake_tool_use_message(BASE_FIELDS)
    connection_error = anthropic_pkg.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:8000"))
    with patch.object(analyze.Anthropic, "__init__", lambda self, **kw: inits.append(kw)), \
         patch.object(analyze.Anthropic, "messages", create=True) as mock_messages:
        mock_messages.create.side_effect = [connection_error, fake_response]
        result = analyze.analyze_video(FakeVideo())
    assert result.category == BASE_FIELDS["category"]
    assert inits[-1] == {"api_key": "sk-ant-fake"}

