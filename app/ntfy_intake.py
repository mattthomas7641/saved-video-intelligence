"""The "Send to Sidequest" doorway: an iOS Shortcut in the share sheet, relayed by ntfy.

From TikTok, Instagram, Safari, Photos, a screenshot - anywhere with a share
sheet: Share -> Send to Sidequest. For links and text the Shortcut POSTs
{"v":1,"token":...,"url":<what you shared>} to a private ntfy.sh topic; for an
image it PUTs the file as an ntfy attachment with the same JSON in the Message
header. This module streams
that topic (ntfy holds messages, so like Telegram it needs no tunnel and nothing
on this machine is exposed), checks the token, and hands the link to the shared
research lane in app/intake.py. Results come back as a phone notification on a
second private topic, which the free ntfy app subscribes to.

On ntfy.sh a topic name is effectively its password, so both names are long
and random, and inbox messages must also carry a separate secret token.
"""
import hmac
import json
import logging
import os
import threading
import time

import httpx

from app import intake
from app.config import get_ntfy_settings, save_ntfy_settings

log = logging.getLogger(__name__)

MAX_BYTES = 3800  # ntfy turns messages over 4,096 bytes into attachments

_started = False


def ready() -> bool:
    s = get_ntfy_settings()
    return bool(s["inbox_topic"] and s["results_topic"] and s["token"])


# ---------------- outgoing notifications ----------------

def notify(title: str, message: str, click: str | None = None, tags: list[str] | None = None,
           actions: list[dict] | None = None, http: httpx.Client | None = None) -> bool:
    """Publish to the results topic as JSON (handles emoji in titles, unlike headers)."""
    s = get_ntfy_settings()
    if not s["results_topic"]:
        return False
    body = {"topic": s["results_topic"], "title": title[:200], "message": _clip(message), "markdown": True}
    if click:
        body["click"] = click
    if tags:
        body["tags"] = tags
    if actions:
        body["actions"] = actions
    client = http or httpx.Client(timeout=15)
    try:
        client.post(s["server"], json=body).raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("ntfy notify failed: %s", e)
        return False
    finally:
        if http is None:
            client.close()


def _clip(text: str) -> str:
    data = text.encode()
    return text if len(data) <= MAX_BYTES else data[:MAX_BYTES].decode(errors="ignore") + "…"


def deep_signature(action_id: int) -> str:
    return hmac.new(get_ntfy_settings()["token"].encode(), f"deep:{int(action_id)}".encode(), "sha256").hexdigest()


def result_notification(action, video) -> dict:
    """Title/message/click/actions for a finished (or stuck) report."""
    from app.telegram_bot import TYPE_LABELS, VERDICT_LABELS, fit_line, report_link

    s = get_ntfy_settings()
    click = report_link(action) or (video.tiktok_url if video.tiktok_url.startswith("http") else None)
    if action.status.value == "drafted" and action.result:
        r = json.loads(action.result)
        lines = [r.get("telegram_text", "")]
        fit = fit_line(r)
        if fit:
            lines += ["", f"🧩 {fit}"]
        if r.get("ideas"):
            lines += [""] + [f"- {i}" for i in r["ideas"][:3]]
        if not report_link(action):
            lines += ["", "_Full report on the dashboard's Agent page._"]
        # Signed for this one action, so a notification (or a screenshot of one) can't be
        # used to submit arbitrary shares the way the raw inbox token could.
        deep = {"action": "http", "label": "Research deeper", "url": f"{s['server']}/{s['inbox_topic']}",
                "method": "POST", "body": json.dumps({"v": 1, "deep": action.id, "sig": deep_signature(action.id)}),
                "clear": True}
        title = (f"{VERDICT_LABELS.get(action.verdict, '')} · {TYPE_LABELS.get(action.action_type.value, '')}: "
                 f"{r.get('headline', '')}")
        return {"title": title, "message": "\n".join(lines), "click": click, "actions": [deep]}
    reason = action.error_message or "Something went wrong."
    return {"title": "⚠️ Couldn't finish researching this", "message": reason, "click": click}


class NtfyReporter(intake.Reporter):
    """One "got it" ping on arrival and one result notification; no progress spam."""

    def received(self) -> None:
        notify("📥 Got it", "Researching now. You'll get the result here in a few minutes.", tags=["inbox_tray"])

    def finished(self, action, video) -> None:
        notify(**result_notification(action, video))

    def failed(self, text: str) -> None:
        notify("⚠️ Couldn't process that video", text)


