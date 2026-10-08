from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app import batch, worker
from app.config import (
    BASE_DIR,
    DEFAULT_WORKERS,
    STALE_THRESHOLD_MONTHS,
    get_action_settings,
    get_agent_token,
    get_api_key,
    get_resume_text,
    get_trusted_sender,
    group_of,
    regenerate_agent_token,
    save_action_settings,
    save_api_key,
    save_resume_text,
    save_trusted_sender,
)
from app.db import get_db, init_db
from app.ingest import insert_new_videos, parse_export
from app.models import Action, ActionStatus, ActionType, Status, Video
from app.schemas import (
    ActionsQueueResponse,
    ActionUpdateRequest,
    ActionUpdateResponse,
    BulkStatusResponse,
    HealthResponse,
    IngestLinkEntry,
    IngestLinksResponse,
    ProgressResponse,
    QueuedAction,
    SyncErrorResponse,
    SyncInboxResponse,
)
from app.services import dashboard_query

app = FastAPI(title="Saved Video Intelligence")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
templates.env.filters["short_date"] = lambda d: d.strftime("%b %-d, %y") if d else "—"
templates.env.filters["fmt_date"] = lambda d: d.strftime("%b %-d, %Y") if d else "date unknown"

init_db()
worker.recover_stuck()
batch.start_poller()


@app.get("/")
def root():
    return RedirectResponse("/dashboard")


@app.get("/onboarding")
def onboarding(request: Request):
    return templates.TemplateResponse(request, "onboarding.html", {
        "has_api_key": bool(get_api_key()),
    })


@app.post("/onboarding/upload")
async def onboarding_upload(file: UploadFile = File(...), session: Session = Depends(get_db)):
    content = await file.read()
    entries = parse_export(content)
    added = insert_new_videos(session, entries)
    return RedirectResponse(f"/dashboard?imported={added}", status_code=303)


@app.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_db), tab: str = "processed", group: str = "",
              category: str = "", theme: str = "", q: str = "", sort: str = "score", show: str = "",
              page: int = 1, imported: int | None = None):
    context = dashboard_query.build_dashboard_context(
        session, tab=tab, group=group, category=category, theme=theme, q=q, sort=sort, show=show,
        page=page, imported=imported,
    )
    key_ok = bool(get_api_key())
    setup = {
        "key": key_ok, "imported": context["stats"]["total"] > 0, "analyzed": context["stats"]["processed"] > 0,
    }
    show_setup = not (setup["key"] and setup["imported"] and setup["analyzed"])

    return templates.TemplateResponse(request, "dashboard.html", {
        **context,
        "setup": setup, "show_setup": show_setup,
        "next_url": request.url.path + ("?" + request.url.query if request.url.query else ""),
        "progress": worker.progress_summary(),
        "has_api_key": key_ok,
    })


@app.get("/video/{video_id}")
def video_detail(request: Request, video_id: int, session: Session = Depends(get_db)):
    video = session.get(Video, video_id)
    return templates.TemplateResponse(request, "video_detail.html", {"video": video})


@app.post("/video/{video_id}/watched")
def toggle_watched(video_id: int, session: Session = Depends(get_db), next: str = Form("")):
    video = session.get(Video, video_id)
    video.watched = not video.watched
    session.add(video)
    session.commit()
    return RedirectResponse(next if next.startswith("/dashboard") else f"/video/{video_id}", status_code=303)


@app.post("/video/{video_id}/archive")
def toggle_archive(video_id: int, session: Session = Depends(get_db), next: str = Form("")):
    video = session.get(Video, video_id)
    video.archived = not video.archived
    session.add(video)
    session.commit()
    return RedirectResponse(next if next.startswith("/dashboard") else f"/video/{video_id}", status_code=303)


@app.post("/video/{video_id}/reprocess")
def reprocess(video_id: int, session: Session = Depends(get_db)):
    video = session.get(Video, video_id)
    video.status = Status.PENDING
    video.error_message = None
    session.add(video)
    session.commit()
    return RedirectResponse(f"/video/{video_id}", status_code=303)


