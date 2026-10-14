"""Gemini for things you send: it watches the video, then researches.

Why Gemini (default since 2026-10): it takes the downloaded video file itself
- audio and frames together - so there's no Whisper/OCR step and it sees what's
on screen (code, menus, prices); paid Gemini 3 models include 5,000 free
Google Search grounded requests a month, and Google Maps grounding covers
places; and built-in tools combine with our own function tools in one request.

Two calls per item:
1. `analyze_video`: watches the video (or screenshot, or reads the text) once,
   at low media resolution, and fills the library's `record_analysis` schema
   plus a transcript and the on-screen text, which are stored on the Video.
2. `research`: a tool loop on the Interactions API, chained by
   `previous_interaction_id` so history and thought signatures stay
   server-side. It works from that text (plus the image, for screenshots), so
   the video is paid for once. Tools: google_search and google_maps (built in),
   github_repo, read_project_file (tech only) and record_findings (ours).

Prompts, playbooks, tool schemas and client-side tools are shared with the
other providers via app/agent.py.
"""
import base64
import json
import logging
import mimetypes
import time
from pathlib import Path

import httpx

from app import agent
from app.analyze import _TOOL_SCHEMA as ANALYSIS_TOOL
from app.analyze import AnalysisResult, FatalAnalysisError, build_params
from app.config import AGENT_MODEL_GEMINI, ANALYSIS_MODEL_GEMINI, get_gemini_key

log = logging.getLogger(__name__)

BACKOFF_S = 12
CALL_TIMEOUT_MS = 120_000
MAX_INLINE_BYTES = 70_000_000   # inline requests must stay under 100 MB after base64 (x4/3)

# $ per million tokens: (text/image/video input, audio input, output incl. thinking). Grounded
# searches are free up to 5,000/month on paid Gemini 3 models, then $14 / 1,000.
_PRICES = {
    "gemini-3.8-flash": (0.75, 3.00, 3.75), "gemini-3.6-flash": (0.75, 3.00, 3.75),
    "gemini-3.5-flash-lite": (0.30, 0.30, 2.50), "gemini-3.1-flash-lite": (0.25, 0.50, 1.50),
    "gemini-3.1-pro": (2.00, 2.00, 12.00), "gemini-2.5-flash": (0.30, 1.00, 2.50),
}
CACHED_DISCOUNT = 0.1

# The library's analysis schema, plus what Gemini heard and saw - stored on the
# Video so the research step (and the archive) can work from text.
_WATCH_TOOL = {
    **ANALYSIS_TOOL,
    "input_schema": {
        **ANALYSIS_TOOL["input_schema"],
        "properties": {
            **ANALYSIS_TOOL["input_schema"]["properties"],
            "transcript": {"type": "string", "description": "What is said in the video, verbatim where possible; "
                                                          "empty for a silent video or an image."},
            "on_screen_text": {"type": "string", "description": "Text shown on screen: captions, code, names, "
                                                              "prices, URLs, handles. Most important first."},
            "visual_notes": {"type": "string", "description": "Under 60 words: what is shown that the words don't "
                                                            "say (a dish, a UI, a product, a place)."},
        },
    },
}


def _client():
    from google import genai
    from google.genai import types

    key = get_gemini_key()
    if not key:
        raise FatalAnalysisError("No Gemini API key. Add one in Settings.")
    # The SDK's own retry loop backs off on 429s for minutes; turn it off so _create's
    # short retries and research()'s no-search fallback are what happens, and bound
    # every call so a stalled connection can't hang the lane.
    return genai.Client(api_key=key, http_options=types.HttpOptions(
        timeout=CALL_TIMEOUT_MS, retry_options=types.HttpRetryOptions(attempts=0)))