# ---------------- incoming ----------------

def handle_message(raw: str, submit=None, attachment: dict | None = None, http: httpx.Client | None = None) -> str:
    """Route one inbox message. Returns what happened, for logs and tests:
    'ignored', 'queued', 'deep', 'busy' or 'empty'."""
    submit = submit or (lambda video_id, deep=False: intake.submit(video_id, NtfyReporter(), deep=deep))
    s = get_ntfy_settings()
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return "ignored"
    if not isinstance(body, dict) or not s["token"]:
        return "ignored"
    if body.get("deep") is not None:  # the notification's "Research deeper" button
        try:
            action_id = int(body["deep"])
        except (TypeError, ValueError):
            return "ignored"
        if not hmac.compare_digest(str(body.get("sig", "")), deep_signature(action_id)):
            return "ignored"
        return "deep" if intake.rerun(action_id, deep=True) else "busy"
    if not hmac.compare_digest(str(body.get("token", "")), s["token"]):
        return "ignored"
    if attachment:
        data = _download_attachment(attachment, s["server"], http)
        if data is None:
            notify("⚠️ Couldn't read that image", "Send a JPEG or PNG under 15 MB.")
            return "ignored"
        video_id = intake.ingest_shared_image(data, attachment.get("name", ""), str(body.get("note") or "").strip())
    else:
        shared = str(body.get("url") or body.get("text") or "").strip()
        url, note = intake.extract_link(shared)
        note = str(body.get("note") or "").strip() or note
        if url:
            video_id = intake.ingest_shared_link(url, note)
        elif shared:
            video_id = intake.ingest_shared_text(shared)
        else:
            notify("⚠️ Nothing to research", "Share a video, link, screenshot or text with Send to Sidequest.")
            return "empty"
    NtfyReporter().received()
    return "queued" if submit(video_id) is not False else "busy"


def _download_attachment(att: dict, server: str, http: httpx.Client | None = None) -> bytes | None:
    """Fetch an image attachment, only from our own ntfy server and only if it's small and an image."""
    url = str(att.get("url") or "")
    if not url.startswith(server + "/") or int(att.get("size") or 0) > intake.MAX_IMAGE_BYTES:
        return None
    if not (str(att.get("type") or "").startswith("image/")
            or any(str(att.get("name") or "").lower().endswith(x) for x in intake.IMAGE_SUFFIXES)):
        return None
    client = http or httpx.Client(timeout=30, follow_redirects=True)
    try:
        r = client.get(url)
        r.raise_for_status()
        return r.content if len(r.content) <= intake.MAX_IMAGE_BYTES else None
    except httpx.HTTPError as e:
        log.warning("ntfy attachment download failed: %s", e)
        return None
    finally:
        if http is None:
            client.close()


def _stream_forever() -> None:
    while True:
        s = get_ntfy_settings()
        if not ready():
            time.sleep(5)
            continue
        since = s["since"] or str(int(time.time()))  # first run: only messages from now on
        url = f"{s['server']}/{s['inbox_topic']}/json"
        try:
            with httpx.stream("GET", url, params={"since": since}, timeout=httpx.Timeout(10, read=90)) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("event") != "message":
                        continue  # open / keepalive
                    try:
                        log.info("Send to Sidequest: %s", handle_message(event.get("message", ""),
                                                                     attachment=event.get("attachment")))
                    except Exception:  # noqa: BLE001 - one bad message must not stop the doorway
                        log.exception("Failed to handle ntfy message %s", event.get("id"))
                    save_ntfy_settings(since=event["id"])  # after handling: a crash replays it
                    if get_ntfy_settings()["inbox_topic"] != s["inbox_topic"]:
                        break  # topics regenerated in Settings: reconnect
        except (httpx.HTTPError, ValueError) as e:
            log.warning("ntfy stream dropped (%s); reconnecting", e)
            time.sleep(5)


def start() -> None:
    global _started
    if _started or os.environ.get("SVI_DISABLE_TELEGRAM"):  # same switch: no background polling in tests
        return
    _started = True
    threading.Thread(target=_stream_forever, daemon=True, name="ntfy-inbox").start()
