from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app import batch, worker
from app.config import (
    BASE_DIR,
    DEFAULT_WORKERS,
    GROUPS,
    STALE_THRESHOLD_MONTHS,
    get_action_settings,
    get_agent_token,
    get_api_key,
    get_resume_text,
    group_of,
    regenerate_agent_token,
    save_action_settings,
    save_api_key,
    save_resume_text,
)
from app.db import get_session, init_db
from app.ingest import insert_new_videos, parse_export
from app.models import Action, ActionStatus, ActionType, Status, Video

app = FastAPI(title="TikTok Saved Scanner")
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


def _video_count() -> int:
    session = get_session()
    try:
        return len(session.exec(select(Video.id)).all())
    finally:
        session.close()


@app.get("/")
def root():
    return RedirectResponse("/dashboard")


@app.get("/onboarding")
def onboarding(request: Request):
    return templates.TemplateResponse("onboarding.html", {
        "request": request,
        "has_api_key": bool(get_api_key()),
    })


@app.post("/onboarding/upload")
async def onboarding_upload(file: UploadFile = File(...)):
    content = await file.read()
    entries = parse_export(content)

    session = get_session()
    try:
        added = insert_new_videos(session, entries)
    finally:
        session.close()

    return RedirectResponse(f"/dashboard?imported={added}", status_code=303)


PER_PAGE = 30
VIEW_NAMES = {
    "processed": "Everything", "worth_rewatching": "Worth rewatching",
    "flagged": "May be expired", "unwatched": "Not revisited",
    "archived": "Archived", "queue": "Waiting & errors",
}
FILTER_KEYS = ("tab", "group", "category", "theme", "q", "sort", "show", "page")


