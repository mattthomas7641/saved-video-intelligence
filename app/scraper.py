"""TikTok bot-account inbox reader, used by the daily sync endpoint.

This automates a **dedicated account created for this purpose**, not your
real TikTok account — per the plan, that's the whole point: if this account
gets rate-limited, flagged, or banned, your real profile, social graph, and
Saved list are untouched. Point `login()` at the bot account, never your own.

This is intentionally NOT an LLM driving a browser fresh each run — it's a
small, directly testable script with a persisted login session, so "does the
saved session survive unattended, across a real day-gap" can be checked by
hand instead of trusted blind.

DOM/network notes (confirmed live against a real bot account with real
shared messages - not guessed):
  - Clicking `[data-e2e="nav-messages"]` opens an INLINE FLYOUT panel, not a
    page navigation (the URL stays on tiktok.com/) - a direct /messages
    navigation is kept as a fallback for a possible alternate layout.
  - That same click also surfaces an unrelated Activity/notifications panel
    that can share overlapping visible text (e.g. a display name) with the
    real conversation list - matching the DM list specifically by
    `[data-e2e="dm-new-conversation-item"]` avoids that false match.
  - The conversation list shows the counterpart's **display nickname**
    ("Matt Thomas"), never their @handle - matching the configured trusted
    handle against the list is a dead end. Instead each conversation is
    opened and the handle is confirmed present in the rendered thread
    itself, which TikTok does include once a thread is open.
  - A shared video card (`[data-e2e="dm-new-shared-video"]`) has NO href,
    id, or any usable attribute in its DOM - it's a CSS background-image
    thumbnail with a client-side-only click handler. The canonical video id
    and author instead show up in the `/api/im/item_detail` network
    response TikTok fires automatically for each shared video as the thread
    renders (no click needed) - listening for that, not scraping the DOM,
    is what actually works here.
  - Headless Chromium with default settings gets served a degraded page
    even with a valid session - the same countermeasures that fixed the
    earlier Favorites-tab work (desktop user-agent, `navigator.webdriver`
    patched out, `--disable-blink-features=AutomationControlled`) apply here
    too.

Known gap: a text note sent alongside a shared video isn't paired with it
yet (`user_note` is always returned as None for now) - getting the right
videos from the right sender reliably was the harder and more important
problem to solve first. `[data-e2e="dm-new-message-text"]` is a confirmed,
real selector for message text if/when this gets implemented.

Setup (once, interactively, on the host, against the BOT account):
    python -m app.scraper login

Then the daily agent calls scrape_new_saves() via POST /api/sync/inbox.
"""
import json
import sys
import time

from app.config import DATA_DIR, get_trusted_sender

_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

AUTH_STATE_PATH = DATA_DIR / "tiktok_auth_state.json"
HOME_URL = "https://www.tiktok.com/"
MESSAGES_URL = "https://www.tiktok.com/messages"

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


_CONVERSATION_ITEM_SELECTOR = '[data-e2e="dm-new-conversation-item"]'


def _collect_from_trusted_sender_conversations(page, handle: str) -> list[dict]:
    """Opens each conversation exactly once, combining sender-verification and
    video extraction in that single visit - opening a conversation a second
    time to "extract" from it doesn't refire the network requests the
    extraction depends on (TikTok doesn't repeat an already-loaded fetch),
    which is a real bug this fixed: an earlier version of this function
    verified the sender first and extracted in a separate second pass, and
    silently came back empty every time as a result.

    The conversation LIST only shows a display nickname (confirmed live:
    "Matt Thomas", not the @handle) - unusable for matching by handle.
    Instead each candidate is opened and the handle is confirmed present in
    the thread itself (TikTok does render the counterpart's real @handle
    inside an opened thread, even though the list only shows their
    nickname). Shared-video cards have no href/id in the DOM at all - the
    canonical video id/author only show up in the `/api/im/item_detail`
    network response TikTok fires automatically for each one as the thread
    renders (no click on the video itself needed)."""
    count = page.locator(_CONVERSATION_ITEM_SELECTOR).count()
    if count == 0:
        raise SelectorMismatch(
            f"No conversations found at all ({_CONVERSATION_ITEM_SELECTOR} matched nothing). "
            "Either nothing's been shared yet, or the inbox markup doesn't match what's expected here - "
            "open the browser pane yourself and check the live inbox DOM."
        )
    needle = handle.lower()
    any_matched = False
    collected: list[dict] = []
    for i in range(count):
        found_here: list[dict] = []

        def on_response(resp, sink=found_here):
            if "/api/im/item_detail" not in resp.url:
                return
            try:
                item = resp.json()["itemInfo"]["itemStruct"]
                sink.append({"id": str(item["id"]), "author": item["author"]["uniqueId"]})
            except Exception:  # noqa: BLE001 - a single malformed response shouldn't lose the rest
                pass

        page.on("response", on_response)
        try:
            page.locator(_CONVERSATION_ITEM_SELECTOR).nth(i).click(timeout=10000)
            page.wait_for_timeout(2500)  # let item_detail requests from opening this thread settle
            if needle in page.content().lower():
                any_matched = True
                collected.extend(found_here)
        except Exception:  # noqa: BLE001 - skip a flaky item, don't abort the whole scan
            continue
        finally:
            page.remove_listener("response", on_response)

    if not any_matched:
        raise SelectorMismatch(
            f"Found {count} conversation(s) but none mention '@{handle}' once opened. Either nothing's "
            "been shared from that account yet, or TikTok no longer surfaces the handle inside an "
            "opened thread the way it did when this was written - inspect the live DOM."
        )
    return collected


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
        collected: list[dict] = []
        for _attempt in range(3):
            try:
                _open_inbox(page)
                collected = _collect_from_trusted_sender_conversations(page, handle)
                break
            except NotLoggedIn:
                browser.close()
                raise
            except NoTrustedSender:
                browser.close()
                raise
            except Exception as e:  # noqa: BLE001 - genuinely flaky; retry before giving up
                last_error = e
                collected = []
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

    seen_ids: set[str] = set()
    results = []
    for item in collected[:max_messages]:
        if item["id"] in seen_ids:
            continue
        seen_ids.add(item["id"])
        url = f"https://www.tiktok.com/@{item['author']}/video/{item['id']}"
        # Note-pairing isn't implemented yet - getting the right videos from the right
        # sender reliably mattered more to get working first. See module docstring.
        results.append({"tiktok_url": url, "saved_date": None, "user_note": None})
    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        print(json.dumps(scrape_new_saves(), indent=2))
