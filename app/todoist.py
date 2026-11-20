"""Your Todoist, for the assistant: add tasks, see what's due, tick things off.

Uses the Todoist API v1 with a personal API token (Todoist → Settings →
Integrations → Developer). Adding and completing tasks only touch your own list
and can be undone in Todoist, so the assistant does them directly and tells you.
"""
import httpx

BASE = "https://api.todoist.com/api/v1"


def _token() -> str:
    from app.config import get_quest_settings

    return get_quest_settings()["todoist_token"]


def enabled() -> bool:
    return bool(_token())


def _client(http: httpx.Client | None) -> httpx.Client:
    return http or httpx.Client(timeout=20)


def _error(r: httpx.Response) -> dict | None:
    if r.status_code in (401, 403):
        return {"error": "Todoist rejected the token. Check it in Sidequest Settings."}
    if r.status_code >= 400:
        return {"error": f"Todoist error {r.status_code}."}
    return None


def add_task(content: str, description: str = "", due_string: str = "", priority: int = 1,
             http: httpx.Client | None = None) -> dict:
    body = {"content": content[:500], "priority": min(max(int(priority or 1), 1), 4)}
    if description:
        body["description"] = description[:2000]
    if due_string:
        body["due_string"] = due_string[:100]
    c = _client(http)
    try:
        r = c.post(f"{BASE}/tasks", json=body, headers={"Authorization": f"Bearer {_token()}"})
    finally:
        if http is None:
            c.close()
    err = _error(r)
    if err:
        return err
    t = r.json()
    return {"ok": True, "id": t.get("id"), "content": t.get("content"), "due": (t.get("due") or {}).get("string"),
            "url": f"https://app.todoist.com/app/task/{t.get('id')}"}


def list_tasks(query: str = "today | overdue", http: httpx.Client | None = None) -> dict:
    c = _client(http)
    try:
        r = c.get(f"{BASE}/tasks/filter", params={"query": query[:200] or "today | overdue", "limit": 50},
                  headers={"Authorization": f"Bearer {_token()}"})
    finally:
        if http is None:
            c.close()
    err = _error(r)
    if err:
        return err
    return {"tasks": [{"id": t.get("id"), "content": t.get("content"), "due": (t.get("due") or {}).get("string"),
                       "priority": t.get("priority")} for t in (r.json().get("results") or [])][:50]}


def complete_task(task_id: str, http: httpx.Client | None = None) -> dict:
    c = _client(http)
    try:
        r = c.post(f"{BASE}/tasks/{str(task_id).strip()}/close", headers={"Authorization": f"Bearer {_token()}"})
    finally:
        if http is None:
            c.close()
    return _error(r) or {"ok": True, "note": "Completed (it can be reopened in Todoist)."}