@app.get("/dashboard")
def dashboard(request: Request, tab: str = "processed", group: str = "", category: str = "",
              theme: str = "", q: str = "", sort: str = "score", show: str = "", page: int = 1,
              imported: int | None = None):
    from collections import Counter, defaultdict
    from urllib.parse import urlencode

    session = get_session()
    try:
        all_videos = session.exec(select(Video)).all()
    finally:
        session.close()

    if tab not in VIEW_NAMES:
        tab = "processed"
    if category and not group:
        group = group_of(category)

    done = [v for v in all_videos if v.status == Status.DONE]
    live = [v for v in done if not v.archived]
    scores = [v.worth_rewatching_score for v in done if v.worth_rewatching_score]
    stats = {
        "total": len(all_videos),
        "processed": len(done),
        "queue": len([v for v in all_videos if v.status != Status.DONE]),
        "errors": len([v for v in all_videos if v.status == Status.ERROR]),
        "worth": len([v for v in live if (v.worth_rewatching_score or 0) >= 4]),
        "flagged": len([v for v in live if v.needs_verification]),
        "unwatched": len([v for v in live if not v.watched]),
        "archived": len([v for v in done if v.archived]),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
    }

    # ---- topic tree (group -> category), counted over the live library
    by_group: dict[str, Counter] = defaultdict(Counter)
    for v in live:
        if v.category:
            by_group[group_of(v.category)][v.category] += 1
    order = [g for g, _ in GROUPS]
    tree = []
    for g in order:
        if g in by_group:
            cats = by_group[g].most_common()
            tree.append({"name": g, "count": sum(n for _, n in cats), "cats": cats})

    # ---- base set for the chosen view
    if tab == "queue":
        videos = [v for v in all_videos if v.status != Status.DONE]
    elif tab == "archived":
        videos = [v for v in done if v.archived]
    elif tab == "worth_rewatching":
        videos = [v for v in live if (v.worth_rewatching_score or 0) >= 4]
    elif tab == "flagged":
        videos = [v for v in live if v.needs_verification]
    elif tab == "unwatched":
        videos = [v for v in live if not v.watched]
    else:
        videos = live

    view_total = len(videos)
    if group:
        videos = [v for v in videos if group_of(v.category) == group]
    if category:
        videos = [v for v in videos if v.category == category]

    # ---- drill-down chips for the next level
    if category:
        tag_counts = Counter(t.strip() for v in videos for t in (v.tags or "").split(",") if t.strip())
        drill_label, drill = "Themes", [("theme", t, n) for t, n in tag_counts.most_common(14)]
    elif group:
        cat_counts = Counter(v.category for v in videos if v.category)
        drill_label, drill = "Categories", [("category", c, n) for c, n in cat_counts.most_common()]
    else:
        grp_counts = Counter(group_of(v.category) for v in videos if v.category)
        drill_label = "Topics"
        drill = [("group", g, grp_counts[g]) for g in order if grp_counts[g]]

    if theme:
        tl = theme.lower()
        videos = [v for v in videos if tl in [t.strip().lower() for t in (v.tags or "").split(",")]]
    if q:
        ql = q.lower()
        def matches(v: Video) -> bool:
            haystack = " ".join(filter(None, [v.summary, v.caption, v.tags, v.key_facts, v.author, v.category]))
            return ql in haystack.lower()
        videos = [v for v in videos if matches(v)]

    def recency(v: Video):
        return v.saved_date or v.created_at
    if sort == "newest_saved":
        videos.sort(key=recency, reverse=True)
    elif sort == "oldest_saved":
        videos.sort(key=recency)
    else:
        sort = "score"
        videos.sort(key=lambda v: ((v.worth_rewatching_score or 0), recency(v)), reverse=True)

    total_results = len(videos)
    pages = max(1, -(-total_results // PER_PAGE))
    page = min(max(page, 1), pages)
    page_videos = videos[(page - 1) * PER_PAGE: page * PER_PAGE]

    current = {"tab": tab, "group": group, "category": category, "theme": theme, "q": q, "sort": sort, "show": show, "page": page}

    def href(**overrides):
        merged = {**current, **overrides}
        # moving up/sideways in the hierarchy clears the levels below it
        if "group" in overrides and "category" not in overrides:
            merged["category"] = ""
        if ("group" in overrides or "category" in overrides) and "theme" not in overrides:
            merged["theme"] = ""
        if any(k in overrides for k in FILTER_KEYS if k != "page"):
            merged["page"] = 1
        if merged["tab"] == "processed":
            merged["tab"] = ""
        if merged["sort"] == "score":
            merged["sort"] = ""
        return "/dashboard?" + urlencode({k: v for k, v in merged.items() if v not in ("", None, 1)}) if any(
            v not in ("", None, 1) for v in merged.values()) else "/dashboard"

    crumbs = [("Library", href(group="", category="", theme="", q="", show=""))]
    if group:
        crumbs.append((group, href(group=group, category="")))
    if category:
        crumbs.append((category, href(category=category)))
    if theme:
        crumbs.append((theme, None))

    # ---- overview (home) data
    filtering = bool(group or category or theme or q)
    overview = tab == "processed" and not filtering and show != "all" and stats["processed"] > 0
    panels, top_picks = [], []
    if overview:
        def best_key(v: Video):
            return ((v.worth_rewatching_score or 0), recency(v))
        for g in tree:
            members = [v for v in live if group_of(v.category) == g["name"]]
            g = {**g, "best": max(members, key=best_key) if members else None,
                 "worth": len([v for v in members if (v.worth_rewatching_score or 0) >= 4]),
                 "flagged": len([v for v in members if v.needs_verification])}
            panels.append(g)
        top_picks = sorted([v for v in live if (v.worth_rewatching_score or 0) >= 4], key=best_key, reverse=True)[:8]

    action_session = get_session()
    try:
        action_queue_count = len(action_session.exec(
            select(Action.id).where(Action.status == ActionStatus.QUEUED)).all())
    finally:
        action_session.close()

    key_ok = bool(get_api_key())
    setup = {
        "key": key_ok, "imported": stats["total"] > 0, "analyzed": stats["processed"] > 0,
    }
    show_setup = not (setup["key"] and setup["imported"] and setup["analyzed"])

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "videos": page_videos, "top_picks": top_picks, "panels": panels, "overview": overview,
        "tab": tab, "group": group, "category": category, "theme": theme, "q": q, "sort": sort,
        "page": page, "pages": pages, "total_results": total_results, "view_total": view_total,
        "tree": tree, "drill": drill, "drill_label": drill_label, "crumbs": crumbs,
        "view_names": VIEW_NAMES, "stats": stats, "href": href,
        "setup": setup, "show_setup": show_setup,
        "next_url": request.url.path + ("?" + request.url.query if request.url.query else ""),
        "action_queue_count": action_queue_count,
        "imported": imported,
        "progress": worker.progress_summary(),
        "has_api_key": key_ok,
    })


