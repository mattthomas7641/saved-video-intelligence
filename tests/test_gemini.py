"""app/gemini_client.py: watching, research loop and cost, with a fake
Interactions API client (no network, no spend)."""
from types import SimpleNamespace

import pytest

from app import agent, gemini_client, pipeline
from app.analyze import FatalAnalysisError
from app.models import Status, Video


class V:
    tiktok_url = "https://www.tiktok.com/@dev/video/1"
    author = "dev"
    caption = "this batching trick"
    hashtags = ""
    summary = "Dynamic batching."
    transcript = ""
    ocr_text = ""
    user_note = None
    local_video_path = None
    thumbnail_path = None


def _usage(i=8000, audio=1900, o=400, thought=300, cached=0):
    return SimpleNamespace(
        total_input_tokens=i, total_output_tokens=o, total_thought_tokens=thought, total_cached_tokens=cached,
        total_tool_use_tokens=0,
        input_tokens_by_modality=[SimpleNamespace(modality="audio", tokens=audio),
                                  SimpleNamespace(modality="video", tokens=i - audio)])


def _ix(steps, id_="i1", **u):
    return SimpleNamespace(id=id_, steps=steps, usage=_usage(**u), output_text="")


def _call(name, args, id_="c1"):
    return SimpleNamespace(type="function_call", name=name, arguments=args, id=id_)


class FakeGenAI:
    def __init__(self, responses):
        self.queue, self.requests = list(responses), []
        self.interactions = SimpleNamespace(create=self._create)

    def _create(self, **params):
        self.requests.append(params)
        return self.queue.pop(0)


ANALYSIS = {"category": "Tech / AI / Coding", "summary": "s", "tags": [], "worth_rewatching_score": 4,
            "worth_rewatching_reason": "r", "has_promo_code": False, "has_dated_offer": False,
            "mentions_link_in_bio": False, "key_facts": [], "is_actionable": True, "action_type": "skill",
            "action_brief": "batching", "transcript": "so you batch requests", "on_screen_text": "vllm --spec",
            "visual_notes": "terminal with a benchmark"}
FINDINGS = {"verdict": "worth_it", "headline": "h", "telegram_text": "t", "report_markdown": "r"}


def test_cost_prices_audio_separately():
    ix = _ix([], i=8000, audio=2000, o=400, thought=600)
    # gemini-3.8-flash: 6000 video/text * 0.75 + 2000 audio * 3.00 + 1000 out * 3.75, per M
    assert gemini_client.interaction_cost(ix, "gemini-3.8-flash") == pytest.approx(
        (6000 * 0.75 + 2000 * 3.0 + 1000 * 3.75) / 1e6)


def test_analyze_watches_the_video_and_keeps_what_it_heard(tmp_path):
    clip = tmp_path / "1.mp4"
    clip.write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * 100)
    v = V()
    v.local_video_path = str(clip)
    client = FakeGenAI([_ix([_call("record_analysis", ANALYSIS)])])
    result = gemini_client.analyze_video(v, client=client)
    req = client.requests[0]
    video_item = req["input"][1]
    assert video_item["type"] == "video" and video_item["resolution"] == "low" and video_item["mime_type"] == "video/mp4"
    assert req["generation_config"]["tool_choice"] == "any"
    assert result.action_type == "skill" and 0 < result.cost_usd < 0.02
    assert v.transcript == "so you batch requests" and "vllm --spec" in v.ocr_text and "terminal" in v.ocr_text


def test_research_tools_chain_and_findings(monkeypatch):
    monkeypatch.setattr(agent, "read_project_file", lambda p, path: {"content": "def batch(): ..."})
    client = FakeGenAI([
        _ix([SimpleNamespace(type="google_search_call"),
             _call("read_project_file", {"project": "gateway", "path": "batcher.py"})]),
        _ix([_call("record_findings", FINDINGS, "c2")], id_="i2"),
    ])
    f = gemini_client.research(V(), "skill", "b", 1.0, client=client, web=True, tech=True)
    assert f.verdict == "worth_it"
    first, second = client.requests
    types = [t.get("type") for t in first["tools"]]
    assert types[0] == "google_search" and "google_maps" not in types
    assert [t["name"] for t in first["tools"] if t["type"] == "function"] == [
        "github_repo", "read_project_file", "record_findings"]
    assert second["previous_interaction_id"] == "i1"
    res = second["input"][0]
    assert res["type"] == "function_result" and res["call_id"] == "c1" and "def batch" in res["result"][0]["text"]


def test_places_get_maps_and_everyday_gets_no_projects():
    client = FakeGenAI([_ix([_call("record_findings", FINDINGS)])])
    gemini_client.research(V(), "place", None, 1.0, client=client, web=True, tech=False)
    tools = client.requests[0]["tools"]
    assert {"type": "google_maps"} in tools
    assert "read_project_file" not in [t.get("name") for t in tools]
    assert "Don't relate it to any software projects" in client.requests[0]["system_instruction"]


def test_no_web_means_no_builtin_tools():
    client = FakeGenAI([_ix([_call("record_findings", FINDINGS)])])
    gemini_client.research(V(), "repo", None, 1.0, client=client, web=False)
    assert all(t["type"] == "function" for t in client.requests[0]["tools"])


