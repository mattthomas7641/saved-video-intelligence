"""App configuration loaded from environment / .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
VIDEOS_DIR = DATA_DIR / "videos"
THUMBS_DIR = DATA_DIR / "thumbnails"
DB_PATH = DATA_DIR / "db.sqlite3"

for d in (DATA_DIR, VIDEOS_DIR, THUMBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANALYSIS_MODEL = os.environ.get("ANALYSIS_MODEL", "claude-haiku-4-5-20251001").strip()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base").strip()
STALE_THRESHOLD_MONTHS = int(os.environ.get("STALE_THRESHOLD_MONTHS", "3"))
DELETE_VIDEO_AFTER_PROCESS = os.environ.get("DELETE_VIDEO_AFTER_PROCESS", "true").strip().lower() != "false"

CATEGORIES = [
    "Recipe / Cooking",
    "Product Review / Shopping",
    "Travel",
    "Fitness / Health",
    "Beauty / Fashion",
    "Comedy / Entertainment",
    "Tutorial / How-To",
    "Life Hack",
    "News / Commentary",
    "Music",
    "Motivation / Advice",
    "Book / Media Recommendation",
    "Other",
]
