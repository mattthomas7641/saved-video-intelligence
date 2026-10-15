import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app import gemini_client, intake, ntfy_intake, projects, telegram_bot, xai_client
from app.config import (
    BASE_DIR,
    GROUPS,
    SecretsUnreadable,
    get_action_settings,
    get_agent_settings,
    get_api_key,
    get_gemini_key,
    get_ntfy_settings,
    get_project_overrides,
    get_telegram_settings,
    get_xai_key,
    group_of,
    new_ntfy_setup,
    new_pairing_code,
    save_action_settings,
    save_agent_settings,
    save_api_key,
    save_gemini_key,
    save_project_override,
    save_telegram_settings,
    save_xai_key,
)
from app.db import get_db, init_db
from app.models import Action, ActionStatus, Status, Video
from app.schemas import (
    HealthResponse,
)
from app.security import GuardMiddleware
from app.services import dashboard_query

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Telegram/ntfy poll is noise
app = FastAPI(title="Sidequest")
app.add_middleware(GuardMiddleware)  # no login: refuse other hosts and cross-site writes (app/security.py)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
LOCAL_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/New_York"))
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.exception_handler(SecretsUnreadable)
def _secrets_unreadable(request: Request, exc: SecretsUnreadable):
    """Every Settings save goes through data/secrets.json; if it's corrupt, say so plainly
    (and what to do) rather than a bare 500."""
    logging.getLogger(__name__).error("%s", exc)
    return JSONResponse(status_code=500, content={"detail": f"Settings couldn't be saved: {exc} Nothing was changed."})


def _split_summary(text: str | None) -> tuple[str, str]:
    """First sentence becomes the headline; everything after it is the body."""
    text = (text or "").strip()
    if not text:
        return "", ""
    for i, ch in enumerate(text):
        if ch in ".!?" and i >= 20 and (i + 1 == len(text) or text[i + 1] == " "):
            return text[: i + 1], text[i + 2:].strip()
    return text, ""


templates.env.filters["headline"] = lambda t: _split_summary(t)[0]
templates.env.filters["rest"] = lambda t: _split_summary(t)[1]
_CAT_ICONS = {
    "Tech / AI / Coding": "code", "Career / Job Search": "briefcase",
    "Finance / Money / Deals": "dollar", "Business / Side Hustle": "trending",
    "Education / Learning": "cap", "Book / Media Recommendation": "book",
    "Motivation / Advice": "bulb", "News / Commentary": "news",
    "Recipe / Cooking": "utensils", "Home / DIY / Life Hack": "wrench",
    "Fitness / Health": "heart", "Beauty / Fashion": "sparkles",
    "Travel": "plane", "Product Review / Shopping": "bag",
    "Comedy / Entertainment": "smile", "Sports / Gaming": "trophy",
    "Music / Art / Creative": "music", "Other": "grid",
}
_GROUP_ICONS = {
    "Tech & Career": "code", "Money & Business": "dollar", "Learning & Ideas": "bulb",
    "Food & Home": "home", "Health & Style": "heart", "Travel & Shopping": "plane",
    "Fun & Culture": "smile", "Other": "grid",
}
_GROUP_SLUGS = {
    "Tech & Career": "tech", "Money & Business": "money", "Learning & Ideas": "learn",
    "Food & Home": "home", "Health & Style": "health", "Travel & Shopping": "travel",
    "Fun & Culture": "fun", "Other": "other",
}
templates.env.filters["cat_icon"] = lambda c: _CAT_ICONS.get(c, "grid")
templates.env.filters["group_icon"] = lambda g: _GROUP_ICONS.get(g, "grid")
templates.env.filters["gslug"] = lambda g: _GROUP_SLUGS.get(g, "other")
templates.env.filters["cat_slug"] = lambda c: _GROUP_SLUGS.get(group_of(c), "other")
def _local(d: datetime) -> datetime:
    """Stored times are naive UTC; show them in the owner's timezone (APP_TIMEZONE)."""
    return d.replace(tzinfo=UTC).astimezone(LOCAL_TZ)


def _short_date(d: datetime | None) -> str:
    if not d:
        return "—"
    d = _local(d)
    today = datetime.now(LOCAL_TZ).date()
    if d.date() == today:
        return "Today"
    if (today - d.date()).days == 1:
        return "Yesterday"
    return d.strftime("%b %-d") if d.year == today.year else d.strftime("%b %-d, %Y")


