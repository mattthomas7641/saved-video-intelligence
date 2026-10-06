"""Deterministic TikTok Saved-page scraper, used by the daily sync endpoint.

This is intentionally NOT an LLM driving a browser fresh each run — it's a
small, directly testable script with a persisted login session, so "does the
saved session survive unattended, across a real day-gap" can be checked by
hand (see the plan's Phase 2 verification) instead of trusted blind.

DOM notes (verified live against tiktok.com while writing this, anonymously —
re-check against the real Favorites tab once you've logged in, since this
confirms the *shape* of the page but not the authenticated-only parts):
  - Profile tabs (Videos/Reposts/Favorites/Liked) are client-side `<p role="tab"
    data-e2e="...-tab">` elements, NOT separate URLs — there is no `/favorites`
    route to navigate straight to. The scraper clicks the tab instead.
  - The tab naming convention observed anonymously is "videos-tab",
    "drama-tab", "repost-tab", "liked-tab" — "favorites-tab" is the one we
    can't see without being logged in as the account owner (Favorites is
    always private). If that guess is wrong, FIRST_RUN_CHECKLIST below tells
    you how to fix it in one place.
  - A private/locked tab for the viewer renders a `[data-e2e*="lock"]` marker
    instead of a video grid (confirmed on a logged-out view of Liked videos).
    The scraper treats that as an error, not an empty result, so a real
    permissions problem doesn't silently look like "no new saves".

Setup (once, interactively, on the host):
    python -m app.scraper login

Then the daily agent calls scrape_new_saves() via POST /api/sync/tiktok.

FIRST_RUN_CHECKLIST — do this before trusting any schedule (see the plan's
Phase 2 verification):
  1. Run `python -m app.scraper login`, log in, close the window.
  2. Run `python -m app.scraper` (no args) and read the output. If it raises
     SelectorMismatch or FavoritesLocked, open the browser pane yourself,
     log into tiktok.com, open your profile, and read the live DOM — the
     exact element to fix is named in the error. Update _FAVORITES_TAB_SELECTORS
     below.
  3. Re-run a few hours later, then the next day, to confirm the saved
     session still works unattended (not just right after logging in).
"""
import json
import re
import sys
import time

from app.config import DATA_DIR

AUTH_STATE_PATH = DATA_DIR / "tiktok_auth_state.json"
HOME_URL = "https://www.tiktok.com/"

# Tried in order; TikTok's exact attribute for this tab is unconfirmed (see module docstring).
_FAVORITES_TAB_SELECTORS = [
    '[data-e2e="favorites-tab"]',
    '[data-e2e="favourite-tab"]',
    'p[role="tab"]:has-text("Favorites")',
]
_VIDEO_LINK_SELECTOR = "a[href*='/video/']"
_LOCK_SELECTOR = '[data-e2e*="lock"]'


class NotLoggedIn(Exception):
    """No saved session yet, or it expired — run `python -m app.scraper login`."""


class SelectorMismatch(Exception):
    """TikTok's page structure didn't match what this script expects. Likely
    needs a selector update in app/scraper.py — see FIRST_RUN_CHECKLIST."""


class FavoritesLocked(Exception):
    """The Favorites tab rendered a locked/empty state even though we're
    logged in — something's off with the account or session, not a code bug."""


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as e:  # pragma: no cover - import-time guard
        raise RuntimeError(
            "Playwright isn't installed. It ships in the Docker image; if running outside Docker, "
            "`pip install playwright && playwright install --with-deps chromium`."
        ) from e


def login() -> None:
    """Open a real, visible browser so you can log into TikTok by hand. Saves
    cookies/local-storage to AUTH_STATE_PATH on close. Run this on the host
    (not inside a headless container) since it needs a real display."""
    sync_playwright = _require_playwright()
    print("Opening a browser window. Log into TikTok, open your profile, and confirm you can see your "
          "Favorites tab, then close the window.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.tiktok.com/login")
        print("Waiting for you to finish logging in (checking every few seconds)...")
        for _ in range(600):  # up to ~20 minutes
            time.sleep(2)
            if "login" not in page.url:
                break
        context.storage_state(path=str(AUTH_STATE_PATH))
        print(f"Saved session to {AUTH_STATE_PATH}")
        browser.close()


