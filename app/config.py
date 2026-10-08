"""App configuration loaded from environment / .env."""
import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.environ.get("SCANNER_DATA_DIR") or BASE_DIR / "data")
VIDEOS_DIR = DATA_DIR / "videos"
THUMBS_DIR = DATA_DIR / "thumbnails"
DB_PATH = DATA_DIR / "db.sqlite3"

RESUME_DIR = DATA_DIR / "resume"
JOBS_DIR = DATA_DIR / "jobs"

for d in (DATA_DIR, VIDEOS_DIR, THUMBS_DIR, RESUME_DIR, JOBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

SECRETS_PATH = DATA_DIR / "secrets.json"
RESUME_PATH = RESUME_DIR / "base_resume.md"
RESUME_PROFILE_PATH = RESUME_DIR / "profile.json"


def _load_secrets() -> dict:
    try:
        return json.loads(SECRETS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_secret(key: str, value) -> None:
    data = _load_secrets()
    data[key] = value
    SECRETS_PATH.write_text(json.dumps(data))
    os.chmod(SECRETS_PATH, 0o600)


def get_api_key() -> str:
    """Key saved from the in-app Settings page wins; falls back to the environment."""
    saved = _load_secrets().get("ANTHROPIC_API_KEY", "").strip()
    if saved:
        return saved
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def save_api_key(key: str) -> None:
    _save_secret("ANTHROPIC_API_KEY", key.strip())


def get_agent_token() -> str:
    """Bearer token the daily agent uses to call the machine-to-machine /api/* routes.
    Generated once on first use and persisted; never required for your own browser use
    of the dashboard, which hits unauthenticated routes exactly as before."""
    token = _load_secrets().get("AGENT_TOKEN", "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        _save_secret("AGENT_TOKEN", token)
    return token


def regenerate_agent_token() -> str:
    token = secrets.token_urlsafe(32)
    _save_secret("AGENT_TOKEN", token)
    return token


def get_action_settings() -> dict:
    """Per-action-type on/off toggles + the global daily-agent pause switch."""
    s = _load_secrets().get("ACTION_SETTINGS", {})
    return {
        "skill_enabled": s.get("skill_enabled", True),
        "project_enabled": s.get("project_enabled", True),
        "job_enabled": s.get("job_enabled", True),
        "agent_paused": s.get("agent_paused", False),
    }


def get_trusted_sender() -> str:
    """Your real TikTok handle - the inbox reader only acts on messages from
    this account; everything else (including random DMs to the bot account)
    is ignored. Stored without a leading '@'."""
    return _load_secrets().get("TRUSTED_SENDER", "").strip().lstrip("@")


def save_trusted_sender(handle: str) -> None:
    _save_secret("TRUSTED_SENDER", handle.strip().lstrip("@"))


def save_action_settings(**updates) -> dict:
    current = get_action_settings()
    current.update({k: v for k, v in updates.items() if v is not None})
    _save_secret("ACTION_SETTINGS", current)
    return current


def get_resume_text() -> str:
    try:
        return RESUME_PATH.read_text()
    except OSError:
        return ""


def save_resume_text(text: str) -> None:
    RESUME_PATH.write_text(text)


ANTHROPIC_API_KEY = get_api_key()  # kept for older imports; prefer get_api_key()
ANALYSIS_MODEL = os.environ.get("ANALYSIS_MODEL", "claude-haiku-4-5-20251001").strip()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base").strip()
STALE_THRESHOLD_MONTHS = int(os.environ.get("STALE_THRESHOLD_MONTHS", "3"))
DEFAULT_WORKERS = int(os.environ.get("WORKERS", "4"))
DELETE_VIDEO_AFTER_PROCESS = os.environ.get("DELETE_VIDEO_AFTER_PROCESS", "true").strip().lower() != "false"

# Broad groups -> categories. Claude assigns a category to each video; the group
# level is derived here so it also applies to videos analyzed earlier.
GROUPS = [
    ("Tech & Career", ["Tech / AI / Coding", "Career / Job Search"]),
    ("Money & Business", ["Finance / Money / Deals", "Business / Side Hustle"]),
    ("Learning & Ideas", ["Education / Learning", "Book / Media Recommendation", "Motivation / Advice", "News / Commentary"]),
    ("Food & Home", ["Recipe / Cooking", "Home / DIY / Life Hack"]),
    ("Health & Style", ["Fitness / Health", "Beauty / Fashion"]),
    ("Travel & Shopping", ["Travel", "Product Review / Shopping"]),
    ("Fun & Culture", ["Comedy / Entertainment", "Sports / Gaming", "Music / Art / Creative"]),
    ("Other", ["Other"]),
]
CATEGORIES = [c for _, cats in GROUPS for c in cats]
_CATEGORY_TO_GROUP = {c: g for g, cats in GROUPS for c in cats}
_KEYWORD_GROUPS = [
    ("sport", "Fun & Culture"), ("anime", "Fun & Culture"), ("movie", "Fun & Culture"),
    ("entertain", "Fun & Culture"), ("game", "Fun & Culture"), ("music", "Fun & Culture"),
    ("tech", "Tech & Career"), ("career", "Tech & Career"), ("coding", "Tech & Career"),
    ("financ", "Money & Business"), ("money", "Money & Business"), ("business", "Money & Business"),
    ("food", "Food & Home"), ("cook", "Food & Home"), ("recipe", "Food & Home"), ("home", "Food & Home"),
    ("health", "Health & Style"), ("fitness", "Health & Style"), ("beauty", "Health & Style"), ("fashion", "Health & Style"),
    ("travel", "Travel & Shopping"), ("shop", "Travel & Shopping"),
    ("learn", "Learning & Ideas"), ("news", "Learning & Ideas"), ("book", "Learning & Ideas"), ("advice", "Learning & Ideas"),
]


def group_of(category: str | None) -> str:
    if not category:
        return "Other"
    if category in _CATEGORY_TO_GROUP:
        return _CATEGORY_TO_GROUP[category]
    lowered = category.lower()
    for needle, group in _KEYWORD_GROUPS:
        if needle in lowered:
            return group
    return "Other"


# Claude occasionally returns a category outside the allowed list; fold those back
# into the canonical set so topics don't fragment. First matching rule wins.
_NORMALIZE_RULES = [
    (("news", "commentary", "politic"), "News / Commentary"),
    (("sport", "gaming", "game", "fantasy", "nba", "nfl", "soccer"), "Sports / Gaming"),
    (("restaurant", "deal", "coupon", "promo", "financ", "money", "invest", "crypto", "budget", "credit"), "Finance / Money / Deals"),
    (("business", "hustle", "entrepreneur", "creator", "marketing", "startup", "sales"), "Business / Side Hustle"),
    (("career", "job", "interview", "resume"), "Career / Job Search"),
    (("tech", "coding", "programming", "software", "developer", "ai"), "Tech / AI / Coding"),
    (("recipe", "cook", "food", "baking"), "Recipe / Cooking"),
    (("fitness", "health", "workout", "nutrition", "wellness"), "Fitness / Health"),
    (("beauty", "fashion", "style", "skincare", "makeup"), "Beauty / Fashion"),
    (("travel", "trip", "destination"), "Travel"),
    (("shopping", "review", "product", "gadget"), "Product Review / Shopping"),
    (("home", "diy", "hack", "cleaning", "organiz"), "Home / DIY / Life Hack"),
    (("book", "media", "movie", "film", "show", "podcast"), "Book / Media Recommendation"),
    (("educat", "learn", "tutorial", "how-to", "how to", "science", "history"), "Education / Learning"),
    (("motivat", "advice", "self", "mindset", "productiv"), "Motivation / Advice"),
    (("music", "art", "creative", "design", "photo"), "Music / Art / Creative"),
    (("comedy", "entertain", "humor", "funny", "anime", "meme"), "Comedy / Entertainment"),
]


def normalize_category(category: str | None) -> str:
    import re
    if not category:
        return "Other"
    if category in CATEGORIES:
        return category
    lowered = category.lower()
    tokens = set(re.split(r"[^a-z]+", lowered))
    for needles, canonical in _NORMALIZE_RULES:
        for n in needles:
            if (len(n) <= 3 and n in tokens) or (len(n) > 3 and n in lowered):
                return canonical
    return "Other"