def _strip_citations(text: str | None) -> str:
    """Grounded answers carry markers like [1], [2.3] or [1, 4]: noise in a report."""
    return re.sub(r"\s?\[\d+(?:\.\d+)*(?:,\s*\d+(?:\.\d+)*)*\](?!\()", "", text or "")


templates.env.filters["short_date"] = _short_date
templates.env.filters["nocite"] = _strip_citations
templates.env.filters["fmt_date"] = lambda d: d.strftime("%b %-d, %Y") if d else "date unknown"


def _render_markdown(text: str | None) -> str:
    """Agent reports are model output built partly from web pages: escape any raw
    HTML first, render Markdown, then drop links that aren't plain http(s)."""
    import html as _html
    import re

    import markdown
    from markupsafe import Markup

    text = _strip_citations(text)
    # Models often start a list right under a paragraph line; Markdown needs a blank line there.
    text = re.sub(r"(?m)^(?!\s*(?:[-*+]|\d+\.)\s)(.+)\n(\s*(?:[-*+]|\d+\.)\s)", r"\1\n\n\2", text or "")
    rendered = markdown.markdown(_html.escape(text, quote=False), extensions=["sane_lists"])
    # No images from model output: a Markdown image makes the browser fetch a URL the
    # model chose, which prompt-injected content could use to smuggle data out.
    rendered = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r"\1", rendered)
    rendered = re.sub(r"<img[^>]*>", "", rendered)
    rendered = re.sub(r'href="(?!https?://)[^"]*"', 'href="#"', rendered)
    # Report headings sit under the card's headline (an h3): keep them visually below it.
    rendered = re.sub(r"<(/?)h[1-4]>", r"<\1h5>", rendered)
    rendered = rendered.replace("<a href=", '<a target="_blank" rel="noopener" href=')
    return Markup(rendered)


templates.env.filters["markdown"] = _render_markdown
# Cache-buster for the stylesheet: browsers otherwise keep serving an old app.css after an update.
templates.env.globals["asset_version"] = hashlib.sha256((BASE_DIR / "static" / "app.css").read_bytes()).hexdigest()[:10]

init_db()
try:  # tidying after a restart must not stop the app from starting (the home page still shows the items)
    intake.recover_interrupted()
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).exception("Startup recovery failed")
telegram_bot.start_poller()
ntfy_intake.start()
if not os.environ.get("SVI_DISABLE_TELEGRAM"):  # no background work in tests
    projects.start_rescans()


@app.get("/")
def root(request: Request, session: Session = Depends(get_db), tab: str = "todo", group: str = ""):
    """Home: the things you sent, sorted into topic groups, each ready to act on."""
    return quests_page(request, session, tab, group)


@app.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_db), tab: str = "processed", group: str = "",
              category: str = "", theme: str = "", q: str = "", sort: str = "score", show: str = "",
              page: int = 1):
    """The Archive: the older TikTok saves analyzed before Sidequest, read-only."""
    context = dashboard_query.build_dashboard_context(
        session, tab=tab, group=group, category=category, theme=theme, q=q, sort=sort, show=show, page=page,
    )
    return templates.TemplateResponse(request, "dashboard.html", {
        **context,
        "next_url": request.url.path + ("?" + request.url.query if request.url.query else ""),
    })


@app.get("/video/{video_id}")
def video_detail(request: Request, video_id: int, session: Session = Depends(get_db)):
    video = session.get(Video, video_id)
    return templates.TemplateResponse(request, "video_detail.html", {"video": video})


@app.post("/video/{video_id}/watched")
def toggle_watched(video_id: int, session: Session = Depends(get_db), next: str = Form("")):
    video = _video_or_404(session, video_id)
    video.watched = not video.watched
    session.add(video)
    session.commit()
    return RedirectResponse(next if next.startswith("/dashboard") else f"/video/{video_id}", status_code=303)


@app.post("/video/{video_id}/archive")
def toggle_archive(video_id: int, session: Session = Depends(get_db), next: str = Form("")):
    video = _video_or_404(session, video_id)
    video.archived = not video.archived
    session.add(video)
    session.commit()
    return RedirectResponse(next if next.startswith("/dashboard") else f"/video/{video_id}", status_code=303)