@app.post("/process/start")
def process_start(retry_errors: bool = Form(False), limit: str = Form("")):
    n = int(limit) if limit.strip().isdigit() and int(limit) > 0 else None
    worker.start_job("full", limit=n, retry_errors=retry_errors)
    return RedirectResponse("/dashboard", status_code=303)


# ---------------- bulk: analyze everything ----------------
def _bulk_counts() -> dict:
    p = worker.progress_summary()["counts"]
    return {
        "total": sum(p.values()), "done": p["done"], "collected": p["transcribed"],
        "submitted": p["submitted"], "pending": p["pending"], "errors": p["error"],
        "working": p["downloading"] + p["downloaded"] + p["transcribing"] + p["analyzing"],
    }


def _optional_int(text: str) -> int | None:
    return int(text) if text.strip().isdigit() and int(text) > 0 else None


def _optional_money(text: str) -> float | None:
    try:
        value = float(text.strip().lstrip("$"))
    except ValueError:
        return None
    return value if value > 0 else None


@app.get("/bulk")
def bulk_page(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "bulk.html", {
        "counts": _bulk_counts(), "job": worker.job_status(), "msg": msg,
        "cost_live": batch.observed_cost(False) or 0.0026, "cost_batch": batch.observed_cost(True) or batch.DEFAULT_BATCH_COST,
        "default_workers": DEFAULT_WORKERS, "has_api_key": bool(get_api_key()),
    })


@app.get("/api/bulk", response_model=BulkStatusResponse)
def api_bulk():
    return {"counts": _bulk_counts(), "job": worker.job_status()}


@app.post("/bulk/collect")
def bulk_collect(limit: str = Form(""), workers: int = Form(DEFAULT_WORKERS), retry_errors: bool = Form(False)):
    started = worker.start_job("collect", limit=_optional_int(limit), workers=workers, retry_errors=retry_errors)
    return RedirectResponse("/bulk" + ("" if started else "?msg=A+job+is+already+running."), status_code=303)


@app.post("/bulk/analyze")
def bulk_analyze(method: str = Form("batch"), limit: str = Form(""), cap: str = Form(""), workers: int = Form(DEFAULT_WORKERS)):
    n, cap_usd = _optional_int(limit), _optional_money(cap)
    if method == "live":
        started = worker.start_job("analyze", limit=n, workers=workers, cap_usd=cap_usd)
        return RedirectResponse("/bulk" + ("" if started else "?msg=A+job+is+already+running."), status_code=303)
    result = batch.submit(n, cap_usd)
    from urllib.parse import quote_plus
    note = f"Sent {result['count']:,} videos to the Batch API (about ${result['estimate']:.2f})." if result["ok"] else result["error"]
    return RedirectResponse(f"/bulk?msg={quote_plus(note)}", status_code=303)


@app.post("/bulk/check-batches")
def bulk_check_batches():
    done = batch.poll_once()
    return RedirectResponse(f"/bulk?msg={done}+videos+finished+in+the+batch." if done else "/bulk?msg=Nothing+new+yet.", status_code=303)


@app.post("/bulk/stop")
def bulk_stop():
    worker.stop_job()
    return RedirectResponse("/bulk", status_code=303)


@app.get("/api/progress", response_model=ProgressResponse)
def api_progress():
    return worker.progress_summary()


@app.get("/health", response_model=HealthResponse)
def health():
    return {"ok": True}


# ---------------- daily agent: machine-to-machine routes ----------------
# Everything below requires the bearer token. These are the only authenticated
# routes in the app; your own browser use of the dashboard above is untouched.
def require_agent_token(authorization: str = Header(default="")):
    expected = f"Bearer {get_agent_token()}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid agent token. See Settings.")


