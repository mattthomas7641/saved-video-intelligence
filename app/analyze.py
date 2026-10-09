"""Categorize + summarize a video's transcript/caption/OCR text via Claude."""
import logging
import os
import time

import anthropic
from anthropic import Anthropic

from app.config import ANALYSIS_MODEL, CATEGORIES, get_api_key, normalize_category

_TOOL_SCHEMA = {
    "name": "record_analysis",
    "description": "Record the category and summary of a saved TikTok video.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": CATEGORIES},
            "summary": {"type": "string", "description": "1-2 sentences, under 45 words: what the video actually says or shows."},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "worth_rewatching_score": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=forgettable, 5=worth rewatching or acting on."},
            "worth_rewatching_reason": {"type": "string", "description": "Under 15 words."},
            "has_promo_code": {"type": "boolean"},
            "promo_code_text": {"type": "string"},
            "has_dated_offer": {"type": "boolean", "description": "Sale, deadline, event date or other time-limited offer."},
            "offer_deadline_text": {"type": "string"},
            "mentions_link_in_bio": {"type": "boolean"},
            "key_facts": {"type": "array", "items": {"type": "string"}, "maxItems": 4, "description": "Product, book or place names; ingredients."},
            "is_actionable": {
                "type": "boolean",
                "description": (
                    "True only if this video gives enough concrete detail to actually act on, as one of: "
                    "(1) SKILL - a specific, reusable technique/tool/command/prompt the viewer could directly apply "
                    "(not vague advice or a 'tips' list with no specifics); "
                    "(2) PROJECT - a software/agentic project demoed with enough detail (stack, steps, approach) "
                    "that a first version could actually be scaffolded (not just 'I built an app' with no how); "
                    "(3) JOB - the video itself is an open invitation to apply: a specific company+role being "
                    "actively offered, with some way to act on it (a link, 'DM to apply', 'comment for the form', "
                    "an email, etc). NOT a hiring manager describing their general criteria/what they look for, "
                    "NOT 'here's how hiring works at my company', NOT interview tips or career advice - those "
                    "describe hiring, they don't offer a specific role to apply to. "
                    "When in doubt whether a JOB video gives an actual way to apply, mark it not actionable. "
                    "Default to false - most saved videos are entertainment or general advice with nothing concrete to act on."
                ),
            },
            "action_type": {"type": "string", "enum": ["skill", "project", "job"], "description": "Required if is_actionable is true; omit/ignore otherwise."},
            "action_brief": {
                "type": "string",
                "description": (
                    "Required if is_actionable is true. For skill: the specific technique and how to use it. "
                    "For project: a short build spec (what it does, suggested stack/approach). "
                    "For job: 'Company | Role | any URL or application info mentioned, or none given'."
                ),
            },
        },
        "required": ["category", "summary", "tags", "worth_rewatching_score", "worth_rewatching_reason",
                     "has_promo_code", "has_dated_offer", "mentions_link_in_bio", "key_facts", "is_actionable"],
    },
}

log = logging.getLogger(__name__)

# Optional LLM Inference Gateway (github.com/mattthomas7641/llm-inference-gateway), which
# serves this task from a local model and escalates to Claude when the answer fails
# validation. A dedicated variable rather than ANTHROPIC_BASE_URL: config.py loads .env
# into os.environ, so ANTHROPIC_BASE_URL would also move the Batch API path in batch.py
# (already half price, and a gateway can't batch it any cheaper) off the direct route.
GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "").strip()

# $ per million tokens (input, output) for live requests; the Batch API is half price.
_PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-5": (2.0, 10.0), "claude-opus-5": (5.0, 25.0)}
MAX_TEXT_CHARS = 6000


def cost_usd(input_tokens: int, output_tokens: int, batch: bool = False) -> float:
    price_in, price_out = next((v for k, v in _PRICES.items() if ANALYSIS_MODEL.startswith(k)), (1.0, 5.0))
    cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    return cost * 0.5 if batch else cost


class FatalAnalysisError(Exception):
    """Out of credit, bad key, or no permission: retrying won't help until the user acts."""


