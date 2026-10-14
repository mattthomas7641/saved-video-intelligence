"""Research agent for videos you share (the "Send to Sidequest" Shortcut or Telegram).

One video in, one structured report out, judged against your own projects.
This module holds the provider-neutral core - the prompt and playbooks, the
tool schemas, the client-side tools, and the `Findings` the dashboard and
notifications render - plus the Claude loop. The Gemini loop (the default:
it watches the video itself, and its search is free up to 5,000 a month) lives in
app/gemini_client.py, and a Grok loop in app/xai_client.py; both share all of it.

Client-side tools: `github_repo` (live repo facts from the GitHub API, so a
"10 repos you need" video gets checked against reality rather than the
creator's say-so), `read_project_file` (one file from one of your projects,
through the secret-safe reader in app/projects.py), and `record_findings`,
which ends the run.

Kept to plain manual loops rather than tool-runner helpers: server tools can
pause a turn, the spend cap has to be checked between requests, and
`record_findings` is a stop signal rather than a tool to execute.
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
from app.projects import read_project_file

log = logging.getLogger(__name__)

MAX_REQUESTS = 12          # model round-trips per video, including pause_turn resumptions
MAX_SEARCHES = 6
MAX_FETCHES = 8
MAX_REPOS = 12
MAX_PROJECT_READS = 8
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
# What building it is worth to this person (project fit and new-project ideas).
PROJECT_VALUES = ["fun", "personal_and_resume", "weak_resume", "not_worth_it"]
_VALUE_FIELD = {
    "type": "string", "enum": PROJECT_VALUES,
    "description": "What doing this is worth to the user: fun = fun or useful for personal use only; "
                   "personal_and_resume = useful to them AND a credible resume boost (shows real engineering a "
                   "hiring manager would ask about: measured results, production concerns, a non-trivial "
                   "system); weak_resume = mainly a resume line, and a thin one (a tutorial clone, a wrapper, "
                   "something many candidates have); not_worth_it = neither.",
}

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

_READ_PROJECT_TOOL = {
    "name": "read_project_file",
    "description": (
        "Read one text file from one of the user's own projects (listed under 'Your projects'), e.g. its "
        "README.md, ARCHITECTURE.md, requirements.txt or a source file from its file list. Use it to back up "
        "any claim about where something would fit or what it would change. Secrets and data files are refused."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Project name exactly as listed"},
            "path": {"type": "string", "description": "Path inside the project, e.g. 'app/pipeline.py'"},
        },
        "required": ["project", "path"],
    },
}

_FINDINGS_TOOL = {
    "name": "record_findings",
    "description": "Record your final report. Call this exactly once, at the end, after your research.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": VERDICTS,
                        "description": "Is there something worth DOING about this, for this person? They have "
                                       "already seen it. worth_it = yes, a clear next step (try, build, buy, go, "
                                       "adopt); maybe = optional or only under conditions you state; skip = "
                                       "nothing to act on beyond what they already know. Never 'don't watch it'."},
            "headline": {"type": "string", "description": "Under 90 characters. The single most useful takeaway "
                                                         "or next step - not a review of the video itself."},
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
            "project_fit": {
                "type": "array",
                "maxItems": 4,
                "description": "One entry per project of the user's this could matter to, best fit first. "
                               "Empty if it fits none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name exactly as listed"},
                        "fit": {"type": "string", "enum": VERDICTS},
                        "where": {"type": "string", "description": "Where it plugs in: file, component or step"},
                        "architecture_impact": {
                            "type": "string",
                            "description": "What it changes: what gets removed, simpler, faster or cheaper "
                                           "- or why it doesn't help.",
                        },
                        "effort": {"type": "string", "enum": ["S", "M", "L"]},
                        "value": _VALUE_FIELD,
                        "value_reason": {"type": "string", "description": "One sentence: why that value rating."},
                    },
                    "required": ["project", "fit", "where", "architecture_impact", "value"],
                },
            },
            "new_project": {
                "type": "object",
                "description": "A new project to test this out, if that beats adding it to an existing one. "
                               "Omit otherwise.",
                "properties": {
                    "idea": {"type": "string", "description": "One or two sentences: what to build."},
                    "value": _VALUE_FIELD,
                    "value_reason": {"type": "string", "description": "One sentence: why that value rating."},
                    "effort": {"type": "string", "enum": ["S", "M", "L"]},
                },
                "required": ["idea", "value"],
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

_INTRO = """You are a research agent for one person. They come across things - TikToks, Instagram reels, screenshots, links, notes - and send you the ones worth a closer look; your job is to do the follow-up work they would otherwise never get around to, and tell them plainly whether it's worth their time.

