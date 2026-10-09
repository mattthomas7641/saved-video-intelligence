"""Telegram intake: share a TikTok to your bot, get research back in the same chat.

Long-polls the Bot API (`getUpdates`) from a daemon thread, so it needs no public
URL, port forwarding or tunnel: it works the same on a laptop as on a server.
The bot answers exactly one chat, the one paired from Settings with a one-time
code; everything else is ignored.

Each shared link gets one status message that's edited in place as the video
moves through collect -> analyze -> research, ending with the verdict and a
short summary. The full report lives on the dashboard's Agent page.
"""
import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from sqlmodel import select

from app.config import (
    PUBLIC_BASE_URL,
    get_action_settings,
    get_agent_settings,
    get_telegram_settings,
    save_action_settings,
    save_telegram_settings,
)

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50  # seconds Telegram holds a getUpdates request open
LANE_WORKERS = 2   # shared videos processed at once, independent of bulk jobs

URL_RE = re.compile(r"https?://[^\s<>\"']+")
TIKTOK_HOST_RE = re.compile(r"^https?://([\w-]+\.)*tiktok(v)?\.com/", re.I)

_VERDICT_LABELS = {"worth_it": "✅ Worth it", "maybe": "🤔 Maybe", "skip": "⏭️ Skip"}
_TYPE_LABELS = {"repo": "Repos", "research": "Research", "place": "Place", "project": "Project",
                "skill": "Skill", "job": "Job", "other": "Other"}

_lane = ThreadPoolExecutor(max_workers=LANE_WORKERS, thread_name_prefix="shared-video")
_in_flight: set[int] = set()
_in_flight_lock = threading.Lock()
_poller_started = False


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


# ---------------- pure helpers (tested directly) ----------------

def extract_link(text: str) -> tuple[str | None, str]:
    """First URL in the message, plus everything else as your note. TikTok's
    share text ("Check out this video…") is dropped so it doesn't pose as a note."""
    m = URL_RE.search(text or "")
    if not m:
        return None, (text or "").strip()
    url = m.group(0).rstrip(").,!?")
    note = (text[: m.start()] + " " + text[m.end():]).strip()
    if re.fullmatch(r"(?is).*(check out|watch).*tiktok.*", note) or note.lower().startswith("#"):
        note = ""
    return url, note


def is_tiktok(url: str) -> bool:
    return bool(TIKTOK_HOST_RE.match(url))


def canonical_tiktok_url(url: str, http: httpx.Client | None = None) -> str:
    """Short share links (vm./vt.tiktok.com, tiktok.com/t/…) redirect to the real
    video URL; resolve them so the same video shared twice dedupes."""
    from app.ingest import _normalize

    direct = _normalize(url)
    if direct:
        return direct
    client = http or httpx.Client(timeout=15, follow_redirects=True,
                                  headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36"})
    try:
        final = str(client.head(url).url)
        return _normalize(final) or url
    except httpx.HTTPError:
        return url
    finally:
        if http is None:
            client.close()


def format_result(action, video) -> str:
    """The final, edited-in-place status message."""
    import json

    title = html.escape(video.author and f"@{video.author}" or "Shared link")
    if action.status.value == "drafted" and action.result:
        r = json.loads(action.result)
        lines = [f"<b>{html.escape(r.get('headline', ''))}</b>",
                 f"{_VERDICT_LABELS.get(action.verdict, '')} · {_TYPE_LABELS.get(action.action_type.value, '')}",
                 "", html.escape(r.get("telegram_text", ""))]
        if r.get("ideas"):
            lines += ["", "<b>Ideas</b>"] + [f"• {html.escape(i)}" for i in r["ideas"][:3]]
        lines += ["", _links_line(action, video)]
        return "\n".join(lines)[:4000]
    if action.status.value == "queued" and action.error_message:
        return f"⏸️ {html.escape(action.error_message)}\n{title}\n\n{_links_line(action, video)}"
    reason = html.escape(action.error_message or "Something went wrong.")
    return f"⚠️ Couldn't finish researching this.\n{reason}\n\n{_links_line(action, video)}"


def _links_line(action, video) -> str:
    parts = [f'<a href="{html.escape(video.tiktok_url)}">Video</a>']
    if not re.search(r"//(127\.0\.0\.1|localhost)", PUBLIC_BASE_URL):
        parts.insert(0, f'<a href="{PUBLIC_BASE_URL}/agent#a{action.id}">Full report</a>')
    else:
        parts.insert(0, "Full report on the Agent page")
    return " · ".join(parts)


# ---------------- update handling ----------------

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
                             "/status · /pause · /resume")
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
    if text.startswith("/"):
        tg.send(chat_id, "Share a TikTok (or any link) and I'll research it.\n/status · /pause · /resume")
        return

    url, note = extract_link(text)
    if not url:
        tg.send(chat_id, "Send me a link: TikTok → Share → Telegram. Text with it becomes your note.")
        return
    video_id = ingest_shared_link(url, note)
    status_id = tg.send(chat_id, "📥 Got it. Queued…", reply_to=msg.get("message_id"))
    submit(video_id, chat_id, status_id)


