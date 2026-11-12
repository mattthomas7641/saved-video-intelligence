"""Every step of share -> research can fail; each failure must end in a visible,
retryable state (never an item spinning forever, never a 500), tell the owner through
whichever doorway is set up, keep background threads alive and leave no files behind.
Everything external (Gemini, Telegram, ntfy, yt-dlp) is faked."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import agent, download, gemini_client, intake, ntfy_intake, pipeline, prefetch, projects
from app import telegram_bot as tb
from app.analyze import AnalysisResult, FatalAnalysisError
from app.config import SECRETS_PATH, VIDEOS_DIR, get_agent_settings, new_ntfy_setup, save_telegram_settings
from app.main import app
from app.models import Action, ActionStatus, ActionType, Status, Video

ANALYSIS = {"category": "Tech / AI / Coding", "summary": "Batching trick.", "tags": [], "worth_rewatching_score": 4,
            "worth_rewatching_reason": "x", "has_promo_code": False, "has_dated_offer": False,
            "mentions_link_in_bio": False, "key_facts": [], "is_actionable": True, "action_type": "skill",
            "action_brief": "batching"}
GOOD = agent.Findings(verdict="maybe", headline="h", telegram_text="t", report_markdown="r")


def _analysis(**over):
    r = AnalysisResult(**{**ANALYSIS, **over})
    r.cost_usd = 0.002
    return r


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(ntfy_intake, "notify", lambda *a, **k: True)
    monkeypatch.setattr(intake, "canonical_tiktok_url", lambda url: url)
    monkeypatch.setattr(prefetch, "gather", lambda v: {"repos": [], "pages": []})
    monkeypatch.setattr(projects, "cards", lambda s: [])


@pytest.fixture
def shared(session, tmp_path):
    """A shared TikTok, downloaded and waiting for Gemini to watch it."""
    vid = intake.ingest_shared_link("https://www.tiktok.com/@a/video/1", "")
    v = session.get(Video, vid)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    clip = VIDEOS_DIR / f"{vid}.mp4"
    clip.write_bytes(b"\0" * 64)
    v.status, v.local_video_path = Status.TRANSCRIBED, str(clip)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


class Recorder(intake.Reporter):
    def __init__(self):
        self.calls = []

    def progress(self, text):
        self.calls.append(("progress", text))

    def finished(self, action, video):
        self.calls.append(("finished", action.status.value))

    def failed(self, text):
        self.calls.append(("failed", text))


def _action(session, video_id):
    session.expire_all()
    return session.exec(select(Action).where(Action.video_id == video_id)).one()


# ---------------- analysis failures ----------------

def test_analysis_crash_marks_card_failed_and_frees_the_video_for_retry(monkeypatch, session, shared):
    def boom(video):
        raise RuntimeError("Gemini did not return an analysis.")
    monkeypatch.setattr(gemini_client, "analyze_video", boom)
    rep = Recorder()
    intake._process(shared.id, rep, False)
    a = _action(session, shared.id)
    assert a.status == ActionStatus.FAILED and "Couldn't analyze this" in a.error_message
    assert "Gemini did not" not in a.error_message  # internals stay in the log
    assert rep.calls[-1][0] == "failed" and intake.in_flight() == 0
    session.refresh(shared)
    assert shared.status == Status.PENDING and shared.local_video_path is None  # Retry re-downloads
    assert not list(VIDEOS_DIR.glob(f"{shared.id}.*"))  # nothing left behind


def test_no_key_during_analysis_needs_input_not_a_spinner(monkeypatch, session, shared):
    def no_key(video):
        raise FatalAnalysisError("No Gemini API key. Add one in Settings.")
    monkeypatch.setattr(gemini_client, "analyze_video", no_key)
    intake._process(shared.id, Recorder(), False)
    a = _action(session, shared.id)
    assert a.status == ActionStatus.NEEDS_INPUT and "No Gemini API key" in a.error_message and "Retry" in a.error_message
    assert not list(VIDEOS_DIR.glob(f"{shared.id}.*"))


def test_unknown_action_type_becomes_other_instead_of_crashing(monkeypatch, session, shared):
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: _analysis(action_type="shopping"))
    monkeypatch.setattr(agent, "run", lambda *a, **k: GOOD)
    action = pipeline.process_shared_video(session, shared)
    assert action.status == ActionStatus.DRAFTED and action.action_type == ActionType.OTHER


def test_retry_after_research_failure_does_not_pay_for_analysis_again(monkeypatch, session, shared):
    analyses = []
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: analyses.append(1) or _analysis(is_actionable=False))
    runs = iter([RuntimeError("socket timed out"), GOOD])

    def run(*a, **k):
        r = next(runs)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(agent, "run", run)
    first = pipeline.process_shared_video(session, shared)
    assert first.status == ActionStatus.FAILED and "connection dropped" in first.error_message
    second = pipeline.process_shared_video(session, shared)
    assert second.status == ActionStatus.DRAFTED and len(analyses) == 1


def test_fatal_error_during_research_needs_input(monkeypatch, session, shared):
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: _analysis())

    def out_of_credit(*a, **k):
        raise FatalAnalysisError("Out of credit.")
    monkeypatch.setattr(agent, "run", out_of_credit)
    rep = Recorder()
    intake._process(shared.id, rep, False)
    a = _action(session, shared.id)
    assert a.status == ActionStatus.NEEDS_INPUT and a.error_message.startswith("Out of credit.")
    assert ("failed", "Out of credit.") in rep.calls


def test_prefetch_failure_does_not_fail_the_research(monkeypatch, session, shared):
    def dns_down(v):
        raise OSError("name resolution failed")
    monkeypatch.setattr(prefetch, "gather", dns_down)
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: _analysis())
    monkeypatch.setattr(agent, "run", lambda *a, **k: GOOD)
    assert pipeline.process_shared_video(session, shared).status == ActionStatus.DRAFTED


def test_leftover_file_after_restart_is_removed_before_research(monkeypatch, session, shared):
    shared.status, shared.summary = Status.DONE, "s"
    session.add(shared)
    session.commit()
    a = _action(session, shared.id)
    a.brief = "batching"
    session.add(a)
    session.commit()
    monkeypatch.setattr(agent, "run", lambda *a, **k: GOOD)
    pipeline.process_shared_video(session, shared)
    assert shared.local_video_path is None and not list(VIDEOS_DIR.glob(f"{shared.id}.*"))


# ---------------- download failures ----------------

def test_failed_download_discards_partial_files(monkeypatch, session):
    vid = intake.ingest_shared_link("https://www.tiktok.com/@a/video/2", "")
    v = session.get(Video, vid)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    part = VIDEOS_DIR / f"{vid}.mp4.part"
    part.write_bytes(b"\0")

    def dies(url, video_id):
        raise RuntimeError("ERROR: [TikTok] This post may not be comfortable for some audiences")
    monkeypatch.setattr(download, "download_video", dies)
    monkeypatch.setattr(pipeline.throttle, "wait", lambda stop: None)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    pipeline.collect_video(session, v)
    assert v.status == Status.ERROR and not part.exists()


# ---------------- lane and reporters ----------------

def test_reporter_crash_does_not_lose_the_result_or_jam_the_lane(monkeypatch, session, shared):
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: _analysis())
    monkeypatch.setattr(agent, "run", lambda *a, **k: GOOD)

    class Broken(intake.Reporter):
        def progress(self, text):
            raise RuntimeError("Telegram sendMessage failed")

        def finished(self, action, video):
            raise RuntimeError("Telegram editMessageText failed")
    intake._process(shared.id, Broken(), False)
    assert _action(session, shared.id).status == ActionStatus.DRAFTED and intake.in_flight() == 0
    assert intake.submit(shared.id, intake.Reporter()) is True  # not stuck as "in flight"


def test_rerun_of_a_deleted_video_fails_visibly(session):
    v = Video(tiktok_url="https://www.tiktok.com/@a/video/3", source="telegram")
    session.add(v)
    session.commit()
    a = Action(video_id=v.id, action_type=ActionType.OTHER, status=ActionStatus.FAILED, error_message="old")
    session.add(a)
    session.commit()
    rep = Recorder()
    intake._process(v.id + 1000, rep, False)  # a video id that no longer exists
    assert rep.calls == [("failed", "The item this belonged to was deleted.")]
    assert intake.rerun(a.id + 1000) is None  # no such action: callers can say so


def test_escaped_lane_exception_is_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(intake, "log", SimpleNamespace(error=lambda msg, **k: logged.append(msg)))
    intake._log_escaped(SimpleNamespace(exception=lambda: RuntimeError("x")))
    intake._log_escaped(SimpleNamespace(exception=lambda: None))
    assert logged == ["Research lane task crashed"]


def test_unexpected_error_marks_a_queued_placeholder_failed(monkeypatch, session, shared):
    def odd(session_, video, *a, **k):
        raise KeyError("usage")
    monkeypatch.setattr("app.pipeline.process_shared_video", odd)
    rep = Recorder()
    intake._process(shared.id, rep, False)
    a = _action(session, shared.id)
    assert a.status == ActionStatus.FAILED and "Tap Retry" in a.error_message
    assert rep.calls[-1][0] == "failed"


def test_mark_failed_leaves_settled_actions_alone(session, shared):
    a = _action(session, shared.id)
    a.status, a.result, a.verdict = ActionStatus.DRAFTED, GOOD.to_json(), "maybe"
    session.add(a)
    session.commit()
    intake.mark_failed(shared.id, "late failure")
    assert _action(session, shared.id).status == ActionStatus.DRAFTED


# ---------------- malformed model output ----------------

def test_findings_coerce_every_odd_shape():
    f = agent._findings_from({
        "verdict": "banana", "headline": " Use it ", "telegram_text": None, "report_markdown": "x" * 100_000,
        "ideas": "not a list", "links": [{"title": 1, "url": 42}, {"title": "ok", "url": "https://a"}, "str"],
        "place": "Joe's", "repos": [{"name": None, "url": 7, "fit": "worth_it"}, 3],
        "project_fit": [{"project": None}, {"project": "gw", "fit": "nope", "value": "huge"}, "x"],
        "new_project": {"idea": ["a", "b"]}, "new_project_idea": 5,
    }, 0.1)
    assert f.verdict == "maybe" and f.headline == "Use it" and f.telegram_text == "Use it"
    assert len(f.report_markdown) == agent.MAX_REPORT_CHARS and f.ideas == []
    assert f.links == [{"title": "ok", "url": "https://a"}] and f.place is None
    assert f.repos == [{"name": "?", "url": None, "fit": "worth_it"}]
    assert f.project_fit == [{"project": "gw", "fit": "nope", "value": "huge"}]
    assert f.new_project is None and f.new_project_idea is None


def test_findings_without_body_is_an_agent_error():
    with pytest.raises(agent.AgentError):
        agent._findings_from({"verdict": "skip", "headline": "h", "report_markdown": ""}, 0)
    with pytest.raises(agent.AgentError):
        agent._findings_from("not a dict", 0)


@pytest.mark.parametrize("stored", ["", None, "{not json", "[]", "null", "{}", json.dumps({"headline": ""}),
                                    json.dumps({"headline": 5})])
def test_unreadable_stored_results_load_as_none(stored):
    assert agent.load_report(stored) is None


def test_home_page_survives_malformed_and_corrupt_reports(session):
    v = Video(tiktok_url="https://www.tiktok.com/@a/video/4", source="telegram", category="Tech / AI / Coding")
    session.add(v)
    session.commit()
    odd = {"headline": "Odd", "telegram_text": "t", "report_markdown": "body", "links": [{"title": "x", "url": 7}],
           "place": "Joe's", "repos": [{"name": "r", "url": 7, "stars": "many", "fit": "worth_it"}],
           "project_fit": [{"project": "gw", "fit": "worth_it", "value": "huge", "where": None}],
           "new_project": {"idea": 3}, "ideas": "one"}
    session.add(Action(video_id=v.id, action_type=ActionType.REPO, status=ActionStatus.DRAFTED, verdict="worth_it",
                       result=json.dumps(odd)))
    w = Video(tiktok_url="https://www.tiktok.com/@a/video/5", source="telegram")
    session.add(w)
    session.commit()
    session.add(Action(video_id=w.id, action_type=ActionType.SKILL, status=ActionStatus.DRAFTED, verdict="maybe",
                       result="{corrupt"))
    session.commit()
    r = TestClient(app, raise_server_exceptions=False).get("/")
    assert r.status_code == 200 and "Odd" in r.text
    assert "The saved report can&#39;t be read" in r.text and "Retry" in r.text  # the corrupt one is retryable
    assert tb.format_result(session.exec(select(Action).where(Action.video_id == w.id)).one(), w).startswith("⚠️")
    n = ntfy_intake.result_notification(session.exec(select(Action).where(Action.video_id == v.id)).one(), v)
    assert "Odd" in n["title"]


def test_home_page_renders_items_being_worked_on(session):
    v = Video(tiktok_url="https://www.tiktok.com/@a/video/6", source="telegram")
    session.add(v)
    session.commit()
    session.add(Action(video_id=v.id, action_type=ActionType.REPO, status=ActionStatus.IN_PROGRESS))
    session.commit()
    r = TestClient(app, raise_server_exceptions=False).get("/")
    assert r.status_code == 200 and "Looking into it" in r.text and "Repos" in r.text


def test_stuck_item_without_a_message_still_explains_itself(session):
    v = Video(tiktok_url="https://www.tiktok.com/@a/video/7", source="telegram")
    session.add(v)
    session.commit()
    session.add(Action(video_id=v.id, action_type=ActionType.OTHER, status=ActionStatus.FAILED))
    session.commit()
    page = TestClient(app).get("/").text
    assert "Something went wrong. Tap Retry." in page


def test_fit_line_tolerates_sparse_entries():
    assert tb.fit_line({"project_fit": ["x", {"fit": "maybe"}]}).startswith("Fits: ?")


# ---------------- doorways ----------------

def test_ntfy_duplicate_share_says_already_on_it(monkeypatch, session):
    s = new_ntfy_setup()
    sent = []
    monkeypatch.setattr(ntfy_intake, "notify", lambda title, *a, **k: sent.append(title) or True)
    raw = json.dumps({"v": 1, "token": s["token"], "url": "https://www.tiktok.com/@a/video/8"})
    assert ntfy_intake.handle_message(raw, submit=lambda vid, deep=False: False) == "busy"
    assert sent == ["⏳ Already on it"]  # and no misleading "Got it"


def test_ntfy_deep_for_a_missing_report_is_explained(monkeypatch):
    new_ntfy_setup()
    sent = []
    monkeypatch.setattr(ntfy_intake, "notify", lambda title, *a, **k: sent.append(title) or True)
    monkeypatch.setattr(intake, "rerun", lambda action_id, deep=False: None)
    assert ntfy_intake.handle_message(json.dumps({"v": 1, "deep": 9, "sig": ntfy_intake.deep_signature(9)})) == "ignored"
    assert sent == ["⚠️ Couldn't find that report"]
    monkeypatch.setattr(intake, "rerun", lambda action_id, deep=False: False)
    assert ntfy_intake.handle_message(json.dumps({"v": 1, "deep": 9, "sig": ntfy_intake.deep_signature(9)})) == "busy"
    assert sent[-1] == "⏳ Already on it"


def test_ntfy_bad_event_is_reported_and_does_not_raise(monkeypatch):
    sent = []
    monkeypatch.setattr(ntfy_intake, "notify", lambda title, *a, **k: sent.append(title) or True)

    def db_locked(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(ntfy_intake, "handle_message", db_locked)
    ntfy_intake._handle_event({"id": "abc", "event": "message", "message": "{}"})
    assert sent == ["⚠️ Couldn't take that in"]


def test_telegram_duplicate_share_edits_the_status_message(session):
    save_telegram_settings(bot_token="1:a", chat_id=42)
    edits = []
    tg = SimpleNamespace(send=lambda chat, text, reply_to=None: 9, edit=lambda chat, mid, text: edits.append(text))
    upd = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 42}, "text": "https://www.tiktok.com/@a/video/9"}}
    tb.handle_update(upd, tg, submit=lambda *a, **k: False)
    assert edits and edits[0].startswith("⏳ Already on it")


def test_telegram_failed_update_tells_the_paired_chat_only():
    save_telegram_settings(bot_token="1:a", chat_id=42)
    sent = []
    tg = SimpleNamespace(send=lambda chat, text, reply_to=None: sent.append(chat) or 9)
    tb._tell_owner({"message": {"chat": {"id": 42}}}, tg)
    tb._tell_owner({"message": {"chat": {"id": 7}}}, tg)
    tb._tell_owner({"garbage": True}, tg)
    assert sent == [42]


def test_shared_image_survives_ocr_crash(monkeypatch, session):
    def no_tesseract(path):
        raise RuntimeError("tesseract is not installed or it's not in your PATH")
    monkeypatch.setattr("app.ocr.extract_image_text", no_tesseract)
    vid = intake.ingest_shared_image(b"\x89PNG\r\n\x1a\n" + b"\0" * 32, "shot.png", "")
    assert session.get(Video, vid).ocr_text == ""


# ---------------- settings file ----------------

def test_corrupt_secrets_file_gives_a_readable_error_and_is_not_overwritten():
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text("{not json")
    r = TestClient(app, raise_server_exceptions=False).post("/settings/agent", data={"about_me": "me"})
    assert r.status_code == 500 and "Settings couldn't be saved" in r.json()["detail"]
    assert SECRETS_PATH.read_text() == "{not json" and get_agent_settings()["about_me"] == ""