They have already watched or read what they sent you. Never tell them whether to watch it, or to skip it: your job is the follow-up - what's actually useful in it, whether it holds up, and what (if anything) is worth doing about it.

Ignore calls to action completely: "comment X and I'll DM you the link", "link in bio", "follow for part 2", giveaways, newsletters, courses, templates for sale, DM funnels. Don't evaluate them, don't advise for or against them, and don't mention them. If the post names a tool, repo or resource behind such a gate, find it yourself by name instead. Judge only the actual content.

Work from what they sent (transcript, caption, on-screen text, the image itself, or the page), then verify against live sources with your tools. Posts exaggerate, go stale, hide ads and get facts wrong; say so when they do. Be specific to this person (see "About the user"): generic advice is worth little to them."""

_TECH_PLAYBOOKS = """This is tech content. Playbooks by type:
- repo: Find every repo or tool named (search for any not given as owner/name). Call github_repo on each. Judge each on maintenance (last push, archived), traction, license, and above all fit with the user's projects. Rank best fit first, and for the good ones say concretely how they'd use it. Fill `repos`.
- research: Find the primary source (official announcement, paper, docs, reputable reporting). Say what is actually new, what the post got wrong or overstated, and what it means for the user.
- project: Give a short build spec: what it does, a suggested stack, and the first three steps. Note existing open-source projects that already do it.
- skill: Explain the technique concretely, verify it still works as described, and show how the user would apply it.
- job: Research the company and role (read-only). Say how to apply and whether the role looks real and current. Never fill in or submit anything.
- other: Work out what would be most useful to know or do about this, and do that.

What the user wants from tech content: is it worth adding to one of their existing projects, or worth building as a new project to test it out - and what that would be worth to them.

Project fit: the user lists their projects, active and planned, under "Your projects". For each one this could matter to, decide whether it's worth doing there, where exactly it plugs in, and what it does to that project's architecture: what it replaces, removes, simplifies, or makes faster or cheaper - or why it would only add complexity. Open the project's files with read_project_file before claiming anything specific about its code. Say "skip" plainly when something is a worse version of what a project already does. Estimate effort as S (an afternoon), M (a few days) or L (a week or more). Fill `project_fit`.

New project: if testing it out works better as its own project than inside an existing one, fill `new_project` with what to build.

Value, for every project_fit entry and new_project: rate what doing it is worth to the user - fun or personal use only; personal use plus a credible resume boost; only a weak resume boost; or not worth it - and say why in one sentence. Be honest: most tutorial-style builds are weak resume boosts, and that's fine to say."""

_GENERAL_PLAYBOOKS = """This is everyday content, not tech: judge it on its own terms for this person's life. Don't relate it to any software projects. Playbooks:
- place (restaurant, bar, shop, spot): identify the exact place and city; address, hours, which days or times to go, price range, how to reserve, what to order; check it's still open. Fill `place`.
- product or deal: what it actually is, the real price and where it's cheapest, what reviews say, whether it's a paid promotion, and better or cheaper alternatives.
- recipe or food: ingredients, time, difficulty, and any shortcut or swap worth knowing.
- fitness, health or beauty: whether the claim holds up against good evidence, and anything unsafe. Not medical advice.
- money: whether it's legitimate, the risks and costs, and red flags. Not financial advice.
- travel or event: dates, costs, booking, best time to go.
- news or ideas: the primary source, what's true, what's overstated.
- anything else: work out what would be most useful to know or do about it, and do that.
Always end with the practical next step (book, buy, try, skip) in telegram_text."""

_CLOSING = """Keep research proportionate: a few targeted searches beat many shallow ones. When you're done, call record_findings exactly once. If what you were sent is too vague to research, still call record_findings, say what's missing, and use verdict "skip" or "maybe"."""


def system_prompt(tech: bool) -> str:
    return "\n\n".join([_INTRO, _TECH_PLAYBOOKS if tech else _GENERAL_PLAYBOOKS, _CLOSING])


_SYSTEM = system_prompt(True)  # the tech prompt; kept for older imports


