"""Grok (xAI API) for shared videos: the first-pass analysis, project cards, and
the research loop. Much cheaper than Claude for this job (grok-4.3 is $1.25 /
$2.50 per M tokens, web search $5 per 1,000 calls), and it adds X search for
breaking AI news.

xAI's API speaks OpenAI's Responses format, so this uses the OpenAI SDK with
xAI's base URL. Built-in tools (`web_search`, `x_search`) run on xAI's side;
our function tools come back as `function_call` items, answered with
`function_call_output` on a request chained by `previous_response_id`, so the
conversation is kept server-side instead of being re-uploaded every step.

Prompt, playbooks, tool schemas and client-side tools are shared with the
Claude path in app/agent.py.
"""
import json
import logging
import time

import httpx
import openai
from openai import OpenAI

from app import agent, tracing
from app.analyze import _TOOL_SCHEMA as ANALYSIS_TOOL
from app.analyze import AnalysisResult, FatalAnalysisError, build_params
from app.config import AGENT_MODEL_XAI, ANALYSIS_MODEL_XAI, get_xai_key

log = logging.getLogger(__name__)

BASE_URL = "https://api.x.ai/v1"

# $ per million tokens: (input, cached input, output). Server-side tools are extra.
_PRICES = {
    "grok-4.7": (2.0, 0.50, 6.0), "grok-4.6": (2.0, 0.50, 6.0), "grok-4.5": (2.0, 0.30, 6.0),
    "grok-4.3": (1.25, 0.20, 2.50), "grok-4.20": (1.25, 0.20, 2.50), "grok-build": (1.0, 0.20, 2.0),
}
WEB_SEARCH_PRICE = 0.005          # $5 / 1,000 calls
X_SEARCH_PRICE_EST = 0.05         # $5 / 1,000 posts returned; ~10 posts per call is an estimate
MAX_WEB_SEARCHES = 4
MAX_X_SEARCHES = 2

_SEARCH_NOTE = (f"\n\nSearch budget for this video: at most {MAX_WEB_SEARCHES} web searches and "
                f"{MAX_X_SEARCHES} X searches, and only when the facts aren't already in front of you.")

_CARD_PROMPT = """Write a project card for an assistant that will later judge whether new tools, libraries, skills and ideas fit this project. 150-220 words, plain text, no preamble, these labelled lines:
Purpose: what it does and for whom.
Stack: languages, frameworks, services.
Architecture: main components and how data flows between them.
Pain points: limitations, slow or costly parts, TODOs or open problems visible in the material.
Next: planned work or obvious next steps.
Key files: up to 6 paths worth opening to understand it.
Only use what's in the material; write "unknown" rather than guessing."""


def _client() -> OpenAI:
    key = get_xai_key()
    if not key:
        raise FatalAnalysisError("No xAI API key. Add one in Settings.")
    return OpenAI(api_key=key, base_url=BASE_URL, timeout=180)


