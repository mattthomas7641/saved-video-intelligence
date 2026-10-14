"""Telegram doorway (optional): share a TikTok to your bot, get research back in the same chat.

Long-polls the Bot API (`getUpdates`) from a daemon thread, so it needs no public
URL, port forwarding or tunnel: it works the same on a laptop as on a server.
The bot answers exactly one chat, the one paired from Settings with a one-time
code; everything else is ignored. Off until a bot token is saved in Settings;
the "Send to Sidequest" Shortcut (app/ntfy_intake.py) is the main doorway.

Each shared link gets one status message that's edited in place as the video
moves through collect -> analyze -> research, ending with the verdict and a
short summary. The full report lives on the dashboard's Agent page.
"""
import html
import json
import logging
import os
import re
import threading
import time

import httpx

from app import intake
from app.config import (
    PUBLIC_BASE_URL,
    get_action_settings,
    get_agent_settings,
    get_telegram_settings,
    save_action_settings,
    save_telegram_settings,
)
from app.intake import extract_link, ingest_shared_link, is_tiktok  # noqa: F401 - re-exported for callers

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50  # seconds Telegram holds a getUpdates request open

VERDICT_LABELS = {"worth_it": "✅ Do it", "maybe": "🤔 Optional", "skip": "💤 Nothing to do"}
TYPE_LABELS = {"repo": "Repos", "research": "Research", "place": "Place", "project": "Project",
               "skill": "Skill", "job": "Job", "other": "Other"}
FIT_LABELS = {"worth_it": "worth it", "maybe": "maybe", "skip": "skip"}
VALUE_LABELS = {"fun": "fun / personal use", "personal_and_resume": "personal use + resume boost",
                "weak_resume": "weak resume boost", "not_worth_it": "not worth it"}

_poller_started = False


def ready() -> bool:
    s = get_telegram_settings()
    return bool(s["bot_token"] and s["chat_id"]) and not s.get("muted")


class TelegramClient:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self.token = token
        self.http = http or httpx.Client(timeout=POLL_TIMEOUT + 10)

    def call(self, method: str, **params):
        r = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description', r.status_code)}")
        return data["result"]

    def send(self, chat_id: int, text: str, reply_to: int | None = None) -> int:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.call("sendMessage", **params)["message_id"]

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                      link_preview_options={"is_disabled": True})
        except RuntimeError as e:
            if "not modified" not in str(e):  # same text twice in a row is harmless
                log.warning("%s", e)


class TelegramReporter(intake.Reporter):
    """Edits one status message in place as the video moves through the lane."""

    def __init__(self, chat_id: int, message_id: int, tg: TelegramClient | None = None):
        self.chat_id, self.message_id = chat_id, message_id
        self.tg = tg or TelegramClient(get_telegram_settings()["bot_token"])

    def _edit(self, text: str) -> None:
        if not get_telegram_settings().get("muted"):  # Settings → Telegram → mute
            self.tg.edit(self.chat_id, self.message_id, text)

    def progress(self, text: str) -> None:
        self._edit(f"⏳ {html.escape(text)}")

    def finished(self, action, video) -> None:
        self._edit(format_result(action, video))

    def failed(self, text: str) -> None:
        log.warning("Shared item failed: %s", text)
        self._edit(f"⚠️ {html.escape(text)}")


def reporter_new_message(chat_id: int, text: str) -> intake.Reporter:
    tg = TelegramClient(get_telegram_settings()["bot_token"])
    try:
        return TelegramReporter(chat_id, tg.send(chat_id, text), tg)
    except (RuntimeError, httpx.HTTPError):
        return intake.Reporter()


# ---------------- formatting ----------------

def fit_line(report: dict) -> str:
    """'Fits: llm-inference-gateway (worth it, S) - replaces the hand-rolled batcher'"""
    fits = [f for f in report.get("project_fit") or [] if f.get("fit") != "skip"]
    if fits:
        f = fits[0]
        effort = f", {f['effort']}" if f.get("effort") else ""
        value = f" · {VALUE_LABELS[f['value']]}" if f.get("value") in VALUE_LABELS else ""
        return (f"Fits: {f['project']} ({FIT_LABELS.get(f.get('fit'), f.get('fit'))}{effort}{value}): "
                f"{f.get('architecture_impact', '')}")
    new = report.get("new_project") or {}
    if new.get("idea"):
        value = f" ({VALUE_LABELS[new['value']]})" if new.get("value") in VALUE_LABELS else ""
        return f"New project{value}: {new['idea']}"
    if report.get("new_project_idea"):
        return f"New project idea: {report['new_project_idea']}"
    if report.get("project_fit"):
        return "Doesn't fit your current projects."
    return ""


