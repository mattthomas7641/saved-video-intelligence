"""app/prefetch.py: free GitHub and page lookups before any model call."""
from types import SimpleNamespace

import httpx
import pytest

from app import prefetch


def test_repo_slugs_and_page_urls():
    text = ("check github.com/limine-bootloader/limine and https://github.com/redox-os/redox.git "
            "plus github.com/features/copilot and https://example.com/post. https://vm.tiktok.com/x")
    assert prefetch.repo_slugs(text) == ["limine-bootloader/limine", "redox-os/redox"]
    assert prefetch.page_urls(text) == ["https://example.com/post"]


def test_gather_and_context(monkeypatch):
    monkeypatch.setattr(prefetch, "is_public_url", lambda url: True)
    video = SimpleNamespace(tiktok_url="https://www.tiktok.com/@a/video/1", caption="github.com/a/b",
                            transcript="read https://blog.example.com/x", ocr_text="", user_note=None)
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html><script>x</script><p>Big news</p></html>")))
    found = prefetch.gather(video, http=http, lookup=lambda slug: {"full_name": slug, "stars": 5})
    assert found["repos"][0]["full_name"] == "a/b" and found["pages"][0]["text"] == "Big news"
    ctx = prefetch.as_context(found)
    assert "GitHub a/b" in ctx and "Big news" in ctx


def test_empty_context_when_nothing_found():
    assert prefetch.as_context({"repos": [], "pages": []}) == ""


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8787/settings", "http://localhost/x", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd", "ftp://example.com/", "http://0.0.0.0/",
])
def test_private_and_non_http_urls_are_refused(url):
    assert prefetch.is_public_url(url) is False
    http = httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail("fetched " + str(req.url))))
    assert prefetch.page_text(url, http).startswith("(not fetched")


def test_redirect_to_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(prefetch, "is_public_url", lambda url: "127.0.0.1" not in url)
    def handler(req):
        if req.url.host == "example.org":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8787/settings"})
        pytest.fail("followed redirect to " + str(req.url))
    out = prefetch.page_text("https://example.org/a", httpx.Client(transport=httpx.MockTransport(handler)))
    assert out.startswith("(not fetched")
