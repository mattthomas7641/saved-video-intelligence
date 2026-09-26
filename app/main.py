from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.config import BASE_DIR, ANTHROPIC_API_KEY, STALE_THRESHOLD_MONTHS, CATEGORIES
from app.db import init_db, get_session
from app.models import Video, Status
from app.ingest import parse_export
from app import worker

app = FastAPI(title="TikTok Saved Scanner")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

init_db()


def _video_count() -> int:
    session = get_session()
    try:
        return len(session.exec(select(Video.id)).all())
    finally:
        session.close()


@app.get("/")
def root():
    if _video_count() == 0:
        return RedirectResponse("/onboarding")
    return RedirectResponse("/dashboard")


@app.get("/onboarding")
def onboarding(request: Request):
    return templates.TemplateResponse("onboarding.html", {
        "request": request,
        "has_api_key": bool(ANTHROPIC_API_KEY),
    })


@app.post("/onboarding/upload")
async def onboarding_upload(file: UploadFile = File(...)):
    content = await file.read()
    entries = parse_export(content)

    session = get_session()
    try:
        added = 0
        for entry in entries:
            existing = session.exec(
                select(Video).where(Video.tiktok_url == entry["tiktok_url"])
            ).first()
            if existing:
                continue
            session.add(Video(tiktok_url=entry["tiktok_url"], saved_date=entry["saved_date"]))
            added += 1
        session.commit()
    finally:
        session.close()

    return RedirectResponse(f"/dashboard?imported={added}", status_code=303)


@app.get("/dashboard")
def dashboard(request: Request, tab: str = "all", category: str = "", q: str = "",
              sort: str = "score", imported: int | None = None):
    session = get_session()
    try:
        videos = session.exec(select(Video)).all()
    finally:
        session.close()

    if tab == "unwatched":
        videos = [v for v in videos if not v.watched and not v.archived]
    elif tab == "worth_rewatching":
        videos = [v for v in videos if (v.worth_rewatching_score or 0) >= 4 and not v.archived]
    elif tab == "flagged":
        videos = [v for v in videos if v.needs_verification and not v.archived]
    elif tab == "archived":
        videos = [v for v in videos if v.archived]
    else:
        videos = [v for v in videos if not v.archived]

    if category:
        videos = [v for v in videos if v.category == category]

    if q:
        ql = q.lower()
        def matches(v: Video) -> bool:
            haystack = " ".join(filter(None, [v.summary, v.caption, v.tags, v.key_facts, v.author]))
            return ql in haystack.lower()
        videos = [v for v in videos if matches(v)]

    if sort == "score":
        videos.sort(key=lambda v: (v.worth_rewatching_score or 0), reverse=True)
    elif sort == "newest_saved":
        videos.sort(key=lambda v: v.saved_date or v.created_at, reverse=True)
    elif sort == "oldest_saved":
        videos.sort(key=lambda v: v.saved_date or v.created_at)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "videos": videos,
        "tab": tab,
        "category": category,
        "q": q,
        "sort": sort,
        "categories": CATEGORIES,
        "imported": imported,
        "progress": worker.progress_summary(),
        "has_api_key": bool(ANTHROPIC_API_KEY),
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
def toggle_watched(video_id: int):
    session = get_session()
    try:
        video = session.get(Video, video_id)
        video.watched = not video.watched
        session.add(video)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(f"/video/{video_id}", status_code=303)


@app.post("/video/{video_id}/archive")
def toggle_archive(video_id: int):
    session = get_session()
    try:
        video = session.get(Video, video_id)
        video.archived = not video.archived
        session.add(video)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(f"/video/{video_id}", status_code=303)


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
    worker.start_processing(retry_errors=retry_errors, limit=n)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/api/progress")
def api_progress():
    return worker.progress_summary()


@app.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "has_api_key": bool(ANTHROPIC_API_KEY),
        "stale_threshold_months": STALE_THRESHOLD_MONTHS,
        "env_path": str(BASE_DIR / ".env"),
    })


@app.get("/media/thumb/{video_id}")
def media_thumb(video_id: int):
    session = get_session()
    try:
        video = session.get(Video, video_id)
    finally:
        session.close()
    if video and video.thumbnail_path and Path(video.thumbnail_path).exists():
        return FileResponse(video.thumbnail_path)
    return FileResponse(str(BASE_DIR / "static" / "placeholder.svg"))
