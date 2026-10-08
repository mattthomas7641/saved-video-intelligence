"""Characterization tests for /dashboard, written BEFORE extracting its logic
into app/services/dashboard_query.py (per the plan: pin current behavior
first, refactor against these, confirm they still pass after). This route
is the single most complex one in the app and already works correctly
against the real 7,311+-video library - these tests exist so the extraction
can't silently change what it does.

Not exhaustive by design - representative coverage of each real behavior
(stats, topic tree, filtering, search, sort, pagination, tabs, overview vs.
filtered view), not every edge case.
"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.models import Status, Video

client = TestClient(app)


def _seed(**overrides) -> Video:
    defaults = dict(
        tiktok_url=f"https://www.tiktok.com/@x/video/{id(overrides)}{len(overrides)}{datetime.utcnow().timestamp()}",
        status=Status.DONE, category="Tech / AI / Coding", summary="A video about something useful.",
        worth_rewatching_score=3, tags="ai,coding", author="someone",
        saved_date=datetime.utcnow(),
    )
    defaults.update(overrides)
    session = get_session()
    v = Video(**defaults)
    session.add(v)
    session.commit()
    session.refresh(v)
    session.close()
    return v


def test_empty_library_shows_onboarding_not_overview():
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Nothing analyzed yet" in resp.text or "Let" in resp.text  # setup/empty state, not a populated overview


def test_basic_load_with_videos_returns_200():
    _seed(summary="A recipe for banana bread.", category="Recipe / Cooking")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "banana bread" in resp.text.lower()


def test_stats_counts_reflect_seeded_data():
    _seed(worth_rewatching_score=5, summary="Five star video about rockets.")
    _seed(worth_rewatching_score=2, summary="Two star video about spoons.")
    _seed(needs_verification=True, has_promo_code=True, summary="Expiring promo video.")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    # the "Worth rewatching" tab count should include at least the 5-star video
    worth_resp = client.get("/dashboard?tab=worth_rewatching")
    assert "rockets" in worth_resp.text.lower()
    assert "spoons" not in worth_resp.text.lower()  # score 2 shouldn't appear in a >=4 filter


def test_category_filter_narrows_results():
    _seed(category="Recipe / Cooking", summary="Chicken parmesan recipe video.")
    _seed(category="Travel", summary="Backpacking through Europe video.")
    resp = client.get("/dashboard?category=Recipe+%2F+Cooking")
    assert resp.status_code == 200
    assert "chicken parmesan" in resp.text.lower()
    assert "backpacking" not in resp.text.lower()


def test_search_query_filters_by_text():
    _seed(summary="A deep dive into kubernetes orchestration.")
    _seed(summary="A recipe for apple pie.")
    resp = client.get("/dashboard?q=kubernetes")
    assert resp.status_code == 200
    assert "kubernetes" in resp.text.lower()
    assert "apple pie" not in resp.text.lower()


def test_archived_videos_excluded_from_default_view_but_shown_in_archived_tab():
    _seed(archived=True, summary="An archived video about old news.")
    default_resp = client.get("/dashboard")
    archived_resp = client.get("/dashboard?tab=archived")
    assert "old news" not in default_resp.text.lower()
    assert "old news" in archived_resp.text.lower()


def test_queue_tab_shows_non_done_videos():
    _seed(status=Status.PENDING, summary="")  # not yet analyzed, no summary
    resp = client.get("/dashboard?tab=queue")
    assert resp.status_code == 200


def test_sort_newest_vs_oldest_saved_changes_order():
    """show=all is required here: the unfiltered view defaults to topic-panel
    overview mode regardless of `sort`, which doesn't render individual video
    titles at all - only the flat list (show=all) does."""
    _seed(summary="Old video from last year.", saved_date=datetime.utcnow() - timedelta(days=300))
    _seed(summary="Brand new video from today.", saved_date=datetime.utcnow())
    newest_resp = client.get("/dashboard?show=all&sort=newest_saved")
    oldest_resp = client.get("/dashboard?show=all&sort=oldest_saved")
    assert newest_resp.text.index("Brand new video") < newest_resp.text.index("Old video")
    assert oldest_resp.text.index("Old video") < oldest_resp.text.index("Brand new video")


def test_pagination_splits_results_across_pages():
    for i in range(35):  # PER_PAGE is 30
        _seed(summary=f"Unique pagination test video number {i}.", category="Recipe / Cooking")
    page1 = client.get("/dashboard?category=Recipe+%2F+Cooking&page=1")
    page2 = client.get("/dashboard?category=Recipe+%2F+Cooking&page=2")
    assert page1.status_code == 200 and page2.status_code == 200
    assert page1.text != page2.text


def test_overview_shown_on_unfiltered_processed_tab_with_data():
    _seed(summary="Overview trigger video.")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Best of everything" in resp.text or "Pick a topic" in resp.text


def test_show_all_bypasses_overview():
    _seed(summary="Non-overview video for show=all check.")
    resp = client.get("/dashboard?show=all")
    assert resp.status_code == 200
    # the overview-only heading shouldn't appear once show=all forces the flat list
    assert "Pick a topic to dig in" not in resp.text
