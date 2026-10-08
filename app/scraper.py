"""TikTok bot-account inbox reader, used by the daily sync endpoint.

This automates a **dedicated account created for this purpose**, not your
real TikTok account — per the plan, that's the whole point: if this account
gets rate-limited, flagged, or banned, your real profile, social graph, and
Saved list are untouched. Point `login()` at the bot account, never your own.

This is intentionally NOT an LLM driving a browser fresh each run — it's a
small, directly testable script with a persisted login session, so "does the
saved session survive unattended, across a real day-gap" can be checked by
hand instead of trusted blind.

DOM notes - what's confirmed vs. still a best guess:
  CONFIRMED (seen live, logged in, during the earlier Saved-page work):
  - `[data-e2e="nav-messages"]` exists in the top nav when logged in.
  - Headless Chromium with default settings gets served a degraded page
    (missing features, generic fallback content) even with a valid session -
    the same basic countermeasures that fixed the Favorites-tab work
    (desktop user-agent, `navigator.webdriver` patched out,
    `--disable-blink-features=AutomationControlled`) are carried over here.
  NOT YET CONFIRMED - written as a reasonable first attempt, expect to debug
  this live once the bot account exists and has real test messages (the
  Favorites-tab code went through exactly this cycle: a first guess, three
  real bugs found by testing against a real account, then it worked):
  - Whether clicking nav-messages opens an inline panel or navigates to a
    dedicated page (a direct `/messages` navigation is tried as a fallback).
  - The conversation-list and message-thread markup, and whether a text note
    sent alongside a shared video is a caption on the same message or a
    separate one. Video *links* are found the same robust, selector-light
    way the Favorites page used (any `a[href*='/video/']`), which doesn't
    depend on guessing bubble markup. Note *association* is best-effort on
    top of that and fails soft per-video (returns no note, not an error) if
    the structure doesn't match what's assumed here - the core job (finding
    shared videos from the trusted sender) still works even if note-pairing
    doesn't.

Setup (once, interactively, on the host, against the BOT account):
    python -m app.scraper login

Then the daily agent calls scrape_new_saves() via POST /api/sync/inbox.
"""
import json
import re
import sys
import time

from app.config import DATA_DIR, get_trusted_sender

_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

AUTH_STATE_PATH = DATA_DIR / "tiktok_auth_state.json"
HOME_URL = "https://www.tiktok.com/"
MESSAGES_URL = "https://www.tiktok.com/messages"

_VIDEO_LINK_SELECTOR = "a[href*='/video/']"
_SESSION_COOKIES = {"sessionid", "sid_tt", "sid_guard"}

# How far back to look each poll. Bounded so a growing conversation doesn't
# make every future poll slower - rely on polling often enough that nothing
# of interest falls outside this window between runs.
_LOOKBACK_HOURS = 48


class NotLoggedIn(Exception):
    """No saved session yet, or it expired — run `python -m app.scraper login`
    against the bot account."""


class SelectorMismatch(Exception):
    """TikTok's page structure didn't match what this script expects. See the
    module docstring's "not yet confirmed" section for what to check first."""


