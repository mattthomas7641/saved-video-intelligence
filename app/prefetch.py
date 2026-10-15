"""Free legwork before any paid model call.

Repo videos usually show or say their GitHub links; articles get shared as
links. Looking those up here (GitHub API, a plain page fetch) costs nothing,
and handing the results to the model up front means it rarely needs a paid
web search for them.
"""
import html
import re

import httpx

from app.agent import MAX_REPOS, github_repo

GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*)", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_SKIP_HOSTS = re.compile(r"(tiktok(v)?\.com|github\.com|instagram\.com|youtube\.com|youtu\.be|x\.com|twitter\.com)", re.I)
_NOT_REPOS = {"features", "topics", "trending", "sponsors", "orgs", "settings", "marketplace", "about", "login"}
MAX_PAGES = 2
PAGE_CHARS = 3000


def _texts(video) -> str:
    return "\n".join(x for x in [video.tiktok_url, video.caption, video.transcript, video.ocr_text,
                                 getattr(video, "user_note", None)] if x)


def repo_slugs(text: str) -> list[str]:
    out = []
    for owner, name in GITHUB_RE.findall(text):
        name = name.removesuffix(".git").rstrip(".")
        slug = f"{owner}/{name}"
        if owner.lower() not in _NOT_REPOS and slug.lower() not in (s.lower() for s in out):
            out.append(slug)
    return out[:MAX_REPOS]


def page_urls(text: str) -> list[str]:
    out = []
    for u in URL_RE.findall(text):
        u = u.rstrip(".,!?")
        if not _SKIP_HOSTS.search(u) and u not in out:
            out.append(u)
    return out[:MAX_PAGES]


MAX_PAGE_BYTES = 1_000_000
MAX_REDIRECTS = 3


def is_public_url(url: str) -> bool:
    """True only for http(s) URLs whose host resolves exclusively to public addresses.
    Links come from videos, OCR and web pages, so without this a post saying
    "http://127.0.0.1:8787/settings" would make the app read its own settings page
    (or a cloud metadata endpoint) into the model's prompt."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def page_text(url: str, http: httpx.Client) -> str:
    """Fetch a page's visible text, re-checking every redirect hop and capping the size."""
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not is_public_url(url):
                return "(not fetched: not a public web address)"
            with http.stream("GET", url, follow_redirects=False) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = str(r.url.join(r.headers["location"]))
                    continue
                r.raise_for_status()
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_PAGE_BYTES:
                        break
                text = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
            break
        else:
            return "(not fetched: too many redirects)"
    except httpx.HTTPError as e:
        return f"(couldn't fetch: {type(e).__name__})"
    body = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", text)
    body = html.unescape(re.sub(r"(?s)<[^>]+>", " ", body))
    body = re.sub(r"\s+", " ", body).strip()
    return body[:PAGE_CHARS]


def gather(video, http: httpx.Client | None = None, lookup=github_repo) -> dict:
    """{"repos": [facts...], "pages": [{"url", "text"}...]} for the links in a video."""
    text = _texts(video)
    client = http or httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0 (saved-video-intelligence)"})
    try:
        repos = [lookup(slug) for slug in repo_slugs(text)]
        pages = [{"url": u, "text": page_text(u, client)} for u in page_urls(text)]
    finally:
        if http is None:
            client.close()
    return {"repos": [r for r in repos if "error" not in r], "pages": pages}


def as_context(found: dict) -> str:
    if not found["repos"] and not found["pages"]:
        return ""
    lines = ["Already looked up for you (free, current):"]
    for r in found["repos"]:
        lines.append(f"- GitHub {r['full_name']}: {r.get('description') or ''} | {r.get('stars')} stars | "
                     f"last push {r.get('last_push')} | license {r.get('license')} | archived {r.get('archived')}"
                     f"\n  README start: {(r.get('readme_start') or '')[:800]}")
    for p in found["pages"]:
        lines.append(f"- Page {p['url']}: {p['text']}")
    return "\n".join(lines)