@app.post("/video/{video_id}/reprocess")
def reprocess(video_id: int, session: Session = Depends(get_db)):
    video = _video_or_404(session, video_id)
    video.status = Status.PENDING
    video.error_message = None
    session.add(video)
    session.commit()
    return RedirectResponse(f"/video/{video_id}", status_code=303)


def _video_or_404(session: Session, video_id: int) -> Video:
    video = session.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="No such video.")
    return video


def _optional_money(text: str) -> float | None:
    try:
        value = float(text.strip().lstrip("$"))
    except ValueError:
        return None
    return value if value > 0 else None


@app.get("/health", response_model=HealthResponse)
def health():
    return {"ok": True}


# ---------------- Sidequests (things you sent) ----------------
AGENT_TYPES = ["repo", "research", "place", "project", "skill", "job", "other"]


TYPE_LABELS = {"repo": "Repos", "research": "Research", "place": "Place", "project": "Project",
               "skill": "Skill", "job": "Job", "other": "Other"}
QUEST_TABS = [("todo", "To do"), ("fyi", "Nothing to do"), ("done", "Done"), ("skipped", "Not for me")]


@app.get("/agent")
def agent_page(request: Request, session: Session = Depends(get_db), tab: str = "todo", group: str = ""):
    """Old address of the home feed; kept so links in past notifications still work."""
    return quests_page(request, session, tab, group)


def quests_page(request: Request, session: Session, tab: str, group: str):
    from app.agent import load_report
    from app.pipeline import agent_spend_today

    rows = session.exec(
        select(Action, Video).join(Video, Video.id == Action.video_id)
        .where((Video.source == "telegram") | (Action.result.is_not(None) & Action.verdict.is_not(None)))
        .order_by(Action.updated_at.desc())).all()
    items = []
    for a, v in rows:
        # load_report coerces or rejects whatever is stored, so one bad result can't 500 the page.
        report = load_report(a.result) if a.result and a.verdict else None
        why = a.error_message
        if report is None and a.status in (ActionStatus.DRAFTED, ActionStatus.DONE):
            why = "The saved report can't be read. Tap Retry to research it again."
        g = group_of(v.category) if v.category else "Other"
        items.append({"action": a, "video": v, "report": report, "why": why, "group": g,
                      "slug": _GROUP_SLUGS.get(g, "other")})

    def bucket(i):
        st = i["action"].status
        if st == ActionStatus.DONE:
            return "done"
        if st == ActionStatus.DISMISSED:
            return "skipped"
        if i["report"] and i["action"].verdict == "skip":
            return "fyi"  # researched, nothing to act on: kept, but out of the To do list
        return "todo"

    tab = tab if tab in dict(QUEST_TABS) else "todo"
    tab_counts = {t: sum(1 for i in items if bucket(i) == t) for t, _ in QUEST_TABS}
    in_tab = [i for i in items if bucket(i) == tab]
    group_counts = {}
    for i in in_tab:
        group_counts[i["group"]] = group_counts.get(i["group"], 0) + 1
    groups = [(g, _GROUP_SLUGS.get(g, "other"), group_counts[g]) for g, _ in GROUPS if g in group_counts]
    shown = [i for i in in_tab if not group or i["slug"] == group]
    # A QUEUED item with a message was parked on purpose (agent paused): it isn't being worked
    # on, so it goes with the items that need you, not under "Looking into it" (which refreshes).
    parked = lambda a: a.status == ActionStatus.QUEUED and bool(a.error_message)  # noqa: E731
    working = [i for i in shown if not i["report"] and not parked(i["action"])
               and i["action"].status in (ActionStatus.IN_PROGRESS, ActionStatus.QUEUED)]
    # A finished item whose stored report is unreadable is stuck too: it gets the Retry button.
    stuck = [i for i in shown if not i["report"] and (parked(i["action"])
             or i["action"].status in (ActionStatus.FAILED, ActionStatus.NEEDS_INPUT, ActionStatus.DRAFTED))]
    reports = [i for i in shown if i["report"]]
    tg = get_telegram_settings()
    return templates.TemplateResponse(request, "agent.html", {
        "tabs": QUEST_TABS, "tab": tab, "tab_counts": tab_counts, "groups": groups, "group": group,
        "type_labels": TYPE_LABELS,
        "working": working, "stuck": stuck, "done": reports, "total": len(items),
        "spent_today": agent_spend_today(session), "agent_settings": get_agent_settings(),
        "paused": get_action_settings()["agent_paused"],
        "intake_ready": ntfy_intake.ready() or bool(tg["bot_token"] and tg["chat_id"]),
        "has_research_key": _has_research_key(),
        "project_status": {p.name: p.status for p in projects.cards(session)},
    })