def _fn(tool: dict) -> dict:
    """An Anthropic-style tool schema as a Responses API function tool."""
    return {"type": "function", "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool["input_schema"]}


def _create(client: OpenAI, **params):
    try:
        started = time.monotonic()
        resp = client.responses.create(**params)
        model = params.get("model", "")
        types = [getattr(o, "type", "") for o in resp.output or []]
        tracing.model_call(model, response_cost(resp, model), getattr(resp.usage, "input_tokens", 0) or 0,
                           getattr(resp.usage, "output_tokens", 0) or 0,
                           searches=sum(1 for t in types if "search" in t), ms=int((time.monotonic() - started) * 1000))
        return resp
    except openai.AuthenticationError as e:
        raise FatalAnalysisError("xAI rejected the API key. Check it in Settings.") from e
    except (openai.PermissionDeniedError, openai.RateLimitError, openai.BadRequestError) as e:
        text = str(e).lower()
        if "credit" in text or "spending limit" in text or "billing" in text:
            raise FatalAnalysisError("Out of xAI credit. Add credit at console.x.ai, then retry.") from e
        raise


def response_cost(resp, model: str) -> float:
    price_in, price_cached, price_out = next((v for k, v in _PRICES.items() if model.startswith(k)), (2.0, 0.5, 6.0))
    usage = resp.usage
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    fresh = max((usage.input_tokens or 0) - cached, 0)
    tokens = (fresh * price_in + cached * price_cached + (usage.output_tokens or 0) * price_out) / 1_000_000
    types = [getattr(o, "type", "") for o in resp.output or []]
    web = sum(1 for t in types if "web_search" in t)
    x = sum(1 for t in types if "x_search" in t)
    return tokens + web * WEB_SEARCH_PRICE + x * X_SEARCH_PRICE_EST


def _calls(resp) -> list:
    return [o for o in resp.output or [] if getattr(o, "type", "") == "function_call"]


def _args(call) -> dict:
    try:
        args = json.loads(call.arguments or "{}")
        return args if isinstance(args, dict) else {}
    except ValueError:
        return {}


# ---------------- first-pass analysis (replaces Claude Haiku for shared videos) ----------------

def analyze_video(video, client: OpenAI | None = None) -> AnalysisResult:
    """Same `record_analysis` schema and prompt as app/analyze.py, run on Grok."""
    client = client or _client()
    model = ANALYSIS_MODEL_XAI
    content = build_params(video)["messages"][0]["content"]
    resp = _create(client, model=model, input=[_user_input(video, content)],
                   tools=[_fn(ANALYSIS_TOOL)], tool_choice="required")
    for call in _calls(resp):
        if call.name == "record_analysis":
            result = AnalysisResult(input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                                    **_args(call))
            result.cost_usd = response_cost(resp, model)
            return result
    raise RuntimeError("Grok did not return an analysis.")


# ---------------- project cards ----------------

def project_card(name: str, material: str, client: OpenAI | None = None) -> tuple[str, float]:
    client = client or _client()
    model = ANALYSIS_MODEL_XAI
    resp = _create(client, model=model, input=[
        {"role": "system", "content": _CARD_PROMPT},
        {"role": "user", "content": f"Project: {name}\n\n{material[:24000]}"},
    ])
    text = (getattr(resp, "output_text", "") or "").strip()
    if not text:
        raise RuntimeError(f"Grok returned an empty card for {name}.")
    return text, response_cost(resp, model)


# ---------------- research loop ----------------

def _user_input(video, text: str) -> dict:
    """The first user message, with the screenshot attached when one was shared."""
    image = agent.image_b64(video)
    if not image:
        return {"role": "user", "content": text}
    return {"role": "user", "content": [
        {"type": "input_text", "text": text},
        {"type": "input_image", "image_url": f"data:{image[0]};base64,{image[1]}", "detail": "high"},
    ]}


def research(video, action_type: str, brief: str | None, budget_usd: float, on_progress=lambda text: None,
             client: OpenAI | None = None, web: bool = True, x_search: bool = False,
             context: agent.Context | None = None, tech: bool = True) -> agent.Findings:
    client = client or _client()
    model = AGENT_MODEL_XAI
    tools = [*([{"type": "web_search"}] if web else []), *([{"type": "x_search"}] if x_search else []),
             *[_fn(t) for t in agent.client_tools(tech)]]
    system = agent.system_prompt(tech) + (_SEARCH_NOTE if web or x_search else
                                          "\n\nNo web search for this one: work from what was sent, the facts "
                                          "provided" + (", GitHub lookups and the user's project files." if tech
                                                        else " and general knowledge."))
    resp = _create(client, model=model, tools=tools, input=[
        {"role": "system", "content": system},
        _user_input(video, agent.user_content(video, action_type, brief, context)),
    ])
    cost, state, nudged = 0.0, {}, False

    for _ in range(agent.MAX_REQUESTS):
        cost += response_cost(resp, model)
        calls = _calls(resp)
        for call in calls:
            if call.name == "record_findings":
                return agent._findings_from(_args(call), cost)
        if cost >= budget_usd:
            raise agent.CapReached(f"Stopped at ${cost:.2f}: today's research budget is used up.")
        if calls:
            outputs = [{"type": "function_call_output", "call_id": c.call_id,
                        "output": json.dumps(agent.run_client_tool(c.name, _args(c), state, on_progress, tech))}
                       for c in calls]
            resp = _create(client, model=model, tools=tools, previous_response_id=resp.id, input=outputs)
            continue
        if nudged:
            break
        nudged = True  # finished talking without filing the report: ask once, explicitly
        resp = _create(client, model=model, tools=tools, previous_response_id=resp.id, tool_choice="required",
                       input=[{"role": "user", "content": "Now call record_findings with your report."}])

    raise agent.AgentError("Grok finished without producing a report.")


def check_key(key: str) -> bool:
    """Validate an xAI key with a free model listing."""
    try:
        r = httpx.get(f"{BASE_URL}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
