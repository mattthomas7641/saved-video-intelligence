"""Per-video stages. Collect (download, transcribe, OCR) is free; analyze is the paid Claude call."""
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app import analyze as analyze_mod
from app import download as download_mod
from app import memory as memory_mod
from app import ocr as ocr_mod
from app import relevance as relevance_mod
from app import tracing
from app import transcribe as transcribe_mod
from app.config import DELETE_VIDEO_AFTER_PROCESS
from app.models import Action, ActionStatus, ActionType, Status, Video
from app.netutil import user_message
from app.throttle import looks_like_throttle, throttle

log = logging.getLogger("pipeline")


class PipelineError(RuntimeError):
    """A step failed in a way already put into plain words for the owner's card."""


def collect_video(session: Session, video: Video, should_stop=lambda: False, transcribe: bool = True) -> None:
    """Download, transcribe and read on-screen text. Leaves the video TRANSCRIBED.
    With `transcribe=False` (Gemini watches the file itself) it only downloads,
    and keeps the file for the analysis step, which deletes it afterwards."""
    try:
        throttle.wait(should_stop)
        time.sleep(random.uniform(0.2, 0.9))  # stay polite when several workers run at once
        video.status = Status.DOWNLOADING
        _touch(session, video)

        result = download_mod.download_video(video.tiktok_url, video.id)
        throttle.report_ok()
        video.local_video_path = result.video_path
        video.thumbnail_path = result.thumbnail_path
        video.author = result.author
        video.caption = result.caption
        video.hashtags = result.hashtags
        video.upload_date = result.upload_date
        video.duration_seconds = result.duration_seconds

        if not transcribe and video.local_video_path and Path(video.local_video_path).exists():
            video.transcript = video.transcript or ""
            video.ocr_text = video.ocr_text or ""
            video.status = Status.TRANSCRIBED
            video.error_message = None
            _touch(session, video)
            return
        if video.local_video_path and Path(video.local_video_path).exists():
            video.status = Status.TRANSCRIBING
            _touch(session, video)
            video.transcript = transcribe_mod.transcribe(video.local_video_path)
            try:
                video.ocr_text = ocr_mod.extract_on_screen_text(video.local_video_path, video.duration_seconds)
            except Exception as e:  # noqa: BLE001 - OCR is best effort
                log.warning("OCR failed for video %s: %s", video.id, e)
                video.ocr_text = ""
        else:
            video.transcript = ""  # photo/slideshow post: analyze the caption alone
            video.ocr_text = ""

        video.status = Status.TRANSCRIBED
        video.error_message = None
        if DELETE_VIDEO_AFTER_PROCESS and video.local_video_path:
            Path(video.local_video_path).unlink(missing_ok=True)
            video.local_video_path = None
        _touch(session, video)

    except Exception as e:  # noqa: BLE001
        message = str(e)
        if looks_like_throttle(message):
            throttle.report_block()
            video.status = Status.PENDING  # try again later, not a permanent failure
            video.error_message = "TikTok slowed us down; will retry."
        else:
            log.exception("Collect failed for video %s", video.id)
            video.status = Status.ERROR
            video.error_message = message[:500]
        download_mod.discard_files(video.id)  # half-downloaded .part files, or a file we can't use
        video.local_video_path = None
        _touch(session, video)


def _action_type(value) -> ActionType:
    """The model's action_type as our enum; anything off the list is OTHER, not a crash."""
    try:
        return ActionType(value)
    except ValueError:
        log.warning("Unknown action_type %r from analysis; using other", value)
        return ActionType.OTHER


def _queue_action_if_new(session: Session, video: Video, analysis: analyze_mod.AnalysisResult) -> None:
    """Idempotent: one Action per video, created the first time analysis flags it actionable."""
    if not analysis.is_actionable:
        return
    existing = session.exec(select(Action).where(Action.video_id == video.id)).first()
    if existing:
        if existing.brief is None and existing.action_type == ActionType.OTHER:
            # The placeholder intake creates so a shared item shows up at once: fill it in now.
            existing.action_type = _action_type(analysis.action_type)
            existing.brief = analysis.action_brief or ""
            session.add(existing)
        return
    session.add(Action(
        video_id=video.id,
        action_type=_action_type(analysis.action_type),
        status=ActionStatus.QUEUED,
        brief=analysis.action_brief,
    ))


