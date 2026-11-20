"""The quest engine (app/quests.py) and the calendar reader (app/calendar_busy.py):
planning tasks from a report, approvals, the scheduler, restart recovery. No network:
GitHub is faked with httpx.MockTransport and follow-up checks with a plain function."""
import json
from datetime import datetime, timedelta

import httpx
from sqlmodel import select

from app import calendar_busy, ntfy_intake, quests, tracing
from app.config import save_agent_settings, save_ntfy_settings, save_quest_settings
from app.models import Action, ActionStatus, ActionType, Run, Status, Task, Video


def _action(session, report: dict, kind=ActionType.PLACE):
    v = Video(tiktok_url="https://x.test/1", status=Status.DONE, source="telegram", summary="s")
    session.add(v)
    session.commit()
    a = Action(video_id=v.id, action_type=kind, status=ActionStatus.DRAFTED, verdict="worth_it",
               result=json.dumps({"headline": "Great tacos", "telegram_text": "Go Friday", "report_markdown": "r",
                                  **report}))
    session.add(a)
    session.commit()
    return a


def _soon(days=2, hour=19):
    d = datetime.now(calendar_busy.tz()) + timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


PLACE = {"place": {"name": "Taqueria X", "address": "1 Main St", "reservation_url": "https://resy.test/x"},
         "suggested_times": [{"start": _soon(2), "minutes": 90, "why": "Fri dinner"},
                             {"start": _soon(3, 12), "minutes": 60},
                             {"start": "2020-01-01T19:00"}]}  # in the past: dropped


def test_plan_proposes_calendar_slots_and_drops_past_ones(session):
    a = _action(session, PLACE)
    tasks = quests.plan(session, a, json.loads(a.result))
    assert [t.kind for t in tasks] == ["calendar", "calendar"]
    assert all(t.status == "proposed" for t in tasks)
    assert "Fri dinner" in tasks[0].title
    p = json.loads(tasks[0].payload)
    assert p["summary"] == "Taqueria X" and p["location"] == "1 Main St" and "resy.test" in p["details"]


def test_replanning_replaces_waiting_tasks_but_keeps_finished_ones(session):
    a = _action(session, {**PLACE, "follow_up": {"after_days": 7, "watch_for": "reopening date"}})
    first = quests.plan(session, a, json.loads(a.result))
    quests.approve(first[0].id)  # picked a slot: finished
    quests.plan(session, a, json.loads(a.result))
    rows = session.exec(select(Task).where(Task.action_id == a.id)).all()
    statuses = sorted(t.status for t in rows)
    assert statuses.count("done") == 1
    assert statuses.count("cancelled") == 1          # the old check-back
    assert sum(1 for t in rows if t.status in ("proposed", "scheduled")) == 3  # the new plan


def test_follow_up_is_scheduled_and_can_be_turned_off(session):
    a = _action(session, {"follow_up": {"after_days": 7, "watch_for": "release date"}}, ActionType.RESEARCH)
    now = datetime(2026, 11, 20, 12)
    [t] = quests.plan(session, a, json.loads(a.result), now=now)
    assert t.kind == "follow_up" and t.status == "scheduled" and t.run_at == now + timedelta(days=7)
    assert json.loads(t.payload)["later"] == [30]   # checked after a week: one more look a month on
    save_quest_settings(follow_ups=False)
    assert quests.plan(session, a, json.loads(a.result), now=now) == []


def test_issue_proposed_only_for_a_project_with_a_github_remote(session, monkeypatch):
    a = _action(session, {"github_issue": {"project": "gateway", "title": "Add caching", "body": "why"}},
                ActionType.REPO)
    monkeypatch.setattr("app.projects.github_repo_of", lambda name: None)
    assert quests.plan(session, a, json.loads(a.result)) == []
    monkeypatch.setattr("app.projects.github_repo_of", lambda name: "me/gateway")
    [t] = quests.plan(session, a, json.loads(a.result))
    assert t.kind == "github_issue" and "me/gateway" in t.title


