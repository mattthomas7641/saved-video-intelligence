"""Research agent for videos you share to the Telegram bot.

One video in, one structured report out. The model gets Anthropic's server-side
web search + web fetch (they run on Anthropic's side, no loop work for us), plus
two tools of ours: `github_repo` (live repo facts from the GitHub API, so a
"10 repos you need" video gets checked against reality rather than the
creator's say-so) and `record_findings`, which ends the run with the report the
dashboard and Telegram render.

Kept to a plain manual loop rather than a tool-runner helper: the server tools
can pause a turn (`pause_turn`), the spend cap has to be checked between
requests, and `record_findings` is a stop signal rather than a tool to execute.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import anthropic
import httpx
from anthropic import Anthropic

from app.analyze import FatalAnalysisError
from app.config import AGENT_MODEL, GITHUB_TOKEN, get_agent_settings, get_api_key

log = logging.getLogger(__name__)

MAX_REQUESTS = 12          # model round-trips per video, including pause_turn resumptions
MAX_SEARCHES = 6
MAX_FETCHES = 8
MAX_REPOS = 12
README_CHARS = 3000

# $ per million tokens (input, output). Web search is billed separately at $10 / 1,000.
_PRICES = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
           "claude-opus-4": (5.0, 25.0), "claude-sonnet-4": (3.0, 15.0)}
_SEARCH_PRICE = 0.01

# The dynamic-filtering web tools need a current model; older ones get the basic variants.
_DYNAMIC_WEB_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
                       "claude-opus-4-6", "claude-sonnet-4-6", "claude-fable")
# Server-side refusal fallbacks ("default" routing), offered on the Opus/Fable tier.
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable")

VERDICTS = ["worth_it", "maybe", "skip"]

_GITHUB_TOOL = {
    "name": "github_repo",
    "description": (
        "Look up a GitHub repository's live facts: description, stars, forks, last push date, license, "
        "open issues, topics, archived flag, and the start of its README. Use it for every repo the video "
        "names, before judging it. Accepts 'owner/name' or a github.com URL. If you only know a project's "
        "name, find its repo with web search first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"repo": {"type": "string", "description": "owner/name, e.g. 'torvalds/linux'"}},
        "required": ["repo"],
    },
}

_FINDINGS_TOOL = {
    "name": "record_findings",
    "description": "Record your final report. Call this exactly once, at the end, after your research.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": VERDICTS,
                        "description": "Is this worth the user's time, given what you know about them?"},
            "headline": {"type": "string", "description": "Under 90 characters. The single most useful takeaway."},
            "telegram_text": {
                "type": "string",
                "description": "Under 600 characters, plain text, for a phone notification-sized reply. "
                               "Lead with the answer; at most 3 short lines or bullets.",
            },
            "report_markdown": {
                "type": "string",
                "description": "The full write-up for the dashboard, in Markdown. Use short sections and "
                               "bullets; no top-level heading (the headline is shown above it).",
            },
            "ideas": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                      "description": "Concrete ways the user could use this, specific to them."},
            "links": {
                "type": "array", "maxItems": 8,
                "items": {"type": "object", "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                          "required": ["title", "url"]},
            },
            "place": {
                "type": "object",
                "description": "Only for place videos.",
                "properties": {
                    "name": {"type": "string"}, "address": {"type": "string"},
                    "neighborhood": {"type": "string"}, "city": {"type": "string"},
                    "hours": {"type": "string"}, "best_times": {"type": "string"},
                    "price": {"type": "string"}, "reservations": {"type": "string"},
                    "status": {"type": "string", "description": "open, temporarily closed, permanently closed, or unknown"},
                },
            },
            "repos": {
                "type": "array",
                "description": "Only for repo videos: one entry per repo checked, best fit first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}, "url": {"type": "string"},
                        "stars": {"type": "integer"}, "last_push": {"type": "string"},
                        "license": {"type": "string"}, "what_it_does": {"type": "string"},
                        "fit": {"type": "string", "enum": VERDICTS},
                        "how_to_use": {"type": "string"},
                    },
                    "required": ["name", "fit"],
                },
            },
        },
        "required": ["verdict", "headline", "telegram_text", "report_markdown"],
    },
}

_SYSTEM = """You are a research agent for one person. They save TikTok videos and share the useful ones to you; your job is to do the follow-up work they would otherwise never get around to, and tell them plainly whether it's worth their time.

Work from the video's transcript, caption and on-screen text, then verify against live sources with your tools. Videos exaggerate, go stale and get facts wrong; say so when they do. Be specific to this person (see "About the user"): generic advice is worth little to them.