class NoTrustedSender(Exception):
    """No trusted-sender handle configured in Settings - the inbox reader
    refuses to guess who to trust, rather than silently acting on messages
    from anyone who happens to DM the bot account."""


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
    """Open a real, visible browser so you can log into the BOT account by
    hand - not your own account. Detects a successful login by watching for
    TikTok's own session cookies (not one tab's URL — login can finish in a
    popup or a second tab). Saves the session and closes the browser itself
    once it sees you're logged in. Run this on the host (not inside a
    headless container); it needs a real display."""
    sync_playwright = _require_playwright()
    print("Opening a browser window. Log into the DEDICATED BOT ACCOUNT (not your own TikTok account), "
          "then confirm its inbox loads. I'll detect it and close the window myself — "
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


def _open_inbox(page):
    """Navigate to the message inbox. Tries the nav icon first (confirmed to
    exist), falls back to a direct URL if that doesn't land on a messages
    view - two different plausible TikTok UX shapes, so both are covered
    rather than guessing one."""
    page.goto(HOME_URL, wait_until="networkidle", timeout=30000)
    try:
        page.locator('[data-e2e="nav-messages"]').first.click(timeout=15000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001 - try the direct URL instead
        pass

    if "login" in page.url:
        raise NotLoggedIn("Saved session expired or was rejected. Re-run: python -m app.scraper login")

    if "messages" not in page.url:
        page.goto(MESSAGES_URL, wait_until="networkidle", timeout=30000)
        if "login" in page.url:
            raise NotLoggedIn("Saved session expired or was rejected. Re-run: python -m app.scraper login")


def _open_trusted_sender_thread(page, handle: str):
    """Find and open the conversation with the trusted sender. Matches by
    visible text in the conversation list, not a guessed data-e2e attribute,
    since that's the part most likely to need live-DOM correction."""
    needle = handle.lower()
    try:
        page.get_by_text(re.compile(re.escape(handle), re.IGNORECASE)).first.click(timeout=15000)
    except Exception as e:
        raise SelectorMismatch(
            f"Couldn't find a conversation with '@{handle}' in the inbox. Either nothing's been shared "
            "yet, or the conversation-list markup doesn't match what get_by_text expects here - open "
            "the browser pane yourself and check the live inbox DOM."
        ) from e
    page.wait_for_timeout(1500)
    if needle not in page.content().lower():
        raise SelectorMismatch(
            f"Clicked a conversation but '@{handle}' doesn't appear in the opened thread - may have "
            "opened the wrong conversation. Inspect the live DOM."
        )


def _extract_videos_with_notes(page) -> list[dict]:
    """Best-effort: pair each shared video link with nearby text as a note.
    Video-link extraction is the robust part (same pattern that worked for
    the Favorites page); note-pairing is a first attempt and fails soft per
    item rather than raising, since getting video links right matters far
    more than getting notes right."""
    data = page.evaluate("""() => {
        const links = [...document.querySelectorAll("a[href*='/video/']")];
        return links.map(link => {
            const bubble = link.closest('[class*="message"], [class*="Message"], li, div[role="listitem"]') || link.parentElement;
            let note = null;
            if (bubble) {
                const prev = bubble.previousElementSibling;
                const next = bubble.nextElementSibling;
                for (const sib of [next, prev]) {
                    if (sib && !sib.querySelector("a[href*='/video/']")) {
                        const text = sib.innerText?.trim();
                        if (text && text.length < 500) { note = text; break; }
                    }
                }
            }
            return { href: link.href, note };
        });
    }""")
    return data or []


def scrape_new_saves(max_messages: int = 100) -> list[dict]:
    """Returns videos shared by the trusted sender, as
    [{"tiktok_url": str, "saved_date": None, "user_note": str|None}, ...].
    Safe to call repeatedly with everything currently visible - the caller's
    dedup-by-URL logic drops anything already known."""
    handle = get_trusted_sender()
    if not handle:
        raise NoTrustedSender("No trusted TikTok handle set in Settings. The inbox reader won't act on "
                               "messages from an unconfigured sender.")
    if not AUTH_STATE_PATH.exists():
        raise NotLoggedIn("No saved TikTok login session. Run: python -m app.scraper login (against the bot account)")

    sync_playwright = _require_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            storage_state=str(AUTH_STATE_PATH), viewport={"width": 1280, "height": 900},
            user_agent=_DESKTOP_UA, locale="en-US",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        last_error: Exception | None = None
        extracted: list[dict] = []
        for _attempt in range(3):
            try:
                _open_inbox(page)
                _open_trusted_sender_thread(page, handle)
                extracted = _extract_videos_with_notes(page)
                break
            except NotLoggedIn:
                browser.close()
                raise
            except NoTrustedSender:
                browser.close()
                raise
            except Exception as e:  # noqa: BLE001 - genuinely flaky; retry before giving up
                last_error = e
                page.wait_for_timeout(2000)
        else:
            browser.close()
            raise SelectorMismatch(
                f"Couldn't read the inbox after 3 attempts (last error: {last_error}). "
                "Open the browser pane yourself and check what's actually rendering."
            ) from last_error

        try:
            context.storage_state(path=str(AUTH_STATE_PATH))  # refresh rotated cookies
        except Exception:  # noqa: BLE001 - non-fatal
            pass
        browser.close()

    video_id_re = re.compile(r"/video/(\d{8,})")
    seen_ids: set[str] = set()
    results = []
    for item in extracted[:max_messages]:
        m = video_id_re.search(item.get("href", ""))
        if not m or m.group(1) in seen_ids:
            continue
        seen_ids.add(m.group(1))
        user = re.search(r"tiktok\.com/(@[\w.\-]+)/video/", item["href"])
        url = f"https://www.tiktok.com/{user.group(1) if user else '@_'}/video/{m.group(1)}"
        results.append({"tiktok_url": url, "saved_date": None, "user_note": item.get("note")})
    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        print(json.dumps(scrape_new_saves(), indent=2))