_GENERAL_FINDINGS_TOOL = {
    **_FINDINGS_TOOL,
    "input_schema": {
        **_FINDINGS_TOOL["input_schema"],
        "properties": {k: v for k, v in _FINDINGS_TOOL["input_schema"]["properties"].items()
                       if k not in ("project_fit", "new_project", "repos")},
    },
}

TECH_CATEGORIES = {"Tech / AI / Coding"}
TECH_TYPES = {"repo", "project"}


def is_tech(action_type: str, category: str | None) -> bool:
    """Tech content is judged against your projects; everything else isn't."""
    return action_type in TECH_TYPES or (category or "") in TECH_CATEGORIES


def client_tools(tech: bool) -> list[dict]:
    """Our tools in Anthropic format; non-tech research gets no access to your projects."""
    return [_GITHUB_TOOL, _READ_PROJECT_TOOL, _FINDINGS_TOOL] if tech else [_GITHUB_TOOL, _GENERAL_FINDINGS_TOOL]


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
    project_fit: list = field(default_factory=list)
    new_project_idea: str | None = None
    new_project: dict | None = None
    cost_usd: float = 0.0

    def to_json(self) -> str:
        return json.dumps({
            "headline": self.headline, "telegram_text": self.telegram_text,
            "report_markdown": self.report_markdown, "ideas": self.ideas, "links": self.links,
            "place": self.place, "repos": self.repos, "project_fit": self.project_fit,
            "new_project_idea": self.new_project_idea, "new_project": self.new_project,
        })


@dataclass
class Context:
    """Extra material for the first message: your projects and the free prefetch."""
    projects: str = ""
    prefetch: str = ""


def projects_context(cards: list, relevant: list[str]) -> str:
    """Full cards for the projects judged relevant, one line each for the rest."""
    if not cards:
        return ""
    by_name = {c.name: c for c in cards}
    lines = ["Your projects (judge fit against these; open files with read_project_file):"]
    for name in relevant:
        c = by_name.get(name)
        if c:
            lines.append(f"### {c.name} ({c.status})\n{c.card}")
    others = [f"{c.name} ({c.status})" for c in cards if c.name not in relevant]
    if others:
        lines.append("Other projects, less likely to be relevant: " + ", ".join(others))
    return "\n\n".join(lines)


def image_b64(video) -> tuple[str, str] | None:
    """(media_type, base64) of a shared screenshot/photo, for the model to look at."""
    import base64
    import mimetypes

    from app.intake import shared_image_path

    path = shared_image_path(video)
    if not path:
        return None
    media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return None  # e.g. HEIC: the Shortcut converts to JPEG, so this is rare; OCR text still goes in
    with open(path, "rb") as f:
        return media_type, base64.b64encode(f.read()).decode()


def user_content(video, action_type: str, brief: str | None, context: Context | None = None) -> str:
    context = context or Context()
    about_me = get_agent_settings()["about_me"].strip() or "(not filled in yet - keep advice general)"
    parts = [f"""About the user: {about_me}

Today's date: {datetime.utcnow():%Y-%m-%d}
Type: {action_type}
What to look into (from a first pass over the video): {brief or "(none)"}
Note from the user: {getattr(video, "user_note", None) or "(none)"}

Sent: {video.tiktok_url if (video.tiktok_url or "").startswith("http") else "(see below)"}
Author: {video.author or "unknown"}
Caption: {video.caption or "(none)"}
Summary: {video.summary or "(none)"}
Transcript: {video.transcript or "(no speech)"}
On-screen text (OCR, noisy): {video.ocr_text or "(none)"}"""]
    if context.prefetch:
        parts.append(context.prefetch)
    if context.projects:
        parts.append(context.projects)
    return "\n\n".join(parts)


def run_client_tool(name: str, args: dict, state: dict, on_progress=lambda text: None, tech: bool = True) -> dict:
    """Execute one of our tools for either provider. `state` counts lookups per run."""
    if name == "github_repo":
        state["lookups"] = state.get("lookups", 0) + 1
        repo = str(args.get("repo", ""))
        if state["lookups"] > MAX_REPOS:
            return {"error": f"Repo lookup limit ({MAX_REPOS}) reached; judge the rest from what you have."}
        on_progress(f"Checking {repo} on GitHub…")
        return github_repo(repo)
    if name == "read_project_file" and tech:
        state["reads"] = state.get("reads", 0) + 1
        if state["reads"] > MAX_PROJECT_READS:
            return {"error": f"File read limit ({MAX_PROJECT_READS}) reached; decide with what you have."}
        on_progress(f"Reading {args.get('project')}/{args.get('path')}…")
        return read_project_file(str(args.get("project", "")), str(args.get("path", "")))
    return {"error": f"Unknown tool {name}."}