Playbooks by type:
- repo: Find every repo or tool the video names (search for any not given as owner/name). Call github_repo on each. Judge each on maintenance (last push, archived), traction, license, and above all fit with the user's projects. Rank best fit first, and for the good ones say concretely how they'd use it. Fill `repos`.
- research: Find the primary source (official announcement, paper, docs, reputable reporting). Say what is actually new, what the video got wrong or overstated, and what it means for the user. Cite links.
- place: Identify the exact place and city. Find address, hours, which days or times to go, price range, how to reserve, and what to order. Check that it's still open. Fill `place`.
- project: Give a short build spec: what it does, a suggested stack, and the first three steps. Note existing open-source projects that already do it.
- skill: Explain the technique concretely, verify it still works as described, and show how the user would apply it.
- job: Research the company and role (read-only). Say how to apply and whether the role looks real and current. Never fill in or submit anything.
- other: Work out what would be most useful to know or do about this video, and do that.

Keep research proportionate: a few targeted searches beat many shallow ones. When you're done, call record_findings exactly once. If the video is too vague to research, still call record_findings, say what's missing, and use verdict "skip" or "maybe"."""


class AgentError(Exception):
    """The run failed in a way worth reporting to the user (refusal, no report, bad output)."""


class CapReached(Exception):
    """Continuing would exceed the daily research budget set in Settings."""


@dataclass
class Findings:
    verdict: str
    headline: str
    telegram_text: str
    report_markdown: str
    ideas: list = field(default_factory=list)
    links: list = field(default_factory=list)
    place: dict | None = None
    repos: list = field(default_factory=list)
    cost_usd: float = 0.0

    def to_json(self) -> str:
        return json.dumps({
            "headline": self.headline, "telegram_text": self.telegram_text,
            "report_markdown": self.report_markdown, "ideas": self.ideas, "links": self.links,
            "place": self.place, "repos": self.repos,
        })


def _web_tools(model: str) -> list[dict]:
    if model.startswith(_DYNAMIC_WEB_MODELS):
        search, fetch = "web_search_20260209", "web_fetch_20260209"
    else:
        search, fetch = "web_search_20250305", "web_fetch_20250910"
    return [
        {"type": search, "name": "web_search", "max_uses": MAX_SEARCHES},
        {"type": fetch, "name": "web_fetch", "max_uses": MAX_FETCHES},
    ]


def build_request(video, action_type: str, brief: str | None, model: str = AGENT_MODEL) -> dict:
    """First request body for one video. Stable parts (tools, system) come first so they cache."""
    about_me = get_agent_settings()["about_me"].strip() or "(not filled in yet - keep advice general)"
    source_url = video.tiktok_url
    content = f"""About the user: {about_me}

Today's date: {datetime.utcnow():%Y-%m-%d}
Type: {action_type}
What to look into (from a first pass over the video): {brief or "(none)"}
Note from the user: {getattr(video, "user_note", None) or "(none)"}

