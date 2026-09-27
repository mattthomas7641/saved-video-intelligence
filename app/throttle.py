"""Shared politeness gate for TikTok downloads: back off when they push back."""
import threading
import time


class Throttle:
    def __init__(self):
        self._lock = threading.Lock()
        self._pause_until = 0.0
        self._backoff = 60.0
        self.consecutive_blocks = 0

    def wait(self, should_stop=lambda: False) -> None:
        while not should_stop():
            remaining = self._pause_until - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))

    def report_block(self) -> None:
        with self._lock:
            self.consecutive_blocks += 1
            self._pause_until = time.time() + self._backoff
            self._backoff = min(self._backoff * 2, 900.0)

    def report_ok(self) -> None:
        with self._lock:
            self.consecutive_blocks = 0
            self._backoff = 60.0

    def paused_for(self) -> int:
        return max(0, int(self._pause_until - time.time()))

    def reset(self) -> None:
        with self._lock:
            self._pause_until = 0.0
            self._backoff = 60.0
            self.consecutive_blocks = 0


throttle = Throttle()

_MARKERS = ("429", "too many requests", "rate limit", "blocked", "captcha", "forbidden", "403",
            "timed out", "connection reset", "temporarily", "try again")


def looks_like_throttle(message: str) -> bool:
    lowered = (message or "").lower()
    return any(m in lowered for m in _MARKERS)
