"""Send anything: Instagram/YouTube links, screenshots, plain text, and the
tech-vs-everyday split (only tech is judged against your projects)."""
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import select

from app import agent, gemini_client, intake, ntfy_intake, pipeline, prefetch, projects
from app.analyze import AnalysisResult
from app.config import new_ntfy_setup
from app.models import Status, Video

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(ntfy_intake, "notify", lambda *a, **k: True)
    monkeypatch.setattr(intake, "canonical_tiktok_url", lambda url: url)
    monkeypatch.setattr("app.ocr.extract_image_text", lambda path: "Joe's Pizza $3 slice")


@pytest.mark.parametrize("url,media", [
    ("https://www.instagram.com/reel/abc123/", True),
    ("https://www.youtube.com/shorts/xyz", True),
    ("https://youtu.be/xyz", True),
    ("https://x.com/someone/status/123", True),
    ("https://vm.tiktok.com/ZM1/", True),
    ("https://github.com/a/b", False),
    ("https://example.com/best-pizza", False),
])
def test_media_links_are_downloaded_and_pages_are_read(url, media, session):
    vid = intake.ingest_shared_link(url, "")
    video = session.get(Video, vid)
    assert (video.status == Status.PENDING) is media


def test_screenshot_is_saved_ocrd_and_shown_to_the_model(session):
    vid = intake.ingest_shared_image(PNG, "shot.png", "is this worth it?")
    video = session.get(Video, vid)
    assert video.tiktok_url.startswith("image:") and video.status == Status.TRANSCRIBED
    assert video.ocr_text == "Joe's Pizza $3 slice" and video.user_note == "is this worth it?"
    media_type, data = agent.image_b64(video)
    assert media_type == "image/png" and data
    media = gemini_client._media(video)
    assert media["type"] == "image" and media["mime_type"] == "image/png"


def test_plain_text_becomes_an_item(session):
    vid = intake.ingest_shared_text("Try the cacio e pepe at Dante's on 6th")
    video = session.get(Video, vid)
    assert video.tiktok_url.startswith("text:") and "cacio e pepe" in video.caption


def test_ntfy_attachment_from_our_server_only(session):
    s = new_ntfy_setup()
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=PNG)))
    ok = {"url": f"{s['server']}/file/abc.jpg", "name": "shot.jpg", "type": "image/jpeg", "size": len(PNG)}
    raw = json.dumps({"v": 1, "token": s["token"]})
    assert ntfy_intake.handle_message(raw, submit=lambda *a, **k: True, attachment=ok, http=http) == "queued"
    assert session.exec(select(Video)).one().tiktok_url.startswith("image:")
    evil = {**ok, "url": "https://evil.example/file.jpg"}
    assert ntfy_intake.handle_message(raw, submit=lambda *a, **k: True, attachment=evil, http=http) == "ignored"
    big = {**ok, "size": 50_000_000}
    assert ntfy_intake.handle_message(raw, submit=lambda *a, **k: True, attachment=big, http=http) == "ignored"


def test_ntfy_text_without_link(session):
    s = new_ntfy_setup()
    out = ntfy_intake.handle_message(json.dumps({"v": 1, "token": s["token"], "url": "new ramen place in fishtown"}),
                                     submit=lambda *a, **k: True)
    assert out == "queued" and session.exec(select(Video)).one().tiktok_url.startswith("text:")


@pytest.mark.parametrize("action_type,category,tech", [
    ("repo", "Other", True), ("project", "Business / Side Hustle", True), ("skill", "Tech / AI / Coding", True),
    ("skill", "Recipe / Cooking", False), ("place", "Recipe / Cooking", False), ("research", "News / Commentary", False),
    ("research", "Tech / AI / Coding", True),
])
def test_is_tech(action_type, category, tech):
    assert agent.is_tech(action_type, category) is tech


def test_general_review_has_no_project_access():
    names = [t["name"] for t in agent.client_tools(False)]
    assert "read_project_file" not in names
    findings_props = [t for t in agent.client_tools(False) if t["name"] == "record_findings"][0]["input_schema"]["properties"]
    assert "project_fit" not in findings_props and "place" in findings_props
    assert "Don't relate it to any software projects" in agent.system_prompt(False)
    assert "Project fit" in agent.system_prompt(True) and "Project fit" not in agent.system_prompt(False)
    assert agent.run_client_tool("read_project_file", {"project": "x", "path": "README.md"}, {}, tech=False)["error"]


def _analysis(**over):
    base = {"category": "Recipe / Cooking", "summary": "Pizza spot.", "tags": [], "worth_rewatching_score": 4,
            "worth_rewatching_reason": "x", "has_promo_code": False, "has_dated_offer": False,
            "mentions_link_in_bio": False, "key_facts": [], "is_actionable": True, "action_type": "place",
            "action_brief": "Joe's Pizza"}
    r = AnalysisResult(**{**base, **over})
    r.cost_usd = 0.002
    return r


def _run_lane(monkeypatch, session, analysis):
    seen = {}
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: analysis)
    monkeypatch.setattr(prefetch, "gather", lambda v: {"repos": [], "pages": []})
    monkeypatch.setattr(projects, "cards", lambda s: [projects.ProjectCard("gateway", "active", "LLM router")])
    monkeypatch.setattr(agent, "run", lambda *a, **k: seen.update(k) or agent.Findings(
        verdict="maybe", headline="h", telegram_text="t", report_markdown="r"))
    vid = intake.ingest_shared_text("Joe's Pizza has a $3 slice")
    pipeline.process_shared_video(session, session.get(Video, vid))
    return seen


def test_everyday_content_skips_projects(monkeypatch, session):
    seen = _run_lane(monkeypatch, session, _analysis())
    assert seen["tech"] is False and seen["context"].projects == ""


def test_tech_content_gets_projects(monkeypatch, session):
    seen = _run_lane(monkeypatch, session, _analysis(category="Tech / AI / Coding", action_type="repo"))
    assert seen["tech"] is True and "gateway" in seen["context"].projects


def test_blocked_download_falls_back_to_link(monkeypatch, session):
    vid = intake.ingest_shared_link("https://www.instagram.com/reel/abc/", "")
    video = session.get(Video, vid)

    def blocked(session_, v, should_stop=None, transcribe=True):
        v.status = Status.ERROR
        v.error_message = "login required"
    monkeypatch.setattr(pipeline, "collect_video", blocked)
    seen = {}
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: seen.update(caption=v.caption) or _analysis())
    monkeypatch.setattr(prefetch, "gather", lambda v: {"repos": [], "pages": []})
    monkeypatch.setattr(agent, "run", lambda *a, **k: agent.Findings(verdict="maybe", headline="h",
                                                                      telegram_text="t", report_markdown="r"))
    action = pipeline.process_shared_video(session, video)
    assert action.status.value == "drafted" and seen["caption"] == "https://www.instagram.com/reel/abc/"


def test_telegram_text_and_short_text(monkeypatch, session):
    from app import telegram_bot as tb
    from app.config import save_telegram_settings
    save_telegram_settings(bot_token="1:a", chat_id=42)
    sent, submitted = [], []
    tg = SimpleNamespace(send=lambda chat, text, reply_to=None: sent.append(text) or 9)
    upd = lambda text: {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 42}, "text": text}}  # noqa: E731
    tb.handle_update(upd("best tacos in south philly"), tg, submit=lambda *a: submitted.append(a))
    tb.handle_update(upd("hi"), tg, submit=lambda *a: submitted.append(a))
    assert len(submitted) == 1 and session.exec(select(Video)).one().tiktok_url.startswith("text:")