@app.post("/api/ingest/links", dependencies=[Depends(require_agent_token)], response_model=IngestLinksResponse)
def api_ingest_links(entries: list[IngestLinkEntry], session: Session = Depends(get_db)):
    """Safe to call with everything currently visible in the source (Saved
    page / inbox) every time - already-known URLs are silently skipped."""
    parsed = [
        {"tiktok_url": e.tiktok_url.strip(), "saved_date": e.saved_date, "user_note": e.user_note}
        for e in entries if e.tiktok_url.strip()
    ]
    added = insert_new_videos(session, parsed)
    return {"seen": len(parsed), "added": added}


@app.post("/api/sync/inbox", dependencies=[Depends(require_agent_token)],
          response_model=SyncInboxResponse, responses={409: {"model": SyncErrorResponse}, 502: {"model": SyncErrorResponse}})
def api_sync_inbox(session: Session = Depends(get_db), start_processing: bool = True):
    """One call covering the whole free half of the pipeline: read the bot
    account's inbox for videos shared by the trusted sender (if a login
    session has been set up), ingest anything new, then kick off the
    existing collect+analyze job so results are ready by the time the agent
    asks for the action queue."""
    from app import scraper
    try:
        links = scraper.scrape_new_saves()
    except scraper.NotLoggedIn:
        return JSONResponse(status_code=409, content={
            "error": "No TikTok login session saved yet. Run `python -m app.scraper login` on the host, "
                     "against the bot account, then retry.",
        })
    except scraper.NoTrustedSender:
        return JSONResponse(status_code=409, content={
            "error": "No trusted TikTok handle set. Add your real handle in Settings so the inbox reader "
                     "knows whose shared videos to act on.",
        })
    except Exception as e:  # noqa: BLE001 - scraping is inherently fragile; report, don't crash the agent run
        return JSONResponse(status_code=502, content={"error": f"TikTok inbox read failed: {e}"[:500]})

    added = insert_new_videos(session, links)

    # Bounded to exactly what THIS sync found - a daily sync processing your
    # entire multi-thousand-video backlog every time it runs (a real bug this
    # fixes: it originally had no limit at all, and also triggered on any
    # pre-existing backlog even with zero new videos) would be both wildly
    # slow and an unbounded Claude bill. Catching up a big backlog is still
    # possible via /bulk, deliberately, not as a side effect of every sync.
    started = False
    if start_processing and added:
        started = worker.start_job("full", limit=added, retry_errors=False)
    return {"scraped": len(links), "added": added, "processing_started": started,
            "job": worker.job_status()}


@app.get("/api/actions/queue", dependencies=[Depends(require_agent_token)], response_model=ActionsQueueResponse)
def api_actions_queue(session: Session = Depends(get_db)):
    rows = session.exec(select(Action).where(Action.status == ActionStatus.QUEUED).order_by(Action.created_at)).all()
    out = []
    for a in rows:
        video = session.get(Video, a.video_id)
        out.append(QueuedAction(
            action_id=a.id, video_id=a.video_id, action_type=a.action_type.value,
            brief=a.brief, tiktok_url=video.tiktok_url if video else None,
            category=video.category if video else None, summary=video.summary if video else None,
            author=video.author if video else None,
        ))
    return {"queue": out, "settings": get_action_settings()}


@app.post("/api/actions/{action_id}", dependencies=[Depends(require_agent_token)], response_model=ActionUpdateResponse)
def api_action_update(action_id: int, body: ActionUpdateRequest, session: Session = Depends(get_db)):
    if body.status not in [s.value for s in ActionStatus]:
        raise HTTPException(status_code=400, detail="Invalid or missing status.")
    action = session.get(Action, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="No such action.")
    action.status = ActionStatus(body.status)
    if body.result is not None:
        import json as _json
        action.result = body.result if isinstance(body.result, str) else _json.dumps(body.result)
    if body.error_message is not None:
        action.error_message = body.error_message or None
    from datetime import datetime as dt
    action.updated_at = dt.utcnow()
    session.add(action)
    session.commit()
    return {"ok": True}


