"""Query/filter/stats logic for the /dashboard route, extracted out of
app/main.py so the route itself is thin HTTP glue (parse params -> call this
-> render). Covered by characterization tests written against the route's
original inline behavior before this extraction happened
(tests/test_dashboard_characterization.py) - this function's behavior is
pinned to match exactly, not redesigned.
"""
from collections import Counter, defaultdict
from urllib.parse import urlencode

from sqlmodel import Session, select

from app.config import GROUPS, group_of
from app.models import Action, ActionStatus, Status, Video

PER_PAGE = 30
VIEW_NAMES = {
    "processed": "Everything", "worth_rewatching": "Worth rewatching",
    "flagged": "May be expired", "unwatched": "Not revisited",
    "archived": "Archived", "queue": "Waiting & errors",
}
FILTER_KEYS = ("tab", "group", "category", "theme", "q", "sort", "show", "page")


def build_dashboard_context(
    session: Session, *, tab: str = "processed", group: str = "", category: str = "", theme: str = "",
    q: str = "", sort: str = "score", show: str = "", page: int = 1, imported: int | None = None,
) -> dict:
    """Returns the full template context for dashboard.html (everything
    except `request`, which the route itself supplies)."""
    all_videos = session.exec(select(Video)).all()

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

    action_queue_count = len(session.exec(select(Action.id).where(Action.status == ActionStatus.QUEUED)).all())

    return {
        "videos": page_videos, "top_picks": top_picks, "panels": panels, "overview": overview,
        "tab": tab, "group": group, "category": category, "theme": theme, "q": q, "sort": sort,
        "page": page, "pages": pages, "total_results": total_results, "view_total": view_total,
        "tree": tree, "drill": drill, "drill_label": drill_label, "crumbs": crumbs,
        "view_names": VIEW_NAMES, "stats": stats, "href": href,
        "action_queue_count": action_queue_count,
        "imported": imported,
    }