def test_nudge_then_fail_and_budget():
    msg = SimpleNamespace(type="model_output")
    client = FakeGenAI([_ix([msg]), _ix([msg], id_="i2")])
    with pytest.raises(agent.AgentError):
        gemini_client.research(V(), "other", None, 1.0, client=client, web=False)
    assert client.requests[1]["generation_config"]["tool_choice"] == "any"
    pricey = FakeGenAI([_ix([_call("github_repo", {"repo": "a/b"})], i=3_000_000, audio=0)])
    with pytest.raises(agent.CapReached):
        gemini_client.research(V(), "repo", None, 1.0, client=pricey, web=False)


def test_missing_key_is_fatal():
    with pytest.raises(FatalAnalysisError):
        gemini_client.analyze_video(V())


def test_research_plan():
    assert pipeline.research_plan("gemini", "repo") == (True, False)      # free search: always allowed
    assert pipeline.research_plan("xai", "repo") == (False, False)        # paid search: only where needed
    assert pipeline.research_plan("xai", "place") == (True, False)
    assert pipeline.research_plan("xai", "research", deep=True) == (True, True)


def test_gemini_lane_downloads_without_whisper_and_deletes_the_file(monkeypatch, session, tmp_path):
    from app import download as download_mod
    from app import intake, prefetch, transcribe

    clip = tmp_path / "v.mp4"
    monkeypatch.setattr(intake, "canonical_tiktok_url", lambda url: url)
    monkeypatch.setattr(transcribe, "transcribe", lambda p: pytest.fail("Whisper ran"))
    monkeypatch.setattr("app.pipeline.throttle.wait", lambda should_stop: None)

    def fake_download(url, video_id):
        clip.write_bytes(b"\0" * 64)
        return download_mod.DownloadResult(str(clip), None, "dev", "cap", "", None, 30.0)
    monkeypatch.setattr(download_mod, "download_video", fake_download)
    seen = {}

    def watch(video):
        seen["path"] = video.local_video_path
        from app.analyze import AnalysisResult
        r = AnalysisResult(**{k: v for k, v in ANALYSIS.items() if k not in ("transcript", "on_screen_text", "visual_notes")})
        r.cost_usd = 0.01
        return r
    monkeypatch.setattr(gemini_client, "analyze_video", watch)
    monkeypatch.setattr(prefetch, "gather", lambda v: {"repos": [], "pages": []})
    monkeypatch.setattr(agent, "run", lambda *a, **k: agent.Findings(verdict="maybe", headline="h",
                                                                      telegram_text="t", report_markdown="r"))
    vid = intake.ingest_shared_link("https://www.tiktok.com/@dev/video/5", "")
    video = session.get(Video, vid)
    action = pipeline.process_shared_video(session, video)
    assert seen["path"] == str(clip) and action.status.value == "drafted"
    assert not clip.exists() and video.local_video_path is None and video.status == Status.DONE


class Err429(Exception):
    status_code = 429


class FlakyGenAI(FakeGenAI):
    """Raises a 429 for any request matching `fail_if`, otherwise replays responses."""
    def __init__(self, responses, fail_if):
        super().__init__(responses)
        self.fail_if = fail_if

    def _create(self, **params):
        self.requests.append(params)
        if self.fail_if(params):
            raise Err429("You exceeded your current quota")
        return self.queue.pop(0)


def test_search_quota_falls_back_to_no_search(monkeypatch):
    monkeypatch.setattr(gemini_client, "BACKOFF_S", 0)
    has_search = lambda p: any(t.get("type") == "google_search" for t in p.get("tools", []))  # noqa: E731
    client = FlakyGenAI([_ix([_call("record_findings", FINDINGS)])], has_search)
    f = gemini_client.research(V(), "place", None, 1.0, client=client, web=True, tech=False)
    assert len(client.requests) == 2 and not has_search(client.requests[1])  # no slow retries before falling back
    assert f.report_markdown.startswith("> Researched without web search")


def test_rate_limit_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(gemini_client, "BACKOFF_S", 0)
    calls = {"n": 0}

    def flaky(params):
        calls["n"] += 1
        return calls["n"] == 1  # first call only
    client = FlakyGenAI([_ix([_call("record_findings", FINDINGS)])], flaky)
    assert gemini_client.research(V(), "repo", None, 1.0, client=client, web=False).verdict == "worth_it"


def test_persistent_rate_limit_becomes_a_readable_failure(monkeypatch, session):
    from app import intake, prefetch
    from app.analyze import AnalysisResult
    monkeypatch.setattr(gemini_client, "BACKOFF_S", 0)
    monkeypatch.setattr(prefetch, "gather", lambda v: {"repos": [], "pages": []})
    r = AnalysisResult(**{k: v for k, v in ANALYSIS.items() if k not in ("transcript", "on_screen_text", "visual_notes")})
    r.cost_usd = 0.01
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: r)

    def always_429(*a, **k):
        raise gemini_client.Throttled("429")
    monkeypatch.setattr(gemini_client, "research", always_429)
    vid = intake.ingest_shared_text("some text about a new AI model release")
    action = pipeline.process_shared_video(session, session.get(Video, vid))
    assert action.status.value == "failed" and "rate limiting" in action.error_message


def test_network_drop_is_retried(monkeypatch):
    import ssl
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Flaky(FakeGenAI):
        def _create(self, **params):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("wrapped") from ssl.SSLEOFError("EOF occurred in violation of protocol")
            return super()._create(**params)
    client = Flaky([_ix([_call("record_findings", FINDINGS)])])
    assert gemini_client.research(V(), "repo", None, 1.0, client=client, web=False).verdict == "worth_it"
    assert calls["n"] == 2