# ---------------- Actions / Jobs dashboard views ----------------
@app.get("/actions")
def actions_page(request: Request, session: Session = Depends(get_db)):
    rows = session.exec(select(Action).order_by(Action.updated_at.desc())).all()
    items = []
    for a in rows:
        if a.action_type == ActionType.JOB:
            continue
        video = session.get(Video, a.video_id)
        items.append({"action": a, "video": video})
    grouped = {}
    for item in items:
        grouped.setdefault(item["action"].status.value, []).append(item)
    return templates.TemplateResponse(request, "actions.html", {
        "grouped": grouped,
        "status_order": [s.value for s in ActionStatus],
        "settings": get_action_settings(),
    })


@app.get("/jobs")
def jobs_page(request: Request, session: Session = Depends(get_db)):
    rows = session.exec(
        select(Action).where(Action.action_type == ActionType.JOB).order_by(Action.updated_at.desc())).all()
    items = [{"action": a, "video": session.get(Video, a.video_id)} for a in rows]
    return templates.TemplateResponse(request, "jobs.html", {
        "items": items, "has_resume": bool(get_resume_text()),
        "settings": get_action_settings(),
    })


@app.post("/actions/{action_id}/dismiss")
def action_dismiss(action_id: int, session: Session = Depends(get_db), next: str = Form("/actions")):
    action = session.get(Action, action_id)
    if action:
        action.status = ActionStatus.DISMISSED
        session.add(action)
        session.commit()
    return RedirectResponse(next if next.startswith("/") else "/actions", status_code=303)


@app.post("/actions/{action_id}/requeue")
def action_requeue(action_id: int, session: Session = Depends(get_db), next: str = Form("/actions")):
    action = session.get(Action, action_id)
    if action:
        action.status = ActionStatus.QUEUED
        action.error_message = None
        session.add(action)
        session.commit()
    return RedirectResponse(next if next.startswith("/") else "/actions", status_code=303)


@app.post("/jobs/{action_id}/mark-applied")
def job_mark_applied(action_id: int, session: Session = Depends(get_db)):
    """You confirming you personally submitted the application. The agent never sets this."""
    action = session.get(Action, action_id)
    if action:
        action.status = ActionStatus.DONE
        session.add(action)
        session.commit()
    return RedirectResponse("/jobs", status_code=303)


@app.post("/settings/resume")
async def settings_resume(file: UploadFile | None = File(None), text: str = Form("")):
    content = text.strip()
    if file is not None and file.filename:
        content = (await file.read()).decode("utf-8", errors="ignore")
    if content:
        save_resume_text(content)
    return RedirectResponse("/settings?key=resume_saved", status_code=303)


@app.post("/settings/actions")
def settings_actions(skill_enabled: bool = Form(False), project_enabled: bool = Form(False),
                      job_enabled: bool = Form(False), agent_paused: bool = Form(False)):
    save_action_settings(skill_enabled=skill_enabled, project_enabled=project_enabled,
                          job_enabled=job_enabled, agent_paused=agent_paused)
    return RedirectResponse("/settings?key=actions_saved", status_code=303)


@app.post("/settings/trusted-sender")
def settings_trusted_sender(handle: str = Form("")):
    save_trusted_sender(handle)
    return RedirectResponse("/settings?key=sender_saved", status_code=303)


@app.post("/settings/regenerate-token")
def settings_regenerate_token():
    regenerate_agent_token()
    return RedirectResponse("/settings?key=token_rotated", status_code=303)


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
        "stale_threshold_months": STALE_THRESHOLD_MONTHS,
        "env_path": str(BASE_DIR / ".env"),
        "agent_token": get_agent_token(),
        "trusted_sender": get_trusted_sender(),
        "action_settings": get_action_settings(),
        "resume_text": get_resume_text(),
    })


@app.get("/media/thumb/{video_id}")
def media_thumb(video_id: int, session: Session = Depends(get_db)):
    video = session.get(Video, video_id)
    if video and video.thumbnail_path and Path(video.thumbnail_path).exists():
        return FileResponse(video.thumbnail_path, media_type="image/jpeg")
    return FileResponse(str(BASE_DIR / "static" / "placeholder.svg"))