def format_result(action, video) -> str:
    """The final, edited-in-place status message."""
    title = html.escape(video.author and f"@{video.author}" or "Shared link")
    if action.status.value == "drafted" and action.result:
        r = json.loads(action.result)
        lines = [f"<b>{html.escape(r.get('headline', ''))}</b>",
                 f"{VERDICT_LABELS.get(action.verdict, '')} · {TYPE_LABELS.get(action.action_type.value, '')}",
                 "", html.escape(r.get("telegram_text", ""))]
        fit = fit_line(r)
        if fit:
            lines += ["", f"🧩 {html.escape(fit)}"]
        if r.get("ideas"):
            lines += ["", "<b>Ideas</b>"] + [f"• {html.escape(i)}" for i in r["ideas"][:3]]
        lines += ["", _links_line(action, video)]
        return "\n".join(lines)[:4000]
    if action.status.value == "queued" and action.error_message:
        return f"⏸️ {html.escape(action.error_message)}\n{title}\n\n{_links_line(action, video)}"
    reason = html.escape(action.error_message or "Something went wrong.")
    return f"⚠️ Couldn't finish researching this.\n{reason}\n\n{_links_line(action, video)}"


def report_link(action) -> str | None:
    """Dashboard link for a report, if PUBLIC_BASE_URL is reachable from a phone."""
    if re.search(r"//(127\.0\.0\.1|localhost)", PUBLIC_BASE_URL):
        return None
    return f"{PUBLIC_BASE_URL}/agent#a{action.id}"


def _links_line(action, video) -> str:
    parts = [f'<a href="{html.escape(video.tiktok_url)}">Original</a>'] if video.tiktok_url.startswith("http") else []
    link = report_link(action)
    parts.insert(0, f'<a href="{link}">Full report</a>' if link else "Full report on the Agent page")
    return " · ".join(parts)


# ---------------- update handling ----------------

def _submit(video_id: int, chat_id: int, message_id: int, deep: bool = False) -> None:
    intake.submit(video_id, TelegramReporter(chat_id, message_id), deep=deep)


def handle_update(update: dict, tg: TelegramClient, submit=None) -> None:
    """Route one Telegram update. `submit(video_id, chat_id, message_id)` starts the
    research lane; injectable so tests don't spin up real work."""
    submit = submit or _submit
    msg = update.get("message") or update.get("channel_post")
    if not msg or "chat" not in msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    settings = get_telegram_settings()
    paired = settings["chat_id"]

    if text.startswith("/start"):
        code = text.removeprefix("/start").strip().upper()
        if paired == chat_id:
            tg.send(chat_id, "Already paired. Share a TikTok here and I'll research it.")
        elif settings["pairing_code"] and code == settings["pairing_code"].upper():
            save_telegram_settings(chat_id=chat_id, pairing_code="")
            tg.send(chat_id, "✅ Paired. From TikTok: Share → Telegram → this chat. Add a note if you want "
                             "something specific checked (\"useful for my OS project?\").\n\n"
                             "/status · /pause · /resume · reply /deep to a report to research it further")
        elif paired is None:
            tg.send(chat_id, "Not paired yet. Send /start followed by the code shown in the dashboard's Settings.")
        return

    if chat_id != paired:
        return  # not your chat: ignore silently

    if text.startswith("/status"):
        tg.send(chat_id, _status_text())
        return
    if text.startswith("/pause"):
        save_action_settings(agent_paused=True)
        tg.send(chat_id, "⏸️ Paused. Shared videos will be saved and analyzed but not researched. /resume to continue.")
        return
    if text.startswith("/resume"):
        save_action_settings(agent_paused=False)
        tg.send(chat_id, "▶️ Research agent is back on.")
        return
    if text.startswith("/deep"):
        _deep(msg, chat_id, tg)
        return
    if text.startswith("/"):
        tg.send(chat_id, "Share a TikTok (or any link) and I'll research it.\n/status · /pause · /resume · /deep")
        return

    url, note = extract_link(text)
    if msg.get("photo"):
        video_id = _ingest_photo(msg["photo"], tg, text)
        if video_id is None:
            tg.send(chat_id, "I couldn't download that photo. Try again, or send it as a screenshot.")
            return
    elif url:
        video_id = ingest_shared_link(url, note)
    elif len(text.split()) >= 3:
        video_id = intake.ingest_shared_text(text)
    else:
        tg.send(chat_id, "Send me a link, a screenshot or a few words: anything you want looked into.")
        return
    status_id = tg.send(chat_id, "📥 Got it. Queued…", reply_to=msg.get("message_id"))
    submit(video_id, chat_id, status_id)