def ingest_shared_link(url: str, note: str) -> int:
    """Find or create the Video for a shared link and mark it as shared."""
    from app.db import get_session
    from app.models import Status, Video

    is_tt = is_tiktok(url)
    canonical = canonical_tiktok_url(url) if is_tt else url
    with get_session() as session:
        video = session.exec(select(Video).where(Video.tiktok_url == canonical)).first()
        if not video:
            video = Video(tiktok_url=canonical, saved_date=datetime.utcnow())
            if not is_tt:
                # A GitHub/article link has nothing to download or transcribe: the
                # agent fetches the page itself, so skip straight to analysis.
                video.status = Status.TRANSCRIBED
                video.caption = canonical
                video.transcript = ""
                video.ocr_text = ""
        video.source = "telegram"
        if note:
            video.user_note = note
        video.updated_at = datetime.utcnow()
        session.add(video)
        session.commit()
        session.refresh(video)
        return video.id


def _status_text() -> str:
    from app.db import get_session
    from app.pipeline import agent_spend_today

    with get_session() as session:
        spent = agent_spend_today(session)
    cap = get_agent_settings()["daily_cap_usd"]
    paused = get_action_settings()["agent_paused"]
    with _in_flight_lock:
        n = len(_in_flight)
    return (f"{'⏸️ Paused' if paused else '▶️ Running'} · {n} in progress\n"
            f"Today's research spend: ${spent:.2f} of ${cap:.2f}")


# ---------------- the research lane ----------------

def _submit(video_id: int, chat_id: int, message_id: int) -> None:
    with _in_flight_lock:
        if video_id in _in_flight:
            return
        _in_flight.add(video_id)
    _lane.submit(_process, video_id, chat_id, message_id)


def _process(video_id: int, chat_id: int, message_id: int) -> None:
    from app import analyze as analyze_mod
    from app.db import get_session
    from app.models import Video
    from app.pipeline import process_shared_video

    tg = TelegramClient(get_telegram_settings()["bot_token"])
    progress = lambda text: tg.edit(chat_id, message_id, f"⏳ {html.escape(text)}")  # noqa: E731
    try:
        with get_session() as session:
            video = session.get(Video, video_id)
            try:
                action = process_shared_video(session, video, progress, chat_id=chat_id, message_id=message_id)
            except analyze_mod.FatalAnalysisError as e:
                tg.edit(chat_id, message_id, f"⚠️ {html.escape(str(e))}")
                return
            except Exception as e:  # noqa: BLE001
                log.exception("Shared video %s failed", video_id)
                tg.edit(chat_id, message_id, f"⚠️ Couldn't process this video.\n{html.escape(str(e))[:300]}")
                return
            tg.edit(chat_id, message_id, format_result(action, video))
    finally:
        with _in_flight_lock:
            _in_flight.discard(video_id)


def rerun(action_id: int) -> bool:
    """Re-run research for one action from the dashboard; reports into its original chat message."""
    from app.db import get_session
    from app.models import Action

    with get_session() as session:
        action = session.get(Action, action_id)
        if not action:
            return False
        chat_id = action.telegram_chat_id or get_telegram_settings()["chat_id"]
        message_id = action.telegram_message_id
        video_id = action.video_id
    if chat_id and get_telegram_settings()["bot_token"]:
        tg = TelegramClient(get_telegram_settings()["bot_token"])
        try:
            message_id = tg.send(chat_id, "🔁 Re-running research…")
        except RuntimeError:
            message_id = None
    if chat_id and message_id:
        _submit(video_id, chat_id, message_id)
    else:
        _lane.submit(_process_silently, video_id)
    return True


def _process_silently(video_id: int) -> None:
    from app.db import get_session
    from app.models import Video
    from app.pipeline import process_shared_video

    with get_session() as session:
        try:
            process_shared_video(session, session.get(Video, video_id))
        except Exception:  # noqa: BLE001
            log.exception("Re-run for video %s failed", video_id)


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
