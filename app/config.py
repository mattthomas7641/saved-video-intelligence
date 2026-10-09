"""App configuration.

Two deliberately separate mechanisms, not one:
- `Settings` below (pydantic-settings) is .env-sourced *deployment* config -
  static for a given deployment, the kind of thing that varies between dev
  and prod but not between button-clicks in the running app. Validated at
  startup; a typo or bad value fails fast instead of silently misbehaving.
- `data/secrets.json` (see get_api_key/save_api_key and friends below) is
  *runtime-mutable, user-editable-via-Settings-UI* state - the Anthropic key,
  the agent bearer token, action-type toggles, your trusted TikTok handle.
  These change while the app is running, from the app's own Settings page,
  which `Settings` (env-var-sourced, read once at process start) can't
  represent. Collapsing the two into one mechanism would be the wrong call,
  not a simplification.
"""
import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Settings below re-parses .env itself (pydantic-settings' own mechanism), but
# ANTHROPIC_API_KEY's fallback (see get_api_key) reads bare os.environ, which
# needs .env loaded into the real process environment too - this is that.
load_dotenv(BASE_DIR / ".env")


class Settings(BaseSettings):
    """.env-sourced deployment config. See module docstring for why this is
    kept separate from the data/secrets.json-backed runtime settings."""
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    scanner_data_dir: str = Field(default="", alias="SCANNER_DATA_DIR")
    analysis_model: str = Field(default="claude-haiku-4-5-20251001", alias="ANALYSIS_MODEL")
    agent_model: str = Field(default="claude-sonnet-5", alias="AGENT_MODEL")
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    public_base_url: str = Field(default="http://127.0.0.1:8787", alias="PUBLIC_BASE_URL")
    whisper_model: str = Field(default="base", alias="WHISPER_MODEL")
    stale_threshold_months: int = Field(default=3, alias="STALE_THRESHOLD_MONTHS")
    workers: int = Field(default=4, alias="WORKERS")
    delete_video_after_process: bool = Field(default=True, alias="DELETE_VIDEO_AFTER_PROCESS")


settings = Settings()

DATA_DIR = Path(settings.scanner_data_dir) if settings.scanner_data_dir.strip() else (BASE_DIR / "data")
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


# ---- Telegram intake + research agent (app/telegram_bot.py, app/agent.py) ----

def get_telegram_settings() -> dict:
    """Bot token from @BotFather, the one chat it answers to (set by pairing),
    and the one-time pairing code shown in Settings until a chat is paired."""
    s = _load_secrets().get("TELEGRAM", {})
    return {
        "bot_token": s.get("bot_token", "").strip(),
        "bot_username": s.get("bot_username", ""),
        "chat_id": s.get("chat_id"),
        "pairing_code": s.get("pairing_code", ""),
    }


def save_telegram_settings(**updates) -> dict:
    current = get_telegram_settings()
    current.update(updates)
    _save_secret("TELEGRAM", current)
    return current


def new_pairing_code() -> str:
    code = secrets.token_hex(3).upper()
    save_telegram_settings(pairing_code=code, chat_id=None)
    return code


def get_agent_settings() -> dict:
    """'About me' is injected into every research prompt so 'useful for me'
    means something; the daily cap bounds what the research agent spends."""
    s = _load_secrets().get("AGENT", {})
    return {
        "about_me": s.get("about_me", ""),
        "daily_cap_usd": float(s.get("daily_cap_usd", 3.0)),
    }


def save_agent_settings(**updates) -> dict:
    current = get_agent_settings()
    current.update({k: v for k, v in updates.items() if v is not None})
    _save_secret("AGENT", current)
    return current


ANTHROPIC_API_KEY = get_api_key()  # kept for older imports; prefer get_api_key()
# Deliberately NOT part of Settings above: the Anthropic key is runtime-mutable
# secrets.json state (see get_api_key), with this bare env var read as its one
# fallback for users who prefer .env - not "deployment config" in the same
# sense as the fields in Settings.
ANALYSIS_MODEL = settings.analysis_model.strip()
AGENT_MODEL = settings.agent_model.strip()
GITHUB_TOKEN = settings.github_token.strip()
PUBLIC_BASE_URL = settings.public_base_url.strip().rstrip("/")
WHISPER_MODEL = settings.whisper_model.strip()
STALE_THRESHOLD_MONTHS = settings.stale_threshold_months
DEFAULT_WORKERS = settings.workers
DELETE_VIDEO_AFTER_PROCESS = settings.delete_video_after_process

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