def _click_favorites_tab(page):
    for selector in _FAVORITES_TAB_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                locator.click()
                return
        except Exception:  # noqa: BLE001 - try the next selector
            continue
    raise SelectorMismatch(
        "Couldn't find a Favorites tab on the profile page with any known selector "
        f"({_FAVORITES_TAB_SELECTORS}). TikTok likely changed its markup — inspect the live page "
        "DOM and update _FAVORITES_TAB_SELECTORS in app/scraper.py."
    )


def scrape_new_saves(max_scrolls: int = 12, scroll_pause: float = 1.5) -> list[dict]:
    """Returns every video link currently visible on the Saved/Favorites page,
    as [{"tiktok_url": str, "saved_date": None}, ...]. TikTok's Saved page
    doesn't expose per-item save timestamps, so saved_date is left for the
    caller's dedup-by-URL logic to handle — already-known links are silently
    dropped, so it's safe to return everything currently visible every time."""
    if not AUTH_STATE_PATH.exists():
        raise NotLoggedIn("No saved TikTok login session. Run: python -m app.scraper login")

    sync_playwright = _require_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(AUTH_STATE_PATH))
        page = context.new_page()

        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        profile_link = page.locator('[data-e2e="nav-profile"]').first
        if not profile_link.count():
            browser.close()
            raise SelectorMismatch("Couldn't find the profile nav link on the logged-in homepage.")
        profile_link.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)

        if "login" in page.url:
            browser.close()
            raise NotLoggedIn("Saved session expired or was rejected. Re-run: python -m app.scraper login")

        _click_favorites_tab(page)
        page.wait_for_timeout(1200)

        if page.locator(_LOCK_SELECTOR).count():
            browser.close()
            raise FavoritesLocked(
                "The Favorites tab shows a locked/empty state even though we're logged in. "
                "Open the browser yourself and check your account's Favorites tab directly."
            )

        hrefs: set[str] = set()
        stable_rounds = 0
        for _ in range(max_scrolls):
            found = page.eval_on_selector_all(_VIDEO_LINK_SELECTOR, "els => els.map(e => e.href)")
            before = len(hrefs)
            hrefs.update(found)
            if len(hrefs) == before:
                stable_rounds += 1
                if stable_rounds >= 2:  # two scrolls with nothing new: reached the end
                    break
            else:
                stable_rounds = 0
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(int(scroll_pause * 1000))

        # Refresh the saved session on disk (TikTok rotates some cookies over time).
        try:
            context.storage_state(path=str(AUTH_STATE_PATH))
        except Exception:  # noqa: BLE001 - non-fatal; next run just reuses the older state
            pass
        browser.close()

    if not hrefs:
        raise SelectorMismatch(
            f"Favorites tab loaded with no lock shown, but no links matched {_VIDEO_LINK_SELECTOR!r}. "
            "TikTok likely renders saved items differently than the public Videos grid — inspect the "
            "live DOM and update _VIDEO_LINK_SELECTOR in app/scraper.py."
        )

    video_id_re = re.compile(r"/video/(\d{8,})")
    seen_ids: set[str] = set()
    results = []
    for href in hrefs:
        m = video_id_re.search(href)
        if not m or m.group(1) in seen_ids:
            continue
        seen_ids.add(m.group(1))
        user = re.search(r"tiktok\.com/(@[\w.\-]+)/video/", href)
        url = f"https://www.tiktok.com/{user.group(1) if user else '@_'}/video/{m.group(1)}"
        results.append({"tiktok_url": url, "saved_date": None})
    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        print(json.dumps(scrape_new_saves(), indent=2))
