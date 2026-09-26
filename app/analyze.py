"""Categorize + summarize a video's transcript/caption/OCR text via Claude."""
import json

from anthropic import Anthropic

from app.config import get_api_key, ANALYSIS_MODEL, CATEGORIES, normalize_category

_TOOL_SCHEMA = {
    "name": "record_analysis",
    "description": "Record the categorization and summary of a saved TikTok video.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": CATEGORIES},
            "summary": {"type": "string", "description": "1-3 sentence summary of what the video actually shows/says."},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
            "worth_rewatching_score": {
                "type": "integer", "minimum": 1, "maximum": 5,
                "description": "1=low value/forgettable, 5=genuinely worth rewatching or acting on.",
            },
            "worth_rewatching_reason": {"type": "string", "description": "One sentence justifying the score."},
            "has_promo_code": {"type": "boolean"},
            "promo_code_text": {"type": "string"},
            "has_dated_offer": {
                "type": "boolean",
                "description": "True if the video references a sale, deadline, event date, or other time-limited offer.",
            },
            "offer_deadline_text": {"type": "string"},
            "mentions_link_in_bio": {"type": "boolean"},
            "key_facts": {
                "type": "array", "items": {"type": "string"}, "maxItems": 6,
                "description": "Actionable extracted items: product/book/place names, recipe ingredients, etc.",
            },
        },
        "required": [
            "category", "summary", "tags", "worth_rewatching_score", "worth_rewatching_reason",
            "has_promo_code", "has_dated_offer", "mentions_link_in_bio", "key_facts",
        ],
    },
}


class AnalysisResult:
    def __init__(self, **kwargs):
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


def analyze(caption: str, hashtags: str, author: str, transcript: str, ocr_text: str) -> AnalysisResult:
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("No Anthropic API key. Add one in Settings.")

    client = Anthropic(api_key=api_key)

    content = f"""Here is everything extracted from one saved TikTok video:

Author: {author or "unknown"}
Caption: {caption or "(none)"}
Hashtags: {hashtags or "(none)"}

Spoken transcript:
{transcript or "(no speech detected)"}

On-screen text (OCR, may be noisy/partial):
{ocr_text or "(none detected)"}

Analyze this and call record_analysis with your findings. Be honest about
worth_rewatching_score — most saved videos are low-effort saves that aren't
actually worth revisiting; reserve 4-5 for content with real lasting value
(a recipe, a tutorial, a strong recommendation) or an unresolved action item."""

    response = client.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=1024,
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "record_analysis"},
        messages=[{"role": "user", "content": content}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "record_analysis":
            return AnalysisResult(**block.input)

    raise RuntimeError("Claude did not return a tool_use analysis block.")
