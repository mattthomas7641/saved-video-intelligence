"""Notes in your Notion: the assistant saves notes as pages under one page you choose,
and can search them.

Uses a Notion internal integration token (notion.so/my-integrations) and a parent
page shared with that integration (page → ··· → Connections). The integration
only sees pages you share with it.
"""
import re

import httpx

BASE = "https://api.notion.com/v1"
VERSION = "2022-06-28"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX32 = re.compile(r"[0-9a-f]{32}", re.I)


def _settings() -> tuple[str, str]:
    from app.config import get_quest_settings

    s = get_quest_settings()
    return s["notion_token"], s["notion_page"]


def enabled() -> bool:
    token, page = _settings()
    return bool(token and page)


def page_id(url_or_id: str) -> str:
    """The page id from a Notion page link (…/Notes-1a2b…) or a bare id, as a UUID ('' if none)."""
    text = re.sub(r"[?#].*$", "", (url_or_id or "").strip()).rstrip("/")
    uuid = _UUID.search(text)
    last = text.split("/")[-1].split("-")[-1]
    raw = uuid.group().replace("-", "") if uuid else (last if _HEX32.fullmatch(last) else "")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}".lower() if raw else ""


def _headers() -> dict:
    return {"Authorization": f"Bearer {_settings()[0]}", "Notion-Version": VERSION}


def _error(r: httpx.Response) -> dict | None:
    if r.status_code in (401, 403):
        return {"error": "Notion rejected the token. Check it in Sidequest Settings."}
    if r.status_code == 404:
        return {"error": "Notion can't see the notes page: share it with the integration (··· → Connections)."}
    if r.status_code >= 400:
        return {"error": f"Notion error {r.status_code}."}
    return None


def _paragraphs(text: str) -> list[dict]:
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk[:1900]}}]}}
            for chunk in [p for p in text.split("\n\n") if p.strip()][:50]]


def save_note(title: str, text: str, http: httpx.Client | None = None) -> dict:
    parent = page_id(_settings()[1])
    if not parent:
        return {"error": "No notes page set in Sidequest Settings."}
    body = {"parent": {"page_id": parent},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title[:200]}}]}},
            "children": _paragraphs(text)}
    c = http or httpx.Client(timeout=20)
    try:
        r = c.post(f"{BASE}/pages", json=body, headers=_headers())
    finally:
        if http is None:
            c.close()
    err = _error(r)
    if err:
        return err
    return {"ok": True, "url": r.json().get("url")}


def search_notes(query: str, http: httpx.Client | None = None) -> dict:
    c = http or httpx.Client(timeout=20)
    try:
        r = c.post(f"{BASE}/search", json={"query": query[:200], "filter": {"property": "object", "value": "page"},
                                           "page_size": 10}, headers=_headers())
    finally:
        if http is None:
            c.close()
    err = _error(r)
    if err:
        return err
    out = []
    for p in r.json().get("results") or []:
        title_prop = next((v for v in (p.get("properties") or {}).values() if v.get("type") == "title"), {})
        title = "".join(t.get("plain_text", "") for t in title_prop.get("title") or [])
        out.append({"title": title or "(untitled)", "url": p.get("url"), "edited": (p.get("last_edited_time") or "")[:10]})
    return {"pages": out}
