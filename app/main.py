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


PER_PAGE = 20


@app.get("/dashboard")
def dashboard(request: Request, tab: str = "processed", category: str = "", q: str = "",
              sort: str = "score", page: int = 1, imported: int | None = None):
    from collections import Counter
    from urllib.parse import urlencode

    session = get_session()
    try:
        all_videos = session.exec(select(Video)).all()
    finally:
        session.close()

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
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
    }
    category_counts = Counter(v.category for v in live if v.category)
    tag_counts = Counter(t.strip() for v in live for t in (v.tags or "").split(",") if t.strip())

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
        tab = "processed"
        videos = live

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

    total_results = len(videos)
    pages = max(1, -(-total_results // PER_PAGE))
    page = min(max(page, 1), pages)
    videos = videos[(page - 1) * PER_PAGE: page * PER_PAGE]

    current = {"tab": tab, "category": category, "q": q, "sort": sort, "page": page}

    def qs(**overrides):
        merged = {**current, **overrides}
        if "page" not in overrides:
            merged["page"] = 1
        return urlencode({k: v for k, v in merged.items() if v not in ("", None)})

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "videos": videos,
        "tab": tab, "category": category, "q": q, "sort": sort,
        "page": page, "pages": pages, "total_results": total_results,
        "categories": CATEGORIES,
        "category_counts": category_counts.most_common(),
        "top_tags": tag_counts.most_common(14),
        "stats": stats,
        "qs": qs,
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
        return FileResponse(video.thumbnail_path, media_type="image/jpeg")
    return FileResponse(str(BASE_DIR / "static" / "placeholder.svg"))
