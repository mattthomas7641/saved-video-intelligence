"""app/scraper.py: only the parts testable without a real browser/TikTok
session - the guard clauses that must fail before ever touching Playwright,
so they're covered independent of browser availability in CI."""
import pytest

from app import scraper
from app.config import save_trusted_sender


def test_refuses_to_run_without_trusted_sender():
    with pytest.raises(scraper.NoTrustedSender):
        scraper.scrape_new_saves()


def test_refuses_to_run_without_login_session(tmp_path, monkeypatch):
    save_trusted_sender("someone")
    monkeypatch.setattr(scraper, "AUTH_STATE_PATH", tmp_path / "does_not_exist.json")
    with pytest.raises(scraper.NotLoggedIn):
        scraper.scrape_new_saves()
