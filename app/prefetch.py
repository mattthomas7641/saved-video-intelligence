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


def page_text(url: str, http: httpx.Client) -> str:
    try:
        r = http.get(url, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        return f"(couldn't fetch: {e})"
    body = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", r.text)
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