def apply_analysis(session: Session, video: Video, analysis: analyze_mod.AnalysisResult, batch: bool = False) -> float:
    video.category = analysis.category
    video.summary = analysis.summary
    video.tags = ",".join(analysis.tags)
    video.worth_rewatching_score = analysis.worth_rewatching_score
    video.worth_rewatching_reason = analysis.worth_rewatching_reason
    video.has_promo_code = analysis.has_promo_code
    video.promo_code_text = analysis.promo_code_text
    video.has_dated_offer = analysis.has_dated_offer
    video.offer_deadline_text = analysis.offer_deadline_text
    video.mentions_link_in_bio = analysis.mentions_link_in_bio
    video.key_facts = ",".join(analysis.key_facts)
    video.needs_verification = relevance_mod.is_stale(
        saved_date=video.saved_date, upload_date=video.upload_date,
        has_promo_code=video.has_promo_code, has_dated_offer=video.has_dated_offer)
    video.input_tokens = analysis.input_tokens
    video.output_tokens = analysis.output_tokens
    own_cost = getattr(analysis, "cost_usd", None)  # set by analyzers with their own pricing (Gemini, Grok)
    video.cost_usd = own_cost if own_cost is not None else analyze_mod.cost_usd(
        analysis.input_tokens, analysis.output_tokens, batch=batch)
    video.batch_id = None
    video.status = Status.DONE
    video.error_message = None
    _queue_action_if_new(session, video, analysis)
    return video.cost_usd


def analyze_video(session: Session, video: Video, analyzer=None) -> float:
    """Live analysis of a TRANSCRIBED video - Claude by default, or `analyzer`
    (e.g. Gemini for shared videos). Returns the cost in dollars."""
    video.status = Status.ANALYZING
    _touch(session, video)
    try:
        cost = apply_analysis(session, video, (analyzer or analyze_mod.analyze_video)(video))
    except analyze_mod.FatalAnalysisError:
        video.status = Status.TRANSCRIBED  # nothing is wrong with the video; keep it for later
        _touch(session, video)
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("Analysis failed for video %s", video.id)
        video.status = Status.TRANSCRIBED
        video.error_message = ("Analysis failed: " + str(e))[:500]
        _touch(session, video)
        return 0.0
    _touch(session, video)
    return cost



def _touch(session: Session, video: Video) -> None:
    video.updated_at = datetime.utcnow()
    session.add(video)
    session.commit()
    session.refresh(video)


# ---------------- shared-video fast lane (Telegram) ----------------
# A video you deliberately shared gets the whole path at once - collect, analyze,
# research - on its own thread (see app/telegram_bot.py), independent of any bulk
# job running in worker.py. Bulk/export videos never reach the research agent.

def agent_spend_today(session: Session) -> float:
    """Everything spent since local midnight, from the run ledger: research, watching,
    failed attempts and project cards alike."""
    return tracing.spend_today(session)


def _action_for(session: Session, video: Video) -> Action:
    action = session.exec(select(Action).where(Action.video_id == video.id)).first()
    if action:
        if action.brief is None and action.action_type == ActionType.OTHER:
            # Analysis ran but didn't flag it actionable: brief the agent from the summary, so
            # a Retry doesn't pay for the analysis again.
            action.brief = video.summary or ""
            session.add(action)
        return action
    # Analysis is told shared videos are always actionable, but don't depend on it.
    action = Action(video_id=video.id, action_type=ActionType.OTHER, status=ActionStatus.QUEUED,
                    brief=video.summary or "")
    session.add(action)
    session.commit()
    session.refresh(action)
    return action


