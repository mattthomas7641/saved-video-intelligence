"""Deterministic TikTok Saved-page scraper, used by the daily sync endpoint.

This is intentionally NOT an LLM driving a browser fresh each run — it's a
small, directly testable script with a persisted login session, so "does the
saved session survive unattended, across a real day-gap" can be checked by
hand (see the plan's Phase 2 verification) instead of trusted blind.

DOM notes (confirmed live, logged in as the account owner, during Phase 2
verification — not guessed):
  - Profile tabs (Videos/Short dramas/Reposts/Favorites/Liked) are client-side
    `<p role="tab">` elements, NOT separate URLs — there's no `/favorites`
    route to navigate straight to. The scraper clicks the tab instead.
  - Every other tab carries a `data-e2e="...-tab"` attribute (videos-tab,
    drama-tab, repost-tab, liked-tab) - **Favorites does not**. It has to be
    found by its accessible role+name ("tab" named "Favorites"), not by a
    data-e2e selector, which is why an earlier version of this script guessed
    wrong. `page.get_by_role("tab", name="Favorites", exact=True)` is what
    actually works.
  - Headless Chromium with default settings got served a degraded profile
    page (generic public tab set, a "Something went wrong" content error,
    no Favorites tab at all) even with a valid logged-in session - consistent
    with TikTok treating stock headless automation as suspicious. Basic
    countermeasures (a real desktop user-agent, `navigator.webdriver` patched
    out, `--disable-blink-features=AutomationControlled`) reliably fixed it
    in testing. This is inherently adversarial and could stop working if
    TikTok tightens detection further - that's the fragility you accepted
    when choosing this approach over the export-file alternative.

Setup (once, interactively, on the host):
    python -m app.scraper login

Then the daily agent calls scrape_new_saves() via POST /api/sync/tiktok.

If TikTok changes its markup again, scrape_new_saves() raises SelectorMismatch
or FavoritesLocked naming the specific thing that didn't match - open the
browser pane yourself, log into tiktok.com, open your own profile, and read
the live DOM to find the fix, the same way this version was grounded.
"""
import json
import re
import sys
import time

from app.config import DATA_DIR

_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

AUTH_STATE_PATH = DATA_DIR / "tiktok_auth_state.json"
HOME_URL = "https://www.tiktok.com/"

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


_SESSION_COOKIES = {"sessionid", "sid_tt", "sid_guard"}


def login() -> None:
    """Open a real, visible browser so you can log into TikTok by hand. Detects
    a successful login by watching for TikTok's own session cookies (not by
    watching one tab's URL — TikTok's login can finish in a popup or a second
    tab, e.g. Google/Apple sign-in or a QR code, which a single-tab URL check
    would never notice). Saves the session and closes the browser itself once
    it sees you're logged in — you don't need to close anything by hand.
    Run this on the host (not inside a headless container); it needs a real
    display."""
    sync_playwright = _require_playwright()
    print("Opening a browser window. Log into TikTok however you like, then open your profile and "
          "confirm you can see your Favorites tab. I'll detect it and close the window myself — "
          "just wait for 'Saved session' here rather than closing it yourself.", flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.tiktok.com/login")

        print("Waiting for a logged-in session (checking every couple seconds)...", flush=True)
        logged_in = False
        for _ in range(600):  # up to ~20 minutes
            time.sleep(2)
            try:
                cookie_names = {c["name"] for c in context.cookies()}
            except Exception:
                break  # browser/window was closed before we saw a login
            if cookie_names & _SESSION_COOKIES:
                logged_in = True
                break

        if not logged_in:
            print("Didn't detect a logged-in session before the window closed (or this timed out). "
                  "Run `python -m app.scraper login` again and wait for 'Saved session' before doing "
                  "anything else — don't close the window yourself.", flush=True)
            return

        try:
            context.storage_state(path=str(AUTH_STATE_PATH))
        except Exception as e:
            print(f"Logged in, but couldn't save the session in time: {e}. Try again.", flush=True)
            return
        print(f"Saved session to {AUTH_STATE_PATH}", flush=True)
        try:
            browser.close()
        except Exception:
            pass


def _click_favorites_tab(page):
    # Unlike the other profile tabs, Favorites carries no data-e2e attribute
    # (confirmed live) - found by accessible role+name instead, which is also
    # the more change-resistant choice since it doesn't depend on a generated
    # CSS class or an attribute TikTok may add later.
    try:
        page.get_by_role("tab", name="Favorites", exact=True).click(timeout=15000)
    except Exception as e:
        raise SelectorMismatch(
            "Couldn't find/click a tab named 'Favorites' on the profile page. TikTok likely changed "
            "its markup or served a degraded page - inspect the live DOM and update "
            "_click_favorites_tab in app/scraper.py."
        ) from e


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
        # The basic automation countermeasures below (see module docstring) were
        # necessary in testing - without them TikTok served a degraded page with
        # no Favorites tab at all, even with a valid logged-in session.
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            storage_state=str(AUTH_STATE_PATH), viewport={"width": 1280, "height": 900},
            user_agent=_DESKTOP_UA, locale="en-US",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        # Getting to a clean, fully-rendered Favorites tab is empirically
        # inconsistent run to run (observed in testing: identical code
        # succeeded on one attempt and got a degraded/bot-suspicious page on
        # the next) - retry with a fresh navigation before treating it as a
        # real failure, rather than raising on the first flaky attempt.
        last_error: Exception | None = None
        reached_favorites = False
        for attempt in range(3):
            try:
                # networkidle (not domcontentloaded): this is a heavily
                # client-rendered page, and the nav isn't in the initial HTML -
                # domcontentloaded fires before TikTok's JS has painted it,
                # which otherwise looks exactly like a genuinely missing
                # selector.
                page.goto(HOME_URL, wait_until="networkidle", timeout=30000)
                page.locator('[data-e2e="nav-profile"]').first.click(timeout=15000)
                page.wait_for_load_state("networkidle", timeout=15000)

                if "login" in page.url:
                    browser.close()
                    raise NotLoggedIn("Saved session expired or was rejected. Re-run: python -m app.scraper login")

                _click_favorites_tab(page)
                page.wait_for_timeout(1200)
                reached_favorites = True
                break
            except NotLoggedIn:
                raise
            except Exception as e:  # noqa: BLE001 - genuinely flaky; retry before giving up
                last_error = e
                page.wait_for_timeout(2000)

        if not reached_favorites:
            browser.close()
            raise SelectorMismatch(
                f"Couldn't reach a working Favorites tab after 3 attempts (last error: {last_error}). "
                "This page is inconsistent run to run - try again, and if it keeps failing, open the "
                "browser pane yourself and check what's actually rendering."
            ) from last_error

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