def _has_research_key() -> bool:
    provider = get_agent_settings()["provider"]
    return bool({"gemini": get_gemini_key, "xai": get_xai_key}.get(provider, get_api_key)())


def _set_status(session: Session, action_id: int, status: ActionStatus) -> None:
    action = session.get(Action, action_id)
    if action:
        action.status = status
        session.add(action)
        session.commit()


@app.get("/runs/{action_id}")
def runs_page(request: Request, action_id: int, session: Session = Depends(get_db)):
    """The trace: every paid attempt on one item, step by step."""
    from sqlalchemy import and_, or_

    from app.models import Run

    action = session.get(Action, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="No such item.")
    runs = session.exec(select(Run).where(or_(Run.action_id == action_id,
                                              and_(Run.action_id.is_(None), Run.video_id == action.video_id)))
                        .order_by(Run.started_at.desc())).all()
    def steps_of(r):
        try:
            return json.loads(r.steps or "[]")
        except ValueError:
            return []
    rows = [{**r.model_dump(), "parsed_steps": steps_of(r)} for r in runs]
    try:
        headline = json.loads(action.result or "{}").get("headline") or ""
    except ValueError:
        headline = ""
    video = session.get(Video, action.video_id)
    return templates.TemplateResponse(request, "runs.html", {
        "action": action, "runs": rows, "total": sum(r.cost_usd for r in runs),
        "headline": _strip_citations(headline) or (video.summary if video else "") or "",
    })


@app.get("/api/quests/status")
def quests_status(session: Session = Depends(get_db)):
    """What the home page polls while something is in progress: a change in `stamp`
    means a sidequest started, finished or failed."""
    from sqlalchemy import func

    working = session.exec(select(func.count()).select_from(Action)
                           .where(Action.status.in_([ActionStatus.IN_PROGRESS, ActionStatus.QUEUED]))
                           .where(Action.error_message.is_(None))).one()
    latest = session.exec(select(func.max(Action.updated_at))).one()
    return {"working": working, "stamp": f"{latest}|{working}"}


@app.post("/agent/{action_id}/done")
def agent_done(action_id: int, session: Session = Depends(get_db), next: str = Form("/")):
    _set_status(session, action_id, ActionStatus.DONE)
    return RedirectResponse(next if next.startswith("/") else "/", status_code=303)


@app.post("/agent/{action_id}/reopen")
def agent_reopen(action_id: int, session: Session = Depends(get_db), next: str = Form("/")):
    _set_status(session, action_id, ActionStatus.DRAFTED)
    return RedirectResponse(next if next.startswith("/") else "/", status_code=303)


@app.post("/agent/{action_id}/rerun")
def agent_rerun(action_id: int, deep: bool = Form(False), next: str = Form("/")):
    intake.rerun(action_id, deep=deep)
    return RedirectResponse(next if next.startswith("/") else "/", status_code=303)


@app.post("/agent/{action_id}/dismiss")
def agent_dismiss(action_id: int, session: Session = Depends(get_db), next: str = Form("/")):
    _set_status(session, action_id, ActionStatus.DISMISSED)
    return RedirectResponse(next if next.startswith("/") else "/", status_code=303)


@app.post("/settings/telegram")
def settings_telegram(token: str = Form("")):
    token = token.strip()
    if not token:
        return RedirectResponse("/settings?key=tg_empty#telegram", status_code=303)
    bot_username = telegram_bot.check_token(token)
    if not bot_username:
        return RedirectResponse("/settings?key=tg_invalid#telegram", status_code=303)
    save_telegram_settings(bot_token=token, bot_username=bot_username, chat_id=None)
    new_pairing_code()
    return RedirectResponse("/settings?key=tg_saved#telegram", status_code=303)


@app.post("/settings/telegram/pair")
def settings_telegram_pair():
    new_pairing_code()
    return RedirectResponse("/settings#telegram", status_code=303)


@app.post("/settings/agent")
def settings_agent(about_me: str = Form(""), daily_cap_usd: str = Form(""), planned_ideas: str | None = Form(None),
                   provider: str = Form("")):
    cap = _optional_money(daily_cap_usd)
    save_agent_settings(about_me=about_me.strip(), daily_cap_usd=cap,
                        planned_ideas=planned_ideas.strip() if planned_ideas is not None else None,
                        provider=provider if provider in ("gemini", "xai", "anthropic") else None)
    return RedirectResponse("/settings?key=agent_saved#research", status_code=303)