def _fn(tool: dict) -> dict:
    return {"type": "function", "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool["input_schema"]}


class Throttled(Exception):
    """429 from Gemini: a per-minute rate limit, or a quota of zero (e.g. Google Search
    grounding on a key without billing - the free tier doesn't include it on Gemini 3)."""


def _status(e: Exception) -> int | None:
    return getattr(e, "status_code", None) or getattr(e, "code", None)


def _create(client, retries: int = 2, **params):
    """One Interactions API call. 429s are retried with a short backoff, then raised as
    Throttled; a rejected key becomes FatalAnalysisError (only you can fix it)."""
    for attempt in range(retries + 1):
        try:
            return client.interactions.create(**params)
        except Exception as e:  # the SDK raises several error families; classify by HTTP status
            status, text = _status(e), str(e).lower()
            if status in (401, 403) and ("key" in text or "permission" in text or "unauthor" in text):
                raise FatalAnalysisError("Gemini rejected the API key. Check it in Settings.") from e
            if status != 429:
                raise
            if attempt == retries:
                raise Throttled(str(e)[:300]) from e
            log.warning("Gemini rate limited (attempt %s); retrying", attempt + 1)
            time.sleep(BACKOFF_S * (attempt + 1))


def interaction_cost(interaction, model: str) -> float:
    price_in, price_audio, price_out = next((v for k, v in _PRICES.items() if model.startswith(k)), (0.75, 3.0, 3.75))
    u = getattr(interaction, "usage", None)
    if u is None:
        return 0.0
    audio = sum((m.tokens or 0) for m in (u.input_tokens_by_modality or [])
                if str(getattr(m, "modality", "")).lower().endswith("audio"))
    total_in = u.total_input_tokens or 0
    cached = u.total_cached_tokens or 0
    other = max(total_in - audio - cached, 0)
    out = (u.total_output_tokens or 0) + (u.total_thought_tokens or 0)
    return (other * price_in + cached * price_in * CACHED_DISCOUNT + audio * price_audio
            + (u.total_tool_use_tokens or 0) * price_in + out * price_out) / 1_000_000


def _steps(interaction, kind: str) -> list:
    return [s for s in (interaction.steps or []) if getattr(s, "type", None) == kind]


def _searches(interaction) -> int:
    return len(_steps(interaction, "google_search_call")) + len(_steps(interaction, "google_maps_call"))


# ---------------- inputs ----------------

def _media(video) -> dict | None:
    """The video file (inline) or shared screenshot, as an input item."""
    path = video.local_video_path
    if path and Path(path).is_file() and Path(path).stat().st_size <= MAX_INLINE_BYTES:
        mime = mimetypes.guess_type(path)[0] or "video/mp4"
        return {"type": "video", "data": base64.b64encode(Path(path).read_bytes()).decode(), "mime_type": mime,
                "resolution": "low"}
    image = agent.image_b64(video)
    if image:
        return {"type": "image", "data": image[1], "mime_type": image[0], "resolution": "high"}
    return None


def can_watch(video) -> bool:
    path = video.local_video_path
    return bool(path and Path(path).is_file() and Path(path).stat().st_size <= MAX_INLINE_BYTES)


# ---------------- 1. watch + analyze ----------------

def analyze_video(video, client=None) -> AnalysisResult:
    """Watch once, fill record_analysis, and keep what was heard and seen on the Video."""
    client = client or _client()
    model = ANALYSIS_MODEL_GEMINI
    prompt = build_params(video)["messages"][0]["content"]
    media = _media(video)
    if media:
        prompt += ("\n\nThe video (or image) itself is attached: watch it, and fill transcript, on_screen_text and "
                   "visual_notes from it. Trust it over the transcript and OCR text above, which may be empty.")
    items = [{"type": "text", "text": prompt}] + ([media] if media else [])
    interaction = _create(client, model=model, input=items, tools=[_fn(_WATCH_TOOL)],
                          generation_config={"tool_choice": "any", "thinking_level": "low"})
    for call in _steps(interaction, "function_call"):
        if call.name == "record_analysis":
            args = dict(call.arguments or {})
            heard, seen, notes = args.pop("transcript", ""), args.pop("on_screen_text", ""), args.pop("visual_notes", "")
            u = interaction.usage
            result = AnalysisResult(input_tokens=(u.total_input_tokens or 0) if u else 0,
                                    output_tokens=(u.total_output_tokens or 0) if u else 0, **args)
            result.cost_usd = interaction_cost(interaction, model)
            if media and media["type"] == "video":
                video.transcript = heard or video.transcript or ""
                video.ocr_text = "\n".join(x for x in [seen, f"[Shown] {notes}" if notes else ""] if x) or video.ocr_text
            elif media:
                video.ocr_text = "\n".join(x for x in [video.ocr_text, seen, f"[Shown] {notes}" if notes else ""] if x)
            return result
    raise RuntimeError("Gemini did not return an analysis.")


# ---------------- project cards ----------------

def project_card(name: str, material: str, client=None) -> tuple[str, float]:
    from app.xai_client import _CARD_PROMPT

    client = client or _client()
    model = ANALYSIS_MODEL_GEMINI
    interaction = _create(client, model=model, system_instruction=_CARD_PROMPT,
                          input=f"Project: {name}\n\n{material[:24000]}",
                          generation_config={"thinking_level": "low"})
    text = (interaction.output_text or "").strip()
    if not text:
        raise RuntimeError(f"Gemini returned an empty card for {name}.")
    return text, interaction_cost(interaction, model)


# ---------------- 2. research loop ----------------

def research(video, action_type: str, brief: str | None, budget_usd: float, on_progress=lambda text: None,
             client=None, web: bool = True, x_search: bool = False, context: agent.Context | None = None,
             tech: bool = True) -> agent.Findings:
    """`x_search` is ignored: Google Search covers news here. `web` gates both search and maps.
    If grounding is refused (no billing on the key: the free tier has no Google Search on
    Gemini 3), the research is redone once without it rather than failing."""
    client = client or _client()
    if not web:
        return _research(video, action_type, brief, budget_usd, on_progress, client, False, context, tech)
    try:
        return _research(video, action_type, brief, budget_usd, on_progress, client, True, context, tech)
    except Throttled as e:
        log.warning("Gemini search unavailable (%s); researching without it", e)
        on_progress("Web search isn't available on this Gemini key; researching without it…")
        findings = _research(video, action_type, brief, budget_usd, on_progress, client, False, context, tech)
        findings.report_markdown = ("> Researched without web search: Google Search needs billing turned on for "
                                    "this Gemini key.\n\n" + findings.report_markdown)
        return findings


def _research(video, action_type, brief, budget_usd, on_progress, client, web, context, tech) -> agent.Findings:
    model = AGENT_MODEL_GEMINI
    builtins = [{"type": "google_search"}] + ([{"type": "google_maps"}] if action_type == "place" else [])
    tools = [*(builtins if web else []), *[_fn(t) for t in agent.client_tools(tech)]]
    system = agent.system_prompt(tech) + (
        "\n\nUse Google Search (and Google Maps for places) when current facts matter; a few targeted searches "
        "beat many shallow ones." if web else
        "\n\nNo web search for this one: work from what was sent, the facts provided"
        + (", GitHub lookups and the user's project files." if tech else " and general knowledge."))
    items = [{"type": "text", "text": agent.user_content(video, action_type, brief, context)}]
    image = agent.image_b64(video)
    if image:
        items.append({"type": "image", "data": image[1], "mime_type": image[0], "resolution": "high"})
    # With search on, don't sit through retries on the first call: a 429 there usually
    # means no grounding quota at all, and the caller falls back to no-search at once.
    interaction = _create(client, retries=0 if web else 2, model=model, system_instruction=system, input=items,
                          tools=tools, generation_config={"thinking_level": "medium"})
    cost, state, nudged, searches = 0.0, {}, False, 0

    for _ in range(agent.MAX_REQUESTS):
        cost += interaction_cost(interaction, model)
        searches += _searches(interaction)
        if searches:
            on_progress(f"Searched the web {searches}×…")
        calls = _steps(interaction, "function_call")
        for call in calls:
            if call.name == "record_findings":
                return agent._findings_from(dict(call.arguments or {}), cost)
        if cost >= budget_usd:
            raise agent.CapReached(f"Stopped at ${cost:.2f}: today's research budget is used up.")
        if calls:
            results = [{"type": "function_result", "name": c.name, "call_id": c.id,
                        "result": [{"type": "text", "text": json.dumps(
                            agent.run_client_tool(c.name, dict(c.arguments or {}), state, on_progress, tech))}]}
                       for c in calls]
            interaction = _create(client, model=model, input=results, tools=tools,
                                  previous_interaction_id=interaction.id)
            continue
        if nudged:
            break
        nudged = True  # finished talking without filing the report: ask once, explicitly
        interaction = _create(client, model=model, tools=tools, previous_interaction_id=interaction.id,
                              input="Now call record_findings with your report.",
                              generation_config={"tool_choice": "any"})

    raise agent.AgentError("Gemini finished without producing a report.")


def check_key(key: str) -> bool:
    """Validate a Gemini key with a free model listing."""
    try:
        r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key}, timeout=10)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
