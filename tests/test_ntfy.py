"""app/ntfy_intake.py: the Send to Sidequest doorway, with ntfy and the lane faked."""
import json

import httpx
import pytest
from sqlmodel import select

from app import intake, ntfy_intake
from app.config import get_ntfy_settings, new_ntfy_setup
from app.models import Action, ActionStatus, ActionType, Video


@pytest.fixture
def channel(monkeypatch):
    s = new_ntfy_setup()
    sent = []
    monkeypatch.setattr(ntfy_intake, "notify", lambda *a, **k: sent.append((a, k)) or True)
    monkeypatch.setattr(intake, "canonical_tiktok_url", lambda url: url)
    return s, sent


def _msg(**body):
    return json.dumps({"v": 1, **body})


def test_right_token_ingests_and_queues(channel, session):
    s, sent = channel
    queued = []
    out = ntfy_intake.handle_message(_msg(token=s["token"], url="https://www.tiktok.com/@a/video/42"),
                                     submit=lambda vid, deep=False: queued.append(vid))
    video = session.exec(select(Video)).one()
    assert out == "queued" and queued == [video.id] and video.source == "telegram"
    assert sent and sent[0][0][0].startswith("📥")


@pytest.mark.parametrize("raw", [_msg(token="wrong", url="https://x.com"), _msg(url="https://x.com"),
                                 "not json", json.dumps(["list"]), ""])
def test_bad_messages_are_ignored(channel, session, raw):
    _, sent = channel
    assert ntfy_intake.handle_message(raw, submit=lambda *a, **k: pytest.fail("queued")) == "ignored"
    assert session.exec(select(Video)).all() == [] and sent == []


def test_share_text_with_note(channel, session):
    s, _ = channel
    ntfy_intake.handle_message(_msg(token=s["token"], url="worth it for my gateway? https://www.tiktok.com/@a/video/7"),
                               submit=lambda *a, **k: True)
    assert session.exec(select(Video)).one().user_note == "worth it for my gateway?"


def test_deep_action_reruns(channel, monkeypatch):
    s, _ = channel
    calls = []
    monkeypatch.setattr(intake, "rerun", lambda action_id, deep=False: calls.append((action_id, deep)) or True)
    assert ntfy_intake.handle_message(_msg(token=s["token"], deep=12)) == "deep" and calls == [(12, True)]


def test_result_notification_fits_and_links(channel, session):
    s, _ = channel
    v = Video(tiktok_url="https://www.tiktok.com/@a/video/1")
    session.add(v)
    session.commit()
    report = {"headline": "Use it", "telegram_text": "Short answer. " * 300, "ideas": ["one"],
              "project_fit": [{"project": "gateway", "fit": "worth_it", "effort": "S",
                               "architecture_impact": "drops a dependency"}]}
    a = Action(id=5, video_id=v.id, action_type=ActionType.SKILL, status=ActionStatus.DRAFTED,
               verdict="worth_it", result=json.dumps(report))
    n = ntfy_intake.result_notification(a, v)
    assert "Do it" in n["title"] and "Use it" in n["title"]
    assert "Fits: gateway (worth it, S): drops a dependency" in n["message"]
    assert n["click"] == v.tiktok_url  # PUBLIC_BASE_URL is localhost in tests
    deep = json.loads(n["actions"][0]["body"])
    assert deep == {"v": 1, "token": s["token"], "deep": 5}
    assert len(ntfy_intake._clip(n["message"]).encode()) <= ntfy_intake.MAX_BYTES + 3


def test_notify_posts_json_to_results_topic():
    new_ntfy_setup()
    seen = []
    http = httpx.Client(transport=httpx.MockTransport(lambda req: seen.append(json.loads(req.content)) or httpx.Response(200)))
    assert ntfy_intake.notify("✅ Title", "body", click="https://x", http=http)
    assert seen[0]["topic"] == get_ntfy_settings()["results_topic"] and seen[0]["title"] == "✅ Title"
    assert seen[0]["markdown"] is True


def test_not_ready_without_setup():
    assert ntfy_intake.ready() is False
    new_ntfy_setup()
    assert ntfy_intake.ready() is True
