"""app/prefetch.py: free GitHub and page lookups before any model call."""
from types import SimpleNamespace

import httpx

from app import prefetch


def test_repo_slugs_and_page_urls():
    text = ("check github.com/limine-bootloader/limine and https://github.com/redox-os/redox.git "
            "plus github.com/features/copilot and https://example.com/post. https://vm.tiktok.com/x")
    assert prefetch.repo_slugs(text) == ["limine-bootloader/limine", "redox-os/redox"]
    assert prefetch.page_urls(text) == ["https://example.com/post"]


def test_gather_and_context():
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