Video: {source_url}
Author: {video.author or "unknown"}
Caption: {video.caption or "(none)"}
Summary: {video.summary or "(none)"}
Transcript: {video.transcript or "(no speech)"}
On-screen text (OCR, noisy): {video.ocr_text or "(none)"}"""
    params = {
        "model": model,
        "max_tokens": 16000,
        # Every loop step resends the whole conversation (search results, pages,
        # READMEs); auto-caching moves the breakpoint forward each request so
        # those repeats bill at the cache-read rate instead of full price.
        "cache_control": {"type": "ephemeral"},
        "system": [{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "tools": [*_web_tools(model), _GITHUB_TOOL, _FINDINGS_TOOL],
        "messages": [{"role": "user", "content": content}],
    }
    if model.startswith(_FALLBACK_MODELS):
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    return params


def request_cost(usage, model: str = AGENT_MODEL) -> float:
    price_in, price_out = next((v for k, v in _PRICES.items() if model.startswith(k)), (5.0, 25.0))
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    tokens = (usage.input_tokens * price_in + cache_write * price_in * 1.25 + cache_read * price_in * 0.1
              + usage.output_tokens * price_out) / 1_000_000
    server = getattr(usage, "server_tool_use", None)
    searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
    return tokens + searches * _SEARCH_PRICE


def github_repo(repo: str, http: httpx.Client | None = None) -> dict:
    """Live facts for one repo. Returns {"error": ...} rather than raising, so the
    model can recover (e.g. search for the right owner) instead of the run dying."""
    slug = repo.strip().removeprefix("https://").removeprefix("http://").removeprefix("www.")
    slug = slug.removeprefix("github.com/").strip("/")
    parts = slug.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return {"error": f"Expected owner/name, got {repo!r}. Search for the repo first."}
    owner, name = parts[0], parts[1].removesuffix(".git")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "saved-video-intelligence"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    client = http or httpx.Client(timeout=15)
    try:
        r = client.get(f"https://api.github.com/repos/{owner}/{name}", headers=headers)
        if r.status_code == 404:
            return {"error": f"{owner}/{name} not found on GitHub. Search for the right owner/name."}
        if r.status_code in (403, 429):
            return {"error": "GitHub rate limit reached; judge from the web instead."}
        r.raise_for_status()
        d = r.json()
        readme = ""
        rr = client.get(f"https://api.github.com/repos/{owner}/{name}/readme",
                        headers={**headers, "Accept": "application/vnd.github.raw"})
        if rr.status_code == 200:
            readme = rr.text[:README_CHARS]
        return {
            "full_name": d.get("full_name"), "url": d.get("html_url"), "description": d.get("description"),
            "stars": d.get("stargazers_count"), "forks": d.get("forks_count"),
            "open_issues": d.get("open_issues_count"), "last_push": (d.get("pushed_at") or "")[:10],
            "created": (d.get("created_at") or "")[:10], "archived": d.get("archived"),
            "license": (d.get("license") or {}).get("spdx_id"), "language": d.get("language"),
            "topics": d.get("topics") or [], "homepage": d.get("homepage"), "readme_start": readme,
        }
    except httpx.HTTPError as e:
        return {"error": f"GitHub lookup failed: {e}"}
    finally:
        if http is None:
            client.close()


def _findings_from(tool_input: dict, cost: float) -> Findings:
    verdict = tool_input.get("verdict") if tool_input.get("verdict") in VERDICTS else "maybe"
    headline = (tool_input.get("headline") or "").strip()
    report = (tool_input.get("report_markdown") or "").strip()
    if not headline or not report:
        raise AgentError("The agent's report was missing a headline or body.")
    return Findings(
        verdict=verdict, headline=headline[:140],
        telegram_text=(tool_input.get("telegram_text") or headline).strip()[:900],
        report_markdown=report,
        ideas=[i for i in (tool_input.get("ideas") or []) if isinstance(i, str)][:5],
        links=[x for x in (tool_input.get("links") or []) if isinstance(x, dict) and x.get("url")][:8],
        place=tool_input.get("place") or None,
        repos=[x for x in (tool_input.get("repos") or []) if isinstance(x, dict)][:MAX_REPOS],
        cost_usd=round(cost, 4),
    )


def run(video, action_type: str, brief: str | None, budget_usd: float,
        on_progress=lambda text: None, client: Anthropic | None = None) -> Findings:
    """Research one video. `budget_usd` is what's left of today's cap; the run stops
    (CapReached) before any request that would start once the budget is used up."""
    api_key = get_api_key()
    if not api_key and client is None:
        raise FatalAnalysisError("No Anthropic API key. Add one in Settings.")
    if budget_usd <= 0:
        raise CapReached("Today's research budget is used up.")
    client = client or Anthropic(api_key=api_key)
    params = build_request(video, action_type, brief)
    model = params["model"]
    messages = params.pop("messages")
    cost = 0.0
    lookups = 0
    nudged = False

    for _ in range(MAX_REQUESTS):
        if cost >= budget_usd:
            raise CapReached(f"Stopped at ${cost:.2f}: today's research budget is used up.")
        try:
            response = client.beta.messages.create(**params, messages=messages)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise FatalAnalysisError("Anthropic rejected the API key. Check it in Settings.") from e
        except anthropic.BadRequestError as e:
            if "credit" in str(e).lower():
                raise FatalAnalysisError("Out of Anthropic credit. Add credit, then resume.") from e
            raise
        cost += request_cost(response.usage, model)

        if response.stop_reason == "refusal":
            raise AgentError("Claude declined to research this video.")
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            on_progress("Still searching…")
            continue  # server-side tool loop hit its limit; resend and it resumes

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        for b in tool_uses:
            if b.name == "record_findings":
                return _findings_from(b.input, cost)
        if tool_uses:
            results = []
            for b in tool_uses:
                if b.name == "github_repo":
                    lookups += 1
                    repo = str(b.input.get("repo", ""))
                    on_progress(f"Checking {repo} on GitHub…")
                    out = (github_repo(repo) if lookups <= MAX_REPOS
                           else {"error": f"Repo lookup limit ({MAX_REPOS}) reached; judge the rest from the web."})
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out),
                                    "is_error": "error" in out})
                else:
                    results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                    "content": f"Unknown tool {b.name}."})
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "max_tokens" or nudged:
            break
        # Finished talking without filing the report: ask once, explicitly.
        nudged = True
        messages.append({"role": "user", "content": "Now call record_findings with your report."})

    raise AgentError("The agent finished without producing a report.")
