"""Categorize + summarize a video's transcript/caption/OCR text via Claude."""
import time

import anthropic
from anthropic import Anthropic

from app.config import get_api_key, ANALYSIS_MODEL, CATEGORIES, normalize_category

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
        },
        "required": ["category", "summary", "tags", "worth_rewatching_score", "worth_rewatching_reason",
                     "has_promo_code", "has_dated_offer", "mentions_link_in_bio", "key_facts"],
    },
}

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
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _clip(text: str | None) -> str:
    text = text or ""
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + " …"


def build_params(video) -> dict:
    """Request body for one video. Used as-is by live calls and as a Batch API entry."""
    content = f"""Saved TikTok video.
Author: {video.author or "unknown"}
Caption: {_clip(video.caption) or "(none)"}
Hashtags: {video.hashtags or "(none)"}
Transcript: {_clip(video.transcript) or "(no speech)"}
On-screen text (OCR, noisy): {_clip(video.ocr_text) or "(none)"}

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


def analyze_video(video) -> AnalysisResult:
    api_key = get_api_key()
    if not api_key:
        raise FatalAnalysisError("No Anthropic API key. Add one in Settings.")
    client = Anthropic(api_key=api_key)

    for attempt in range(4):
        try:
            return parse_message(client.messages.create(**build_params(video)))
        except anthropic.RateLimitError:
            time.sleep(15 * (attempt + 1))  # per-minute limits: wait and try again
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise FatalAnalysisError("Anthropic rejected the API key. Check it in Settings.") from e
        except anthropic.BadRequestError as e:
            if "credit" in str(e).lower():
                raise FatalAnalysisError("Out of Anthropic credit. Add credit, then resume.") from e
            raise
    raise RuntimeError("Anthropic rate limit: gave up after several tries.")