@app.post("/settings/xai")
def settings_xai(key: str = Form("")):
    key = key.strip()
    if not key or not xai_client.check_key(key):
        return RedirectResponse("/settings?key=xai_invalid#research", status_code=303)
    save_xai_key(key)
    return RedirectResponse("/settings?key=xai_saved#research", status_code=303)


@app.post("/settings/gemini")
def settings_gemini(key: str = Form("")):
    key = key.strip()
    if not key or not gemini_client.check_key(key):
        return RedirectResponse("/settings?key=gemini_invalid#research", status_code=303)
    save_gemini_key(key)
    return RedirectResponse("/settings?key=gemini_saved#research", status_code=303)


@app.post("/settings/ntfy")
def settings_ntfy():
    new_ntfy_setup()
    return RedirectResponse("/settings?key=ntfy_new#send", status_code=303)


@app.post("/settings/ntfy/test")
def settings_ntfy_test():
    ok = ntfy_intake.notify("✅ Send to Sidequest is connected", "Results for videos you share will arrive here.",
                            tags=["white_check_mark"])
    return RedirectResponse(f"/settings?key={'ntfy_sent' if ok else 'ntfy_failed'}#send", status_code=303)


@app.post("/settings/projects/rescan")
def settings_projects_rescan(session: Session = Depends(get_db)):
    projects.refresh(session)
    return RedirectResponse("/settings?key=projects_rescanned#projects", status_code=303)


@app.post("/settings/projects/{name}")
def settings_project_status(name: str, status: str = Form("")):
    save_project_override(name, status)
    return RedirectResponse("/settings#projects", status_code=303)


@app.post("/settings/pause")
def settings_pause(paused: bool = Form(False)):
    save_action_settings(agent_paused=paused)
    return RedirectResponse("/settings?key=agent_saved#research", status_code=303)


@app.post("/settings/key")
def settings_key(key: str = Form(""), next: str = Form("/settings")):
    """Validate the key with a free token-count call, then store it in data/secrets.json."""
    import anthropic

    from app.config import ANALYSIS_MODEL
    key = key.strip()
    dest = next if next.startswith("/") else "/settings"
    sep = "&" if "?" in dest else "?"
    if not key:
        return RedirectResponse(f"{dest}{sep}key=empty", status_code=303)
    try:
        anthropic.Anthropic(api_key=key).messages.count_tokens(
            model=ANALYSIS_MODEL, messages=[{"role": "user", "content": "hi"}])
    except anthropic.AuthenticationError:
        return RedirectResponse(f"{dest}{sep}key=invalid", status_code=303)
    except Exception:  # noqa: BLE001 - network hiccup: save anyway, real errors surface on analysis
        pass
    save_api_key(key)
    return RedirectResponse(f"{dest}{sep}key=saved", status_code=303)


@app.get("/settings")
def settings_page(request: Request, key: str = ""):
    return templates.TemplateResponse(request, "settings.html", {
        "key_status": key,
        "has_api_key": bool(get_api_key()),
        "action_settings": get_action_settings(),
        "telegram": get_telegram_settings(),
        "agent_settings": get_agent_settings(),
        "has_xai_key": bool(get_xai_key()),
        "has_gemini_key": bool(get_gemini_key()),
        "ntfy": get_ntfy_settings(),
        "projects": _projects_for_settings(),
        "spend_days": _spend_days(),
        "project_overrides": get_project_overrides(),
    })


def _spend_days() -> list:
    from app import tracing
    from app.db import get_session

    with get_session() as session:
        return tracing.spend_by_day(session)


def _projects_for_settings() -> list:
    from app.db import get_session
    from app.models import Project

    with get_session() as session:
        return session.exec(select(Project).order_by(Project.name)).all()


@app.get("/media/thumb/{video_id}")
def media_thumb(video_id: int, session: Session = Depends(get_db)):
    video = session.get(Video, video_id)
    if video and video.thumbnail_path and Path(video.thumbnail_path).exists():
        return FileResponse(video.thumbnail_path, media_type="image/jpeg")
    return FileResponse(str(BASE_DIR / "static" / "placeholder.svg"))
