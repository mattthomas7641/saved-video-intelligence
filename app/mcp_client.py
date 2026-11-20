"""Sidequest as an MCP *client*: the assistant can use other people's MCP servers.

Only a pinned allowlist of servers and, within each, of tools: a server can't add
tools to the assistant by itself, and everything here is search-only (read). An
MCP server that could send, book or pay would have to go through the approval
dispatcher like every other side-effecting action, so none is listed yet.

- Kiwi.com (official, free, no key): flight search; results carry booking links.
- seats.aero (official, free without an account for cached award space up to
  60 days out, personal use allowed): award seats, and which airline and hotel
  programs each card currency (e.g. Chase Ultimate Rewards, "chaseus") transfers to.

Tool schemas are cached for a few hours; calls run synchronously from the
assistant's worker thread.
"""
import json
import logging
import time

import anyio

log = logging.getLogger(__name__)

SERVERS = {
    "kiwi": {"url": "https://mcp.kiwi.com", "tools": {"search-flight"},
             "about": "Kiwi.com flight search (cash fares, with booking links)"},
    "seats": {"url": "https://seats.aero/mcp",
              "tools": {"get_flights", "get_flight_details", "list_transfer_partners",
                        "list_transfer_program_mileage_programs", "search_airports"},
              "about": "seats.aero award (points) seat availability and card transfer partners"},
}
CACHE_S = 6 * 3600
RESULT_CHARS = 12_000
SEP = "__"

_cache: dict = {"at": 0.0, "tools": []}


def _name(server: str, tool: str) -> str:
    return f"{server}{SEP}{tool.replace('-', '_')}"


def clean_schema(schema: dict) -> dict:
    """A server's JSON schema in the subset Gemini function declarations accept: one type
    per field (nullable unions collapse to the non-null type), no titles/defaults/extras."""
    if not isinstance(schema, dict):
        return {"type": "string"}
    out: dict = {}
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t:
        out["type"] = t
    for key in ("description", "minimum", "maximum", "minLength", "maxLength", "format"):
        if key in schema:
            out[key] = schema[key]
    if "enum" in schema:
        out["enum"] = [v for v in schema["enum"] if v is not None]
    if "properties" in schema:
        out["type"] = "object"
        out["properties"] = {k: clean_schema(v) for k, v in schema["properties"].items()}
        if schema.get("required"):
            out["required"] = [r for r in schema["required"] if r in schema["properties"]]
    if "items" in schema:
        out["items"] = clean_schema(schema["items"])
    if "anyOf" in schema and "type" not in out:
        first = next((s for s in schema["anyOf"] if isinstance(s, dict) and s.get("type") != "null"), {})
        out.update(clean_schema(first))
    return out or {"type": "string"}


async def _list(url: str):
    from mcp.client.client import Client

    async with Client(url) as c:
        return (await c.list_tools()).tools


def tools(lister=None) -> list[dict]:
    """Gemini function declarations for every allowlisted tool that its server offers right now."""
    if time.monotonic() - _cache["at"] < CACHE_S and _cache["tools"]:
        return _cache["tools"]
    out = []
    for key, server in SERVERS.items():
        try:
            listed = lister(server["url"]) if lister else anyio.run(_list, server["url"])
        except Exception as e:  # noqa: BLE001 - one server down mustn't take the assistant down
            log.warning("MCP server %s unavailable: %s", key, type(e).__name__)
            continue
        for t in listed:
            if t.name in server["tools"]:
                out.append({"type": "function", "name": _name(key, t.name),
                            "description": f"[{server['about']}] {(t.description or '')[:900]}",
                            "parameters": clean_schema(t.input_schema)})
    _cache.update(at=time.monotonic(), tools=out)
    return out


def handles(name: str) -> bool:
    server, _, tool = name.partition(SEP)
    return server in SERVERS and bool(tool)


async def _call(url: str, tool: str, args: dict) -> str:
    from mcp.client.client import Client

    async with Client(url) as c:
        result = await c.call_tool(tool, args)
    parts = [getattr(p, "text", "") for p in (result.content or [])]
    structured = getattr(result, "structured_content", None)
    text = "\n".join(p for p in parts if p) or (json.dumps(structured) if structured else "")
    return ("ERROR: " if getattr(result, "is_error", False) else "") + text


def call(name: str, args: dict, caller=None) -> dict:
    """Run an allowlisted tool. Returns {"result": text} or {"error": ...}."""
    server, _, tool = name.partition(SEP)
    conf = SERVERS.get(server)
    real = next((t for t in (conf or {}).get("tools", ()) if t.replace("-", "_") == tool), None)
    if conf is None or real is None:
        return {"error": f"{name} isn't an allowed tool."}
    try:
        text = caller(conf["url"], real, args) if caller else anyio.run(_call, conf["url"], real, args)
    except Exception as e:  # noqa: BLE001
        log.warning("MCP tool %s failed: %s", name, type(e).__name__)
        return {"error": f"{name} failed ({type(e).__name__}); try another way."}
    if text.startswith("ERROR: "):
        return {"error": text[7:RESULT_CHARS]}
    return {"result": text[:RESULT_CHARS]}
