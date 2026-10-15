"""Request hardening for a dashboard with no login.

The app only listens on 127.0.0.1, but your browser can still be pointed at it
by any website you visit:
- a cross-site <form method=post action="http://127.0.0.1:8787/..."> would
  submit (CSRF): spend on research, change settings, rotate channels;
- DNS rebinding would make an attacker's hostname resolve to 127.0.0.1, so the
  page becomes "same origin" and can read /settings.

`GuardMiddleware` closes both: the Host header must be one we serve (rebinding
arrives with the attacker's hostname), and state-changing requests from another
site are refused (browsers send Origin / Sec-Fetch-Site on form posts and
fetches; requests without either, like curl or the test client, aren't from a
browser page and are allowed). It also sets headers that stop the browser from
loading report images from third parties (a prompt-injection exfil channel) or
framing the app.
"""
import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.config import PUBLIC_BASE_URL

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = "; ".join([
    "default-src 'self'",
    "img-src 'self' data:",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "script-src 'self' 'unsafe-inline'",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
])


def allowed_hosts() -> set[str]:
    """Hostnames (no port) this app answers to: loopback, the PUBLIC_BASE_URL host
    (e.g. a Tailscale name), and anything in ALLOWED_HOSTS (comma-separated)."""
    hosts = {"127.0.0.1", "localhost", "[::1]"}
    public = urlsplit(PUBLIC_BASE_URL).hostname
    if public:
        hosts.add(public.lower())
    hosts.update(h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip())
    return hosts


def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8787
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


class GuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        hosts = allowed_hosts()
        if _hostname(request.headers.get("host", "")) not in hosts:
            return PlainTextResponse("Unknown host.", status_code=421)
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and origin != "null" and (urlsplit(origin).hostname or "").lower() not in hosts:
                return PlainTextResponse("Cross-site request refused.", status_code=403)
            if origin == "null" or request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                return PlainTextResponse("Cross-site request refused.", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response