def _ingest_photo(sizes: list, tg: TelegramClient, caption: str) -> int | None:
    """Download the largest size of a shared photo through the Bot API."""
    try:
        file_path = tg.call("getFile", file_id=sizes[-1]["file_id"])["file_path"]
        r = tg.http.get(f"https://api.telegram.org/file/bot{tg.token}/{file_path}")
        r.raise_for_status()
    except (RuntimeError, httpx.HTTPError, KeyError, IndexError):
        return None
    if len(r.content) > intake.MAX_IMAGE_BYTES:
        return None
    return intake.ingest_shared_image(r.content, file_path, caption)


def _deep(msg: dict, chat_id: int, tg: TelegramClient) -> None:
    """Reply /deep to a report message to re-run it with web research forced on."""
    from sqlmodel import select

    from app.db import get_session
    from app.models import Action

    replied = (msg.get("reply_to_message") or {}).get("message_id")
    if not replied:
        tg.send(chat_id, "Reply /deep to one of my report messages to research that video further.")
        return
    with get_session() as session:
        action = session.exec(select(Action).where(Action.telegram_chat_id == chat_id,
                                                   Action.telegram_message_id == replied)).first()
        video_id = action.video_id if action else None
    if not video_id:
        tg.send(chat_id, "I couldn't match that message to a report.")
        return
    status_id = tg.send(chat_id, "🔎 Researching deeper…", reply_to=replied)
    intake.submit(video_id, TelegramReporter(chat_id, status_id, tg), deep=True)


def _status_text() -> str:
    from app.db import get_session
    from app.pipeline import agent_spend_today

    with get_session() as session:
        spent = agent_spend_today(session)
    cap = get_agent_settings()["daily_cap_usd"]
    paused = get_action_settings()["agent_paused"]
    return (f"{'⏸️ Paused' if paused else '▶️ Running'} · {intake.in_flight()} in progress\n"
            f"Today's research spend: ${spent:.2f} of ${cap:.2f}")


# ---------------- poller ----------------

def _poll_forever() -> None:
    offset = None
    token = None
    tg = None
    while True:
        current = get_telegram_settings()["bot_token"]
        if not current:
            time.sleep(5)
            continue
        if current != token:  # token added or changed in Settings
            token, tg, offset = current, TelegramClient(current), None
        try:
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            for update in tg.call("getUpdates", **params):
                offset = update["update_id"] + 1
                try:
                    handle_update(update, tg)
                except Exception:  # noqa: BLE001 - one bad message must not stop the bot
                    log.exception("Failed to handle Telegram update %s", update.get("update_id"))
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            log.warning("Telegram poll failed (%s); retrying shortly", e)
            time.sleep(10)


def start_poller() -> None:
    global _poller_started
    if _poller_started or os.environ.get("SVI_DISABLE_TELEGRAM"):
        return
    _poller_started = True
    threading.Thread(target=_poll_forever, daemon=True, name="telegram-poller").start()


def check_token(token: str) -> str | None:
    """The bot's @username if the token is valid, else None."""
    try:
        return TelegramClient(token, httpx.Client(timeout=10)).call("getMe").get("username")
    except (httpx.HTTPError, RuntimeError, ValueError):
        return None