def process_shared_video(session: Session, video: Video, on_progress=lambda text: None,
                         chat_id: int | None = None, message_id: int | None = None, deep: bool = False) -> Action:
    """Download -> watch/analyze -> prefetch -> research for one shared item.
    Returns its Action, whose status/result/error_message say how it went. Raises
    FatalAnalysisError for problems only you can fix (no key, no credit).
    `deep` forces web research on (the "Research deeper" button)."""
    from app import agent as agent_mod
    from app import prefetch as prefetch_mod
    from app import projects as projects_mod
    from app.config import get_action_settings, get_agent_settings

    provider = get_agent_settings()["provider"]
    download_failed = False
    analyzer = None
    if provider == "gemini":
        from app import gemini_client
        analyzer = gemini_client.analyze_video  # watches the video file itself: no Whisper/OCR
    elif provider == "xai":
        from app import xai_client
        analyzer = xai_client.analyze_video  # shared videos never need Anthropic credit

    if video.status in (Status.PENDING, Status.ERROR, Status.DOWNLOADING, Status.TRANSCRIBING, Status.DOWNLOADED):
        on_progress("Downloading…" if provider == "gemini" else "Downloading and transcribing…")
        collect_video(session, video, transcribe=provider != "gemini")
        if provider == "gemini" and video.status == Status.TRANSCRIBED and not gemini_client.can_watch(video):
            on_progress("Too long to send whole; transcribing…")  # rare: over the inline size limit
            transcribe_existing(session, video)
        if video.status != Status.TRANSCRIBED:
            if not (video.tiktok_url or "").startswith("http"):
                raise PipelineError("Couldn't get this ready to analyze. Tap Retry.")
            # Blocked or private (common on Instagram): judge it from the link instead of failing.
            log.warning("Download failed for shared video %s (%s); using the link", video.id, video.error_message)
            download_failed = True
            on_progress("Couldn't download it; working from the link…")
            video.status = Status.TRANSCRIBED
            video.caption = video.caption or video.tiktok_url
            video.transcript = video.transcript or ""
            video.ocr_text = video.ocr_text or ""
            video.error_message = None
            _touch(session, video)

    analysis_cost = 0.0
    action_row = session.exec(select(Action).where(Action.video_id == video.id)).first()
    needs_analysis = action_row is None or (action_row.brief is None and action_row.action_type == ActionType.OTHER)
    if video.status in (Status.TRANSCRIBED, Status.ANALYZING, Status.SUBMITTED) or (
            video.status == Status.DONE and needs_analysis):
        on_progress("Watching the video…" if provider == "gemini" and video.local_video_path else "Reading it…")
        had_file = bool(video.local_video_path)
        try:
            with tracing.run("analysis", action_id=action_row.id if action_row else None, video_id=video.id,
                             provider=provider) as run:
                analysis_cost = analyze_video(session, video, analyzer)
                if video.status != Status.DONE:
                    run.fail(video.error_message or "Analysis failed.")
        finally:
            _drop_video_file(session, video)  # whatever happened, the file isn't kept around
            if video.status != Status.DONE and had_file:
                video.status = Status.PENDING  # the file is gone: Retry downloads it again (free)
                _touch(session, video)
        if video.status != Status.DONE:
            raise PipelineError("Couldn't analyze this (details are in the app log). Tap Retry.")
    elif video.status == Status.DONE:
        _drop_video_file(session, video)  # e.g. a restart right after analysis left the file behind

    action = _action_for(session, video)
    action.telegram_chat_id = chat_id or action.telegram_chat_id
    action.telegram_message_id = message_id or action.telegram_message_id

    if get_action_settings()["agent_paused"]:
        action.status = ActionStatus.QUEUED
        action.error_message = "Research agent is paused in Settings."
        _touch_action(session, action)
        return action

    budget = get_agent_settings()["daily_cap_usd"] - agent_spend_today(session)  # analysis is already in the ledger
    action.status = ActionStatus.IN_PROGRESS
    action.error_message = None
    _touch_action(session, action)
    action_type = action.action_type.value
    plan_cost = 0.0
    try:
        on_progress("Looking up links and repos…")
        try:
            found = prefetch_mod.gather(video)
        except Exception as e:  # noqa: BLE001 - free legwork; the agent can look things up itself
            log.warning("Prefetch failed for video %s: %s", video.id, e)
            found = {"repos": [], "pages": []}
        # Only tech content is judged against your projects; food, products, fitness etc. get a general review.
        tech = agent_mod.is_tech(action_type, video.category)
        cards = projects_mod.cards(session) if tech else []
        web, x_search = research_plan(provider, action_type, deep)
        context = agent_mod.Context(projects=agent_mod.projects_context(cards, [c.name for c in cards]) if tech else "",
                                    prefetch=prefetch_mod.as_context(found))
        on_progress(f"Researching ({action_type}{', with web search' if web else ''})…")
        with tracing.run("research", action_id=action.id, video_id=video.id, provider=provider):
            related = _recall(session, video, action)
            context.memory = memory_mod.context_block(related)
            findings = agent_mod.run(video, action_type, action.brief, budget - plan_cost, on_progress=on_progress,
                                     web=web, x_search=x_search, context=context, provider=provider, tech=tech)
    except agent_mod.CapReached as e:
        action.status = ActionStatus.NEEDS_INPUT
        action.error_message = f"{e} Raise the daily cap in Settings, then re-run it from the Agent page."
    except agent_mod.AgentError as e:  # our own, readable messages
        action.status = ActionStatus.FAILED
        action.error_message = str(e)[:500]
    except analyze_mod.FatalAnalysisError as e:
        # Only you can fix this (no key, no credit): park it where the home page shows why.
        action.status = ActionStatus.NEEDS_INPUT
        action.error_message = f"{e} Then tap Retry."[:500]
        _touch_action(session, action)
        raise
    except Exception as e:  # noqa: BLE001 - report it on the card and in Telegram, don't kill the lane
        action.status = ActionStatus.FAILED
        if type(e).__name__ == "Throttled":  # gemini_client.Throttled, after its retries
            action.error_message = ("Gemini is rate limiting this key. Turning on billing at aistudio.google.com "
                                    "raises the limits; then tap Retry.")
        else:
            log.exception("Research failed for video %s", video.id)
            action.error_message = user_message(e)
    else:
        if download_failed:
            findings.report_markdown = ("> Couldn't download this video (the site blocked it), so this was judged "
                                        "from the link and caption only.\n\n" + findings.report_markdown)
        findings.related = [{"action_id": r["action_id"], "headline": r["headline"], "date": r["date"]}
                            for r in related]
        action.status = ActionStatus.DRAFTED
        action.verdict = findings.verdict
        _ = analysis_cost  # recorded in the ledger by its run
        action.result = findings.to_json()
    _touch_action(session, action)
    if action.status == ActionStatus.DRAFTED:
        _remember(session, action, video)
    return action


