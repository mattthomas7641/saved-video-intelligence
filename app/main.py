from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.config import BASE_DIR, get_api_key, save_api_key, STALE_THRESHOLD_MONTHS, GROUPS, group_of
from app.db import init_db, get_session
from app.models import Video, Status
from app.ingest import parse_export
from app import worker

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
    worker.start_processing(retry_errors=retry_errors, limit=n)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/api/progress")
def api_progress():
    return worker.progress_summary()


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