@app.get("/video/{video_id}")
def video_detail(request: Request, video_id: int):
    session = get_session()
    try:
        video = session.get(Video, video_id)
    finally:
        session.close()
    return templates.TemplateResponse("video_detail.html", {"request": request, "video": video})


@app.post("/video/{video_id}/watched")
def toggle_watched(video_id: int, next: str = Form("")):
    session = get_session()
    try:
        video = session.get(Video, video_id)
        video.watched = not video.watched
        session.add(video)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(next if next.startswith("/dashboard") else f"/video/{video_id}", status_code=303)


@app.post("/video/{video_id}/archive")
def toggle_archive(video_id: int, next: str = Form("")):
    session = get_session()
    try:
        video = session.get(Video, video_id)
        video.archived = not video.archived
        session.add(video)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(next if next.startswith("/dashboard") else f"/video/{video_id}", status_code=303)


@app.post("/video/{video_id}/reprocess")
def reprocess(video_id: int):
    session = get_session()
    try:
        video = session.get(Video, video_id)
        video.status = Status.PENDING
        video.error_message = None
        session.add(video)
        session.commit()
    finally:
        session.close()
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
    return templates.TemplateResponse("bulk.html", {
        "request": request, "counts": _bulk_counts(), "job": worker.job_status(), "msg": msg,
        "cost_live": batch.observed_cost(False) or 0.0026, "cost_batch": batch.observed_cost(True) or batch.DEFAULT_BATCH_COST,
        "default_workers": DEFAULT_WORKERS, "has_api_key": bool(get_api_key()),
    })


@app.get("/api/bulk")
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


@app.get("/api/progress")
def api_progress():
    return worker.progress_summary()


@app.get("/health")
def health():
    return {"ok": True}


# ---------------- daily agent: machine-to-machine routes ----------------
# Everything below requires the bearer token. These are the only authenticated
# routes in the app; your own browser use of the dashboard above is untouched.
def require_agent_token(authorization: str = Header(default="")):
    expected = f"Bearer {get_agent_token()}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid agent token. See Settings.")


@app.post("/api/ingest/links", dependencies=[Depends(require_agent_token)])
def api_ingest_links(entries: list[dict] = Body(...)):
    """entries: [{"tiktok_url": str, "saved_date": "2026-01-01T00:00:00"|null}, ...].
    Safe to call with everything currently visible on the Saved page every time —
    already-known URLs are silently skipped."""
    from datetime import datetime as dt
    parsed = []
    for e in entries:
        url = (e.get("tiktok_url") or "").strip()
        if not url:
            continue
        saved_date = None
        if e.get("saved_date"):
            try:
                saved_date = dt.fromisoformat(e["saved_date"])
            except ValueError:
                saved_date = None
        parsed.append({"tiktok_url": url, "saved_date": saved_date})
    session = get_session()
    try:
        added = insert_new_videos(session, parsed)
    finally:
        session.close()
    return {"seen": len(parsed), "added": added}


@app.post("/api/sync/tiktok", dependencies=[Depends(require_agent_token)])
def api_sync_tiktok(start_processing: bool = True):
    """One call covering the whole free half of the pipeline: scrape your Saved
    page (if a login session has been set up), ingest anything new, then kick
    off the existing collect+analyze job so results are ready by the time the
    agent asks for the action queue."""
    from app import scraper
    try:
        links = scraper.scrape_new_saves()
    except scraper.NotLoggedIn:
        return JSONResponse(status_code=409, content={
            "error": "No TikTok login session saved yet. Run `python -m app.scraper login` on the host once, "
                     "then retry.",
        })
    except Exception as e:  # noqa: BLE001 - scraping is inherently fragile; report, don't crash the agent run
        return JSONResponse(status_code=502, content={"error": f"TikTok scrape failed: {e}"[:500]})

    session = get_session()
    try:
        added = insert_new_videos(session, links)
    finally:
        session.close()

    started = False
    if start_processing and (added or worker.progress_summary()["counts"]["pending"]):
        started = worker.start_job("full", retry_errors=False)
    return {"scraped": len(links), "added": added, "processing_started": started,
            "job": worker.job_status()}