class AnalysisResult:
    def __init__(self, input_tokens=0, output_tokens=0, **kwargs):
        self.category = normalize_category(kwargs.get("category"))
        self.summary = kwargs.get("summary")
        self.tags = kwargs.get("tags") or []
        self.worth_rewatching_score = kwargs.get("worth_rewatching_score")
        self.worth_rewatching_reason = kwargs.get("worth_rewatching_reason")
        self.has_promo_code = bool(kwargs.get("has_promo_code"))
        self.promo_code_text = kwargs.get("promo_code_text")
        self.has_dated_offer = bool(kwargs.get("has_dated_offer"))
        self.offer_deadline_text = kwargs.get("offer_deadline_text")
        self.mentions_link_in_bio = bool(kwargs.get("mentions_link_in_bio"))
        self.key_facts = kwargs.get("key_facts") or []
        self.is_actionable = bool(kwargs.get("is_actionable")) and bool(kwargs.get("action_type"))
        self.action_type = kwargs.get("action_type") if self.is_actionable else None
        self.action_brief = kwargs.get("action_brief") if self.is_actionable else None
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _clip(text: str | None) -> str:
    text = text or ""
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + " …"


def build_params(video) -> dict:
    """Request body for one video. Used as-is by live calls and as a Batch API entry."""
    user_note = getattr(video, "user_note", None)
    note_block = (
        f"\nNote from the person who saved this (their own stated intent - weight this heavily for "
        f"is_actionable/action_type/action_brief; direct stated intent should generally win over "
        f"inferred content): {user_note}\n"
        if user_note else ""
    )
    content = f"""Saved TikTok video.
Author: {video.author or "unknown"}
Caption: {_clip(video.caption) or "(none)"}
Hashtags: {video.hashtags or "(none)"}
Transcript: {_clip(video.transcript) or "(no speech)"}
On-screen text (OCR, noisy): {_clip(video.ocr_text) or "(none)"}
{note_block}
Call record_analysis. Score honestly: most saves are low-effort; reserve 4-5 for lasting value (recipe, tutorial, strong recommendation) or an unresolved action item."""
    return {
        "model": ANALYSIS_MODEL,
        "max_tokens": 700,
        "tools": [_TOOL_SCHEMA],
        "tool_choice": {"type": "tool", "name": "record_analysis"},
        "messages": [{"role": "user", "content": content}],
    }


def parse_message(message) -> AnalysisResult:
    for block in message.content:
        if block.type == "tool_use" and block.name == "record_analysis":
            return AnalysisResult(
                input_tokens=message.usage.input_tokens, output_tokens=message.usage.output_tokens, **block.input)
    raise RuntimeError("Claude did not return an analysis.")


def _client(api_key: str, via_gateway: bool) -> Anthropic:
    if via_gateway:
        return Anthropic(api_key=api_key, base_url=GATEWAY_URL, default_headers={"x-task": "video-tagging"})
    return Anthropic(api_key=api_key)


def analyze_video(video) -> AnalysisResult:
    api_key = get_api_key()
    if not api_key:
        raise FatalAnalysisError("No Anthropic API key. Add one in Settings.")
    via_gateway = bool(GATEWAY_URL)
    client = _client(api_key, via_gateway)

    for attempt in range(4):
        try:
            return parse_message(client.messages.create(**build_params(video)))
        except anthropic.APIConnectionError:
            if not via_gateway:
                raise
            # Gateway down or timed out: don't fail the video, go straight to Anthropic.
            log.warning("LLM gateway at %s unreachable; calling Anthropic directly", GATEWAY_URL)
            via_gateway = False
            client = _client(api_key, via_gateway)
        except anthropic.RateLimitError:
            time.sleep(15 * (attempt + 1))  # per-minute limits: wait and try again
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise FatalAnalysisError("Anthropic rejected the API key. Check it in Settings.") from e
        except anthropic.BadRequestError as e:
            if "credit" in str(e).lower():
                raise FatalAnalysisError("Out of Anthropic credit. Add credit, then resume.") from e
            raise
    raise RuntimeError("Anthropic rate limit: gave up after several tries.")