def test_approving_a_slot_opens_google_calendar_and_declines_the_others(session):
    a = _action(session, PLACE)
    t1, t2 = quests.plan(session, a, json.loads(a.result))
    message, url = quests.approve(t1.id)
    assert url.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert "Taqueria" in url and "ctz=" in url
    session.expire_all()
    assert session.get(Task, t1.id).status == "done"
    assert session.get(Task, t2.id).status == "declined"


def test_opening_an_approved_issue(session, monkeypatch):
    a = _action(session, {"github_issue": {"project": "gateway", "title": "Add caching", "body": "why"}},
                ActionType.REPO)
    monkeypatch.setattr("app.projects.github_repo_of", lambda name: "me/gateway")
    [t] = quests.plan(session, a, json.loads(a.result))
    save_quest_settings(github_token="github_pat_test")
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"html_url": "https://github.com/me/gateway/issues/7"})

    t.status = "running"
    session.add(t)
    session.commit()
    quests._open_issue(t.id, http=httpx.Client(transport=httpx.MockTransport(handler)))
    session.expire_all()
    done = session.get(Task, t.id)
    assert done.status == "done" and json.loads(done.result)["url"].endswith("/issues/7")
    assert seen["url"] == "https://api.github.com/repos/me/gateway/issues"
    assert seen["auth"] == "Bearer github_pat_test" and seen["body"]["title"] == "Add caching"


def test_issue_without_permission_fails_in_plain_words_and_can_be_approved_again(session, monkeypatch):
    a = _action(session, {"github_issue": {"project": "gateway", "title": "T", "body": "b"}}, ActionType.REPO)
    monkeypatch.setattr("app.projects.github_repo_of", lambda name: "me/gateway")
    [t] = quests.plan(session, a, json.loads(a.result))
    save_quest_settings(github_token="github_pat_test")
    t.status = "running"
    session.add(t)
    session.commit()
    quests._open_issue(t.id, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))))
    session.expire_all()
    failed = session.get(Task, t.id)
    assert failed.status == "failed" and "Issues write access" in failed.error
    monkeypatch.setattr(quests, "_open_issue", lambda task_id: quests._finish(task_id, "done", result={"url": "u"}))
    quests.approve(t.id, run_async=False)
    session.expire_all()
    assert session.get(Task, t.id).status == "done"


def test_due_follow_up_runs_notifies_on_change_and_schedules_one_more(session, monkeypatch):
    a = _action(session, {"follow_up": {"after_days": 7, "watch_for": "release date"}}, ActionType.RESEARCH)
    [t] = quests.plan(session, a, json.loads(a.result), now=datetime.utcnow() - timedelta(days=8))
    sent = []
    monkeypatch.setattr(quests, "_notify", lambda title, message, click=None: sent.append(title))
    out = {"changed": True, "headline": "It shipped", "summary": "Out today, $20/mo", "keep_watching": True,
           "links": [], "cost_usd": 0.004}
    monkeypatch.setattr("app.gemini_client.follow_up", lambda payload: out)
    monkeypatch.setattr("app.config.get_gemini_key", lambda: "AIza-test")
    assert quests.run_due() == 1
    session.expire_all()
    assert session.get(Task, t.id).status == "done"
    assert sent == ["🔁 Update: It shipped"]
    later = session.exec(select(Task).where(Task.status == "scheduled")).all()
    assert len(later) == 1 and json.loads(later[0].payload)["later"] == []
    assert session.exec(select(Run).where(Run.kind == "follow_up")).first() is not None
    assert quests.run_due() == 0   # nothing else due: never runs twice


def test_quiet_follow_up_doesnt_notify(session, monkeypatch):
    a = _action(session, {"follow_up": {"after_days": 30, "watch_for": "x"}}, ActionType.RESEARCH)
    quests.plan(session, a, json.loads(a.result), now=datetime.utcnow() - timedelta(days=31))
    sent = []
    monkeypatch.setattr(quests, "_notify", lambda *args, **kw: sent.append(args))
    monkeypatch.setattr("app.gemini_client.follow_up", lambda p: {"changed": False, "headline": "No news",
                                                                  "keep_watching": True, "links": []})
    monkeypatch.setattr("app.config.get_gemini_key", lambda: "AIza-test")
    quests.run_due()
    assert sent == []
    assert session.exec(select(Task).where(Task.status == "scheduled")).all() == []  # 30 days: no later check