def _recall(session: Session, video: Video, action: Action) -> list[dict]:
    """Related past reports for this item, or [] - memory must never fail an item."""
    from app import memory as memory_mod

    if not memory_mod.enabled():
        return []
    try:
        query = "\n".join(x for x in [action.brief, video.summary, getattr(video, "user_note", None)] if x)
        return memory_mod.recall(session, query, exclude_action_id=action.id)
    except Exception:  # noqa: BLE001
        log.exception("Memory recall failed for video %s; researching without it", video.id)
        return []


def _remember(session: Session, action: Action, video: Video) -> None:
    from app import memory as memory_mod

    if not memory_mod.enabled():
        return
    try:
        with tracing.run("memory", action_id=action.id, video_id=video.id, provider="gemini"):
            memory_mod.remember(session, action, video, json.loads(action.result or "{}"))
    except Exception:  # noqa: BLE001
        log.exception("Couldn't add action %s to memory", action.id)


def _touch_action(session: Session, action: Action) -> None:
    if action.id is not None:
        action.cost_usd = round(tracing.action_cost(session, action.id, action.video_id), 4) or None  # every attempt
    action.updated_at = datetime.utcnow()
    session.add(action)
    session.commit()
    session.refresh(action)


_WEB_TYPES = {"place", "research", "job"}


def research_plan(provider: str, action_type: str, deep: bool = False) -> tuple[bool, bool]:
    """(web search allowed, X search allowed). Gemini's grounded search is free up to
    5,000 requests a month, so it's always on there and the model decides when to use
    it; on paid-per-search providers it's on only for types that need current facts."""
    if provider == "gemini":
        return True, False
    web = deep or action_type in _WEB_TYPES
    return web, provider == "xai" and action_type == "research" and deep


def transcribe_existing(session: Session, video: Video) -> None:
    """Whisper + OCR on the file already downloaded (no second download)."""
    path = video.local_video_path
    if path and Path(path).exists():
        video.transcript = transcribe_mod.transcribe(path)
        try:
            video.ocr_text = ocr_mod.extract_on_screen_text(path, video.duration_seconds)
        except Exception as e:  # noqa: BLE001 - OCR is best effort
            log.warning("OCR failed for video %s: %s", video.id, e)
    _drop_video_file(session, video)
    video.status = Status.TRANSCRIBED
    _touch(session, video)


def _drop_video_file(session: Session, video: Video) -> None:
    """Gemini keeps the file until it has watched it; tidy up afterwards like collect does."""
    if DELETE_VIDEO_AFTER_PROCESS and video.local_video_path:
        Path(video.local_video_path).unlink(missing_ok=True)
        video.local_video_path = None
        _touch(session, video)
