"""Parse a TikTok "Download your data" export and pull out saved (Favorite) videos.

TikTok's export format has shifted over time and differs between JSON and TXT
requests, so this parser is deliberately flexible: it walks the whole JSON
structure looking for any list of entries that look like
{"Date": ..., "Link": ...} under a key mentioning "favorite" (falling back to
"like" if no favorites list is found), and also supports plain-text exports
where TikTok video URLs are just listed one per line.
"""
import json
import re
from datetime import datetime

TIKTOK_URL_RE = re.compile(r"https?://(?:[\w-]+\.)?tiktok(?:v)?\.com/\S+", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"/video/(\d{8,})")

DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d",
]


def _parse_date(value: str):
    if not value:
        return None
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _walk(obj, path=""):
    """Yield (path, obj) for every dict/list node in a JSON tree."""
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _extract_link(entry: dict) -> str | None:
    for key in ("Link", "link", "url", "URL", "Url"):
        v = entry.get(key)
        if isinstance(v, str) and re.search(r"tiktok(?:v)?\.com", v):
            return v
    return None


def _normalize(url: str) -> str | None:
    """Only video links are useful; map tiktokv.com/share/video/ID (and other
    share formats) to a canonical URL yt-dlp understands. Sounds, hashtags,
    effects etc. return None."""
    m = VIDEO_ID_RE.search(url)
    if not m:
        return None
    user = re.search(r"tiktok(?:v)?\.com/(@[\w.\-]+)/video/", url)
    return f"https://www.tiktok.com/{user.group(1) if user else '@_'}/video/{m.group(1)}"


def _extract_date(entry: dict):
    for key in ("Date", "date", "Time", "time", "CreateTime"):
        if key in entry and isinstance(entry[key], str):
            d = _parse_date(entry[key])
            if d:
                return d
    return None


def parse_export(file_bytes: bytes) -> list[dict]:
    """Return a list of {"tiktok_url": str, "saved_date": datetime|None}."""
    text = file_bytes.decode("utf-8", errors="ignore")

    results: dict[str, datetime | None] = {}

    # Try JSON first.
    parsed_json = None
    try:
        parsed_json = json.loads(text)
    except json.JSONDecodeError:
        parsed_json = None

    def _classify(path: str) -> str | None:
        # Match section names precisely: TikTok nests everything under
        # "Likes and Favorites", and lists like "ItemFavoriteList" live inside
        # "Like List", so loose substring matching mixes sections together.
        lowered = path.lower()
        if "favorite video" in lowered:
            return "favorite"
        if "like list" in lowered:
            return "like"
        return None

    if parsed_json is not None:
        favorite_entries = []
        like_entries = []
        for path, node in _walk(parsed_json):
            if not isinstance(node, list):
                continue
            entries = [e for e in node if isinstance(e, dict) and _extract_link(e)]
            if not entries:
                continue
            kind = _classify(path)
            if kind == "favorite":
                favorite_entries.extend(entries)
            elif kind == "like":
                like_entries.extend(entries)

        chosen = favorite_entries if favorite_entries else like_entries
        for entry in chosen:
            link = _extract_link(entry)
            url = _normalize(link) if link else None
            if url:
                results.setdefault(url, _extract_date(entry))

    # Fall back to (or supplement with) plain URL scraping for TXT-style exports
    # or JSON we couldn't confidently parse.
    if not results:
        for match in TIKTOK_URL_RE.finditer(text):
            url = _normalize(match.group(0).rstrip(").,\"'"))
            if url:
                results.setdefault(url, None)

    return [{"tiktok_url": url, "saved_date": date} for url, date in results.items()]


def insert_new_videos(session, entries: list[dict]) -> int:
    """Shared dedup-insert used by both the export-upload path and the daily
    sync path: skip anything whose tiktok_url is already known, insert the rest
    as PENDING. Returns how many new rows were added."""
    from app.models import Video  # local import: avoids a circular import with models/db

    added = 0
    for entry in entries:
        url = entry.get("tiktok_url")
        if not url:
            continue
        existing = session.exec(_video_by_url(url)).first()
        if existing:
            continue
        session.add(Video(tiktok_url=url, saved_date=entry.get("saved_date")))
        added += 1
    session.commit()
    return added


def _video_by_url(url: str):
    from sqlmodel import select

    from app.models import Video
    return select(Video).where(Video.tiktok_url == url)
