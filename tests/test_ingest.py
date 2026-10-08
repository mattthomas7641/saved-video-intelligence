"""app/ingest.py: TikTok export parsing + dedup-insert.

Fixtures mirror real cases hit during manual testing this session: the
Favorite Videos / Like List / Favorite Sounds ambiguity that caused the
original false-"no videos found" bug, and the tiktokv.com link-normalization
fix for the real export format.
"""
import json
from datetime import datetime, timedelta

from sqlmodel import select

from app.ingest import insert_new_videos, parse_export
from app.models import Video


def test_picks_favorite_videos_not_favorite_sounds_or_likes():
    """Regression test for a real bug: TikTok nests 'ItemFavoriteList' inside
    'Like List', and the section name alone ('favorite' appears in both the
    wanted and unwanted branches) isn't enough to disambiguate."""
    export = {
        "Activity": {
            "Favorite Videos": {"FavoriteVideoList": [
                {"Date": "2024-01-15 10:00:00", "Link": "https://www.tiktok.com/@a/video/7123456789012345678"},
                {"Date": "2025-06-02 18:22:01", "Link": "https://www.tiktok.com/@b/video/7234567890123456789"},
            ]},
            "Like List": {"ItemFavoriteList": [
                {"Date": "2023-03-03 01:01:01", "Link": "https://www.tiktok.com/@c/video/7345678901234567890"},
            ]},
        }
    }
    result = parse_export(json.dumps(export).encode())
    urls = {r["tiktok_url"] for r in result}
    assert len(result) == 2
    assert "7123456789012345678" in "".join(urls)
    assert "7345678901234567890" not in "".join(urls)  # the Like List entry must not leak in


def test_normalizes_tiktokv_share_links_to_canonical_form():
    """Regression test: the real export uses tiktokv.com/share/video/<id>/
    links, which the original parser didn't recognize at all."""
    export = {"Activity": {"Favorite Videos": {"FavoriteVideoList": [
        {"Date": "2026-09-26 13:23:53", "Link": "https://www.tiktokv.com/share/video/7689762333345778958/"},
    ]}}}
    result = parse_export(json.dumps(export).encode())
    assert result == [{
        "tiktok_url": "https://www.tiktok.com/@_/video/7689762333345778958",
        "saved_date": result[0]["saved_date"],
    }]
    assert result[0]["saved_date"].year == 2026


def test_plain_text_fallback_extracts_bare_urls():
    text = b"random junk https://www.tiktok.com/@foo/video/111222333 more junk https://www.tiktok.com/@bar/video/444555666."
    result = parse_export(text)
    assert len(result) == 2


def test_ignores_non_video_links_like_sounds_and_hashtags():
    export = {"Activity": {
        "Favorite Sounds": {"FavoriteSoundList": [{"Date": "2024-01-01", "Link": "https://www.tiktokv.com/share/sticker/detail/123"}]},
        "Favorite Hashtags": {"FavoriteHashtagList": [{"Date": "2024-01-01", "Link": "https://m.tiktok.com/h5/share/tag/123.html"}]},
    }}
    assert parse_export(json.dumps(export).encode()) == []


def test_insert_new_videos_dedups_by_url(session):
    entries = [
        {"tiktok_url": "https://www.tiktok.com/@a/video/1", "saved_date": None},
        {"tiktok_url": "https://www.tiktok.com/@b/video/2", "saved_date": None},
    ]
    added_first = insert_new_videos(session, entries)
    added_second = insert_new_videos(session, entries)  # re-ingest the same links
    assert added_first == 2
    assert added_second == 0
    assert len(session.exec(select(Video)).all()) == 2


def test_insert_new_videos_defaults_missing_saved_date_to_now(session):
    """Regression test for a real bug found live: the DM-sync path doesn't
    know TikTok's original save time and passes saved_date=None. Leaving
    that as NULL sorts a synced video dead last in the saved_date-DESC
    processing queue (SQLite sorts NULL as the smallest value) - behind an
    entire historical backlog, not ahead of it as the daily sync intends.
    Defaulting to "now" fixes the ordering without the scraper needing to
    guess a real save time it doesn't have."""
    old_video = Video(tiktok_url="https://www.tiktok.com/@old/video/1",
                       saved_date=datetime.utcnow() - timedelta(days=300))
    session.add(old_video)
    session.commit()

    insert_new_videos(session, [{"tiktok_url": "https://www.tiktok.com/@new/video/2", "saved_date": None}])

    new_video = session.exec(select(Video).where(Video.tiktok_url.contains("@new"))).first()
    assert new_video.saved_date is not None
    assert new_video.saved_date > old_video.saved_date
