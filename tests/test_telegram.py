"""app/telegram_bot.py: link parsing, pairing, chat filtering, commands, ingest.

The Bot API is a recording fake; the research lane is replaced by a list, so
these tests check routing decisions, not the pipeline itself.
"""
import pytest
from sqlmodel import select

from app import telegram_bot as tb
from app.config import get_action_settings, get_telegram_settings, new_pairing_code, save_telegram_settings
from app.models import Status, Video


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, reply_to=None):
        self.sent.append((chat_id, text))
        return 99


def _update(chat_id, text, message_id=5):
    return {"update_id": 1, "message": {"message_id": message_id, "chat": {"id": chat_id}, "text": text}}


@pytest.fixture
def paired():
    save_telegram_settings(bot_token="123:abc", chat_id=42, pairing_code="")
    return 42


@pytest.fixture(autouse=True)
def no_redirect_resolution(monkeypatch):
    monkeypatch.setattr(tb, "canonical_tiktok_url", lambda url: url)


@pytest.mark.parametrize("text,url,note", [
    ("https://vm.tiktok.com/ZMabc/", "https://vm.tiktok.com/ZMabc/", ""),
    ("is this good for my OS? https://www.tiktok.com/@a/video/1", "https://www.tiktok.com/@a/video/1",
     "is this good for my OS?"),
    ("Check out this video on TikTok! https://vt.tiktok.com/xyz/", "https://vt.tiktok.com/xyz/", ""),
    ("no link here", None, "no link here"),
])
def test_extract_link(text, url, note):
    assert tb.extract_link(text) == (url, note)


def test_is_tiktok():
    assert tb.is_tiktok("https://vm.tiktok.com/ZM/")
    assert tb.is_tiktok("https://www.tiktok.com/@a/video/1")
    assert not tb.is_tiktok("https://github.com/a/b")
    assert not tb.is_tiktok("https://nottiktok.com.evil.io/")


def test_pairing_with_the_right_code_saves_the_chat():
    save_telegram_settings(bot_token="123:abc")
    code = new_pairing_code()
    tg = FakeTelegram()
    tb.handle_update(_update(7, f"/start {code.lower()}"), tg, submit=lambda *a: None)
    assert get_telegram_settings()["chat_id"] == 7
    assert get_telegram_settings()["pairing_code"] == ""
    assert "Paired" in tg.sent[0][1]


def test_pairing_with_a_wrong_code_does_nothing():
    save_telegram_settings(bot_token="123:abc")
    new_pairing_code()
    tb.handle_update(_update(7, "/start WRONG"), FakeTelegram(), submit=lambda *a: None)
    assert get_telegram_settings()["chat_id"] is None


def test_messages_from_other_chats_are_ignored(paired):
    tg, submitted = FakeTelegram(), []
    tb.handle_update(_update(1000, "https://vm.tiktok.com/ZMabc/"), tg, submit=lambda *a: submitted.append(a))
    assert tg.sent == [] and submitted == []


def test_shared_link_is_ingested_and_submitted(paired, session):
    tg, submitted = FakeTelegram(), []
    tb.handle_update(_update(paired, "useful? https://www.tiktok.com/@a/video/123"), tg,
                     submit=lambda *a: submitted.append(a))
    video = session.exec(select(Video)).one()
    assert video.source == "telegram" and video.user_note == "useful?" and video.status == Status.PENDING
    assert submitted == [(video.id, paired, 99)]


def test_resharing_a_known_video_reuses_its_row(paired, session):
    session.add(Video(tiktok_url="https://www.tiktok.com/@a/video/123", status=Status.DONE))
    session.commit()
    tb.handle_update(_update(paired, "https://www.tiktok.com/@a/video/123"), FakeTelegram(), submit=lambda *a: None)
    rows = session.exec(select(Video)).all()
    session.refresh(rows[0])
    assert len(rows) == 1 and rows[0].source == "telegram" and rows[0].status == Status.DONE


def test_non_tiktok_link_skips_download(paired, session):
    tb.handle_update(_update(paired, "https://github.com/limine-bootloader/limine"), FakeTelegram(),
                     submit=lambda *a: None)
    video = session.exec(select(Video)).one()
    assert video.status == Status.TRANSCRIBED and video.caption == "https://github.com/limine-bootloader/limine"


def test_pause_and_resume_commands(paired):
    tb.handle_update(_update(paired, "/pause"), FakeTelegram())
    assert get_action_settings()["agent_paused"] is True
    tb.handle_update(_update(paired, "/resume"), FakeTelegram())
    assert get_action_settings()["agent_paused"] is False


def test_status_command_reports_spend(paired):
    tg = FakeTelegram()
    tb.handle_update(_update(paired, "/status"), tg)
    assert "$0.00" in tg.sent[0][1]


def test_plain_text_without_link_gets_a_hint(paired):
    tg, submitted = FakeTelegram(), []
    tb.handle_update(_update(paired, "hello"), tg, submit=lambda *a: submitted.append(a))
    assert submitted == [] and "link" in tg.sent[0][1]