def test_follow_up_waits_for_tomorrow_when_the_cap_is_used_up(session, monkeypatch):
    a = _action(session, {"follow_up": {"after_days": 7, "watch_for": "x"}}, ActionType.RESEARCH)
    [t] = quests.plan(session, a, json.loads(a.result), now=datetime.utcnow() - timedelta(days=8))
    save_agent_settings(daily_cap_usd=1.0)
    monkeypatch.setattr(tracing, "spend_today", lambda s: 1.5)
    monkeypatch.setattr("app.gemini_client.follow_up", lambda p: (_ for _ in ()).throw(AssertionError("spent")))
    quests.run_due()
    session.expire_all()
    t = session.get(Task, t.id)
    assert t.status == "scheduled" and t.run_at > datetime.utcnow()


def test_failed_check_retries_then_gives_up(session, monkeypatch):
    a = _action(session, {"follow_up": {"after_days": 7, "watch_for": "x"}}, ActionType.RESEARCH)
    [t] = quests.plan(session, a, json.loads(a.result), now=datetime.utcnow() - timedelta(days=8))
    monkeypatch.setattr("app.gemini_client.follow_up", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("app.config.get_gemini_key", lambda: "AIza-test")
    for _ in range(quests.MAX_ATTEMPTS):
        session.expire_all()
        row = session.get(Task, t.id)
        row.run_at = datetime.utcnow() - timedelta(minutes=1)
        session.add(row)
        session.commit()
        quests.run_due()
    session.expire_all()
    row = session.get(Task, t.id)
    assert row.status == "failed" and "boom" not in row.error and row.attempts == quests.MAX_ATTEMPTS


def test_recover_puts_interrupted_tasks_back(session):
    a = _action(session, {})
    session.add(Task(action_id=a.id, kind="follow_up", status="running", title="t"))
    session.add(Task(action_id=a.id, kind="github_issue", status="running", title="i"))
    session.commit()
    assert quests.recover() == 2
    session.expire_all()
    kinds = {t.kind: t.status for t in session.exec(select(Task)).all()}
    assert kinds == {"follow_up": "scheduled", "github_issue": "proposed"}


def test_notification_buttons_and_signed_approval(session, monkeypatch):
    save_ntfy_settings(inbox_topic="in", results_topic="out", token="tok")
    a = _action(session, {"github_issue": {"project": "gateway", "title": "T", "body": "b"}}, ActionType.REPO)
    monkeypatch.setattr("app.projects.github_repo_of", lambda name: "me/gateway")
    [t] = quests.plan(session, a, json.loads(a.result))
    [button] = quests.notification_actions([t])
    body = json.loads(button["body"])
    assert button["action"] == "http" and body["approve"] == t.id
    approved = []
    monkeypatch.setattr(quests, "approve", lambda task_id: approved.append(task_id) or ("ok", None))
    forged = json.dumps({"v": 1, "approve": t.id, "sig": "nope"})
    assert ntfy_intake.handle_message(forged) == "ignored"
    assert ntfy_intake.handle_message(button["body"]) == "approved" and approved == [t.id]


def test_calendar_slots_become_view_buttons(session):
    a = _action(session, PLACE)
    tasks = quests.plan(session, a, json.loads(a.result))
    buttons = quests.notification_actions(tasks)
    assert [b["action"] for b in buttons] == ["view", "view"]
    assert buttons[0]["url"].startswith("https://calendar.google.com/")  # PUBLIC_BASE_URL is local in tests


# ---------------- calendar_busy ----------------

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
SUMMARY:Secret dentist appointment
DTSTART;TZID=America/New_York:20261124T180000
DTEND;TZID=America/New_York:20261124T200000
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Gym
DTSTART;TZID=America/New_York:20261121T070000
DTEND;TZID=America/New_York:20261121T080000
RRULE:FREQ=WEEKLY
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Free reminder
TRANSP:TRANSPARENT
DTSTART;TZID=America/New_York:20261122T120000
DTEND;TZID=America/New_York:20261122T130000
END:VEVENT
END:VCALENDAR
"""


def test_busy_times_from_ical_without_titles():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=ICS)))
    now = datetime(2026, 11, 20, 9, tzinfo=calendar_busy.tz())
    blocks = calendar_busy.busy("webcal://cal.test/basic.ics", days=14, http=http, now=now)
    assert len(blocks) == 3   # dentist + the weekly gym twice; the "free" event doesn't block time
    text = calendar_busy.context_block(blocks, now=now)
    assert "Tue Nov 24: 18:00-20:00" in text and "Sat Nov 21: 07:00-08:00" in text
    assert "Sun Nov 22: free" in text
    assert "dentist" not in text.lower()   # only times reach the model
    assert calendar_busy.overlaps(blocks, datetime(2026, 11, 24, 19, tzinfo=calendar_busy.tz()),
                                  datetime(2026, 11, 24, 21, tzinfo=calendar_busy.tz()))


def test_calendar_address_must_be_https():
    import pytest

    with pytest.raises(ValueError):
        calendar_busy.busy("http://cal.test/basic.ics", http=httpx.Client())


def test_ics_file_for_a_slot():
    start = calendar_busy.parse_local("2026-11-28T19:00")
    data = calendar_busy.ics("Taqueria X", start, start + timedelta(hours=1), "1 Main St", uid="task-1@sidequest")
    assert b"SUMMARY:Taqueria X" in data and b"LOCATION:1 Main St" in data


# ---------------- pages ----------------

def test_report_shows_next_steps_and_routes_work(session):
    from fastapi.testclient import TestClient

    from app.main import app

    a = _action(session, {**PLACE, "follow_up": {"after_days": 7, "watch_for": "new menu"}})
    tasks = quests.plan(session, a, json.loads(a.result))
    client = TestClient(app)
    page = client.get("/").text
    assert "Pick a time to go" in page and "Checking back" in page and "new menu" in page
    assert "Waiting on you: pick a time" in page
    ics = client.get(f"/tasks/{tasks[0].id}/event.ics")
    assert ics.status_code == 200 and ics.headers["content-type"].startswith("text/calendar")
    r = client.post(f"/tasks/{tasks[0].id}/approve", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://calendar.google.com/")
    assert client.get(f"/tasks/{tasks[1].id}/approve?sig=bad", follow_redirects=False).status_code == 403
    follow = [t for t in tasks if t.kind == "follow_up"][0]
    client.post(f"/tasks/{follow.id}/cancel", data={"next": "/"})
    session.expire_all()
    assert session.get(Task, follow.id).status == "cancelled"


def test_settings_section_saves_toggles(session):
    from fastapi.testclient import TestClient

    from app.config import get_quest_settings
    from app.main import app

    client = TestClient(app)
    assert "Follow-through" in client.get("/settings").text
    client.post("/settings/quests", data={"flex_shares": "true", "github_token": "github_pat_x"})
    s = get_quest_settings()
    assert s["flex_shares"] and not s["follow_ups"] and s["github_token"] == "github_pat_x"


def test_activity_line_and_waiting_on_you(session):
    from fastapi.testclient import TestClient

    from app.main import _plain_error, app

    a = _action(session, PLACE)
    quests.plan(session, a, json.loads(a.result))
    session.add(Run(action_id=a.id, kind="research", status="ok", cost_usd=0.03, searches=3, duration_ms=41000,
                    steps=json.dumps([{"t": "tool", "name": "github_repo", "args": "repo=a/b", "ok": True},
                                      {"t": "tool", "name": "read_project_file", "args": "project=gw, path=app.py",
                                       "ok": True}])))
    session.commit()
    act = tracing.activity(session, [a.id])[a.id]
    assert act["searches"] == 3 and act["repos"] == 1 and act["files"] == 1 and act["projects"] == ["gw"]
    page = TestClient(app).get("/").text
    assert "Waiting on you" in page and "1 waiting on you" in page
    assert "3 web searches" in page and "read 1 file in gw" in page
    assert _plain_error("Research failed: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF") == \
        "The connection dropped while researching. Tap Retry."
    assert _plain_error("Research agent is paused in Settings.") == "Research agent is paused in Settings."


# ---------------- Telegram: buttons on the result, taps, updates ----------------

class FakeTG:
    def __init__(self):
        self.calls = []
        self.token = "t"

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"message_id": 99}

    def send(self, chat_id, text, reply_to=None, buttons=None):
        self.calls.append(("send", {"chat_id": chat_id, "text": text, "buttons": buttons}))
        return 99

    def edit(self, chat_id, message_id, text, buttons=None):
        self.calls.append(("edit", {"text": text, "buttons": buttons}))


def test_telegram_result_has_slot_buttons_and_taps_work(session, monkeypatch):
    from app import telegram_bot
    from app.config import save_telegram_settings

    save_telegram_settings(bot_token="123:abc", chat_id=42)
    a = _action(session, {**PLACE, "follow_up": {"after_days": 7, "watch_for": "new menu"}})
    tasks = quests.plan(session, a, json.loads(a.result))
    tg = FakeTG()
    telegram_bot.TelegramReporter(42, 7, tg).finished(a, session.get(Video, a.video_id))
    _, edit = tg.calls[-1]
    flat = [b for row in edit["buttons"] for b in row]
    assert [b["callback_data"] for b in flat][:2] == [f"approve:{tasks[0].id}", f"approve:{tasks[1].id}"]
    assert flat[-1]["callback_data"] == f"deep:{a.id}"
    assert "Pick a time" in edit["text"] and "check back" in edit["text"]

    telegram_bot.handle_callback({"id": "q1", "data": f"approve:{tasks[0].id}", "message": {"chat": {"id": 42}}}, tg)
    sent = [c for c in tg.calls if c[0] == "send"][-1][1]
    assert sent["buttons"][0][0]["url"].startswith("https://calendar.google.com/")
    session.expire_all()
    assert session.get(Task, tasks[0].id).status == "done"

    before = len(tg.calls)   # a stranger's chat can't approve anything
    telegram_bot.handle_callback({"id": "q2", "data": f"approve:{tasks[1].id}", "message": {"chat": {"id": 7}}}, tg)
    assert [c[0] for c in tg.calls[before:]] == ["answerCallbackQuery"]


def test_follow_up_update_goes_to_telegram(monkeypatch):
    from app import telegram_bot

    sent = []
    monkeypatch.setattr(telegram_bot, "ready", lambda: True)
    monkeypatch.setattr(telegram_bot, "notify", lambda text, buttons=None: sent.append(text) or True)
    quests._notify("🔁 Update: It shipped", "Out today <b>", click=None)
    assert sent and "It shipped" in sent[0] and "&lt;b&gt;" in sent[0]


def test_telegram_repeat_message_and_research_again_button(session, monkeypatch):
    from app import intake, telegram_bot
    from app.config import save_telegram_settings

    save_telegram_settings(bot_token="123:abc", chat_id=42)
    a = _action(session, PLACE)
    tg = FakeTG()
    telegram_bot.TelegramReporter(42, 7, tg).already_sent(a, session.get(Video, a.video_id))
    _, edit = tg.calls[-1]
    assert "You sent this on" in edit["text"] and "Great tacos" in edit["text"]
    assert edit["buttons"][0][0]["callback_data"] == f"again:{a.id}"
    reruns = []
    monkeypatch.setattr(intake, "rerun", lambda action_id, deep=False: reruns.append((action_id, deep)) or True)
    telegram_bot.handle_callback({"id": "q", "data": f"again:{a.id}", "message": {"chat": {"id": 42}}}, tg)
    assert reruns == [(a.id, False)]