def _web_tools(model: str) -> list[dict]:
    if model.startswith(_DYNAMIC_WEB_MODELS):
        search, fetch = "web_search_20260209", "web_fetch_20260209"
    else:
        search, fetch = "web_search_20250305", "web_fetch_20250910"
    return [
        {"type": search, "name": "web_search", "max_uses": MAX_SEARCHES},
        {"type": fetch, "name": "web_fetch", "max_uses": MAX_FETCHES},
    ]


def build_request(video, action_type: str, brief: str | None, model: str = AGENT_MODEL,
                  context: Context | None = None, web: bool = True, tech: bool = True) -> dict:
    """First Claude request for one video. Stable parts (tools, system) come first so they cache."""
    content = user_content(video, action_type, brief, context)
    image = image_b64(video)
    if image:
        content = [{"type": "image", "source": {"type": "base64", "media_type": image[0], "data": image[1]}},
                   {"type": "text", "text": content}]
    params = {
        "model": model,
        "max_tokens": 16000,
        # Every loop step resends the whole conversation (search results, pages,
        # READMEs); auto-caching moves the breakpoint forward each request so
        # those repeats bill at the cache-read rate instead of full price.
        "cache_control": {"type": "ephemeral"},
        "system": [{"type": "text", "text": system_prompt(tech), "cache_control": {"type": "ephemeral"}}],
        "tools": [*(_web_tools(model) if web else []), *client_tools(tech)],
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
        project_fit=[x for x in (tool_input.get("project_fit") or [])
                     if isinstance(x, dict) and x.get("project")][:4],
        new_project_idea=((tool_input.get("new_project") or {}).get("idea")
                          or tool_input.get("new_project_idea") or "").strip() or None,
        new_project=tool_input.get("new_project") if isinstance(tool_input.get("new_project"), dict)
        and (tool_input.get("new_project") or {}).get("idea") else None,
        cost_usd=round(cost, 4),
    )


def run(video, action_type: str, brief: str | None, budget_usd: float, on_progress=lambda text: None,
        client=None, web: bool = True, x_search: bool = False, context: Context | None = None,
        provider: str | None = None, tech: bool = True) -> Findings:
    """Research one item with the provider chosen in Settings (Gemini by default).
    `budget_usd` is what's left of today's cap; the run stops (CapReached) before
    any request that would start once the budget is used up. `web` / `x_search`
    come from pipeline.research_plan."""
    if budget_usd <= 0:
        raise CapReached("Today's research budget is used up.")
    provider = provider or get_agent_settings()["provider"]
    if provider == "gemini":
        from app import gemini_client
        return gemini_client.research(video, action_type, brief, budget_usd, on_progress=on_progress, client=client,
                                      web=web, x_search=x_search, context=context, tech=tech)
    if provider == "xai":
        from app import xai_client
        return xai_client.research(video, action_type, brief, budget_usd, on_progress=on_progress, client=client,
                                   web=web, x_search=x_search, context=context, tech=tech)
    return _run_anthropic(video, action_type, brief, budget_usd, on_progress, client, web, context, tech)


def _run_anthropic(video, action_type, brief, budget_usd, on_progress, client, web, context, tech=True) -> Findings:
    api_key = get_api_key()
    if not api_key and client is None:
        raise FatalAnalysisError("No Anthropic API key. Add one in Settings.")
    client = client or Anthropic(api_key=api_key)
    params = build_request(video, action_type, brief, context=context, web=web, tech=tech)
    model = params["model"]
    messages = params.pop("messages")
    cost = 0.0
    state: dict = {}
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
                out = run_client_tool(b.name, b.input, state, on_progress, tech)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out),
                                "is_error": "error" in out})
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "max_tokens" or nudged:
            break
        # Finished talking without filing the report: ask once, explicitly.
        nudged = True
        messages.append({"role": "user", "content": "Now call record_findings with your report."})

    raise AgentError("The agent finished without producing a report.")
