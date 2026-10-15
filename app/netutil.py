"""Small shared helpers for classifying failures and keeping secrets out of messages."""
import re
import ssl

import httpx

_NETWORK_WORDS = re.compile(r"ssl|eof occurred|unexpected_eof|connection (reset|aborted|refused|closed)|"
                            r"remote ?protocol|timed? ?out|temporarily unavailable|broken pipe", re.I)


def is_network_error(e: BaseException) -> bool:
    """A dropped or flaky connection (worth retrying), however deeply an SDK wrapped it.
    Some SDKs re-raise without chaining, so the message text is checked too."""
    chain, seen = e, 0
    while chain is not None and seen < 8:
        if isinstance(chain, httpx.TransportError | ssl.SSLError | ConnectionError | TimeoutError):
            return True
        if _NETWORK_WORDS.search(str(chain)):
            return True
        chain, seen = chain.__cause__ or chain.__context__, seen + 1
    return False


def user_message(e: BaseException, doing: str = "researching") -> str:
    """What to show on a card or in a notification: never raw exception text (it can
    carry URLs, internals or keys); the details go to the app log instead."""
    if is_network_error(e):
        return f"The connection dropped while {doing}. Tap Retry."
    return f"Something went wrong while {doing} (details are in the app log). Tap Retry."


def redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "<redacted>")
    return text
