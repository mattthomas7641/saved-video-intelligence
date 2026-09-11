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
from typing import Iterable

TIKTOK_URL_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/\S+", re.IGNORECASE)

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
        if key in entry and isinstance(entry[key], str) and "tiktok.com" in entry[key]:
            return entry[key]
    return None


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
        # Walk path segments outer-to-inner so the containing section's name
        # (e.g. "Like List") wins over an inner list key that happens to
        # contain "Favorite" in its name (TikTok's own "ItemFavoriteList").
        for segment in path.lower().split("."):
            if "like" in segment:
                return "like"
            if "favorite" in segment:
                return "favorite"
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
            if link:
                results.setdefault(link, _extract_date(entry))

    # Fall back to (or supplement with) plain URL scraping for TXT-style exports
    # or JSON we couldn't confidently parse.
    if not results:
        for match in TIKTOK_URL_RE.finditer(text):
            url = match.group(0).rstrip(").,\"'")
            results.setdefault(url, None)

    return [{"tiktok_url": url, "saved_date": date} for url, date in results.items()]