@app.get("/api/actions/queue", dependencies=[Depends(require_agent_token)])
def api_actions_queue():
    session = get_session()
    try:
        rows = session.exec(select(Action).where(Action.status == ActionStatus.QUEUED).order_by(Action.created_at)).all()
        out = []
        for a in rows:
            video = session.get(Video, a.video_id)
            out.append({
                "action_id": a.id, "video_id": a.video_id, "action_type": a.action_type.value,
                "brief": a.brief, "tiktok_url": video.tiktok_url if video else None,
                "category": video.category if video else None, "summary": video.summary if video else None,
                "author": video.author if video else None,
            })
        return {"queue": out, "settings": get_action_settings()}
    finally:
        session.close()


@app.post("/api/actions/{action_id}", dependencies=[Depends(require_agent_token)])
def api_action_update(action_id: int, body: dict = Body(...)):
    status_in = body.get("status")
    if status_in not in [s.value for s in ActionStatus]:
        raise HTTPException(status_code=400, detail="Invalid or missing status.")
    session = get_session()
    try:
        action = session.get(Action, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="No such action.")
        action.status = ActionStatus(status_in)
        if "result" in body:
            import json as _json
            action.result = _json.dumps(body["result"]) if not isinstance(body["result"], str) else body["result"]
        if "error_message" in body:
            action.error_message = (body["error_message"] or "")[:500] or None
        from datetime import datetime as dt
        action.updated_at = dt.utcnow()
        session.add(action)
        session.commit()
        return {"ok": True}
    finally:
        session.close()


# ---------------- Actions / Jobs dashboard views ----------------
@app.get("/actions")
def actions_page(request: Request):
    session = get_session()
    try:
        rows = session.exec(select(Action).order_by(Action.updated_at.desc())).all()
        items = []
        for a in rows:
            if a.action_type == ActionType.JOB:
                continue
            video = session.get(Video, a.video_id)
            items.append({"action": a, "video": video})
    finally:
        session.close()
    grouped = {}
    for item in items:
        grouped.setdefault(item["action"].status.value, []).append(item)
    return templates.TemplateResponse("actions.html", {
        "request": request, "grouped": grouped,
        "status_order": [s.value for s in ActionStatus],
        "settings": get_action_settings(),
    })


@app.get("/jobs")
def jobs_page(request: Request):
    session = get_session()
    try:
        rows = session.exec(
            select(Action).where(Action.action_type == ActionType.JOB).order_by(Action.updated_at.desc())).all()
        items = [{"action": a, "video": session.get(Video, a.video_id)} for a in rows]
    finally:
        session.close()
    return templates.TemplateResponse("jobs.html", {
        "request": request, "items": items, "has_resume": bool(get_resume_text()),
        "settings": get_action_settings(),
    })


@app.post("/actions/{action_id}/dismiss")
def action_dismiss(action_id: int, next: str = Form("/actions")):
    session = get_session()
    try:
        action = session.get(Action, action_id)
        if action:
            action.status = ActionStatus.DISMISSED
            session.add(action)
            session.commit()
    finally:
        session.close()
    return RedirectResponse(next if next.startswith("/") else "/actions", status_code=303)


@app.post("/actions/{action_id}/requeue")
def action_requeue(action_id: int, next: str = Form("/actions")):
    session = get_session()
    try:
        action = session.get(Action, action_id)
        if action:
            action.status = ActionStatus.QUEUED
            action.error_message = None
            session.add(action)
            session.commit()
    finally:
        session.close()
    return RedirectResponse(next if next.startswith("/") else "/actions", status_code=303)


@app.post("/jobs/{action_id}/mark-applied")
def job_mark_applied(action_id: int):
    """You confirming you personally submitted the application. The agent never sets this."""
    session = get_session()
    try:
        action = session.get(Action, action_id)
        if action:
            action.status = ActionStatus.DONE
            session.add(action)
            session.commit()
    finally:
        session.close()
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
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "key_status": key,
        "has_api_key": bool(get_api_key()),
        "stale_threshold_months": STALE_THRESHOLD_MONTHS,
        "env_path": str(BASE_DIR / ".env"),
        "agent_token": get_agent_token(),
        "action_settings": get_action_settings(),
        "resume_text": get_resume_text(),
    })


@app.get("/media/thumb/{video_id}")
def media_thumb(video_id: int):
    session = get_session()
    try:
        video = session.get(Video, video_id)
    finally:
        session.close()
    if video and video.thumbnail_path and Path(video.thumbnail_path).exists():
        return FileResponse(video.thumbnail_path, media_type="image/jpeg")
    return FileResponse(str(BASE_DIR / "static" / "placeholder.svg"))
