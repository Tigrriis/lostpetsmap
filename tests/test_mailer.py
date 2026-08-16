"""Outbound mail transport.

The User-Agent test is not busywork. api.resend.com is behind Cloudflare, which
rejects urllib's default "Python-urllib/3.x" with a 403 and Cloudflare error
1010 before the request reaches Resend at all. Because send failures are
logged rather than raised — deliberately, so the reset endpoint cannot be used
to probe which addresses exist — the symptom is silence: no email arrives, and
the log blames a key that is perfectly valid.
"""
import json

import pytest

import config
import mailer


@pytest.fixture
def captured(monkeypatch):
    """Intercept the HTTP call and hand back the Request that would have gone."""
    sent = {}

    class FakeResponse:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        sent["request"] = req
        return FakeResponse()

    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test_key")
    return sent


def test_a_user_agent_is_always_sent(app, captured):
    with app.app_context():
        mailer.send_email("someone@example.com", "Subject", "Body")

    headers = {k.lower(): v for k, v in captured["request"].header_items()}
    agent = headers.get("User-agent".lower(), "")
    assert agent, "Cloudflare rejects the request outright without one"
    assert "urllib" not in agent.lower(), \
        "a script-runtime agent is what Cloudflare blocks with error 1010"
    assert agent == mailer.USER_AGENT


def test_the_request_is_shaped_the_way_resend_expects(app, captured):
    with app.app_context():
        mailer.send_email("someone@example.com", "Subject", "Body",
                          reply_to="finder@example.com")

    req = captured["request"]
    headers = {k.lower(): v for k, v in req.header_items()}
    assert req.full_url == mailer.RESEND_ENDPOINT
    assert req.method == "POST"
    assert headers["authorization"] == "Bearer re_test_key"
    assert headers["content-type"] == "application/json"

    body = json.loads(req.data.decode("utf-8"))
    assert body["to"] == ["someone@example.com"]
    assert body["subject"] == "Subject"
    assert body["text"] == "Body"
    assert body["reply_to"] == "finder@example.com"


def test_reply_to_is_omitted_when_not_given(app, captured):
    with app.app_context():
        mailer.send_email("someone@example.com", "Subject", "Body")
    assert "reply_to" not in json.loads(captured["request"].data.decode("utf-8"))


def test_nothing_is_sent_without_a_key(app, monkeypatch):
    """Without a provider the message is logged, never posted."""
    called = []
    monkeypatch.setattr(mailer.urllib.request, "urlopen",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(config, "RESEND_API_KEY", "")

    with app.app_context():
        mailer.send_email("someone@example.com", "Subject", "Body")
    assert called == []


def test_a_transport_failure_is_swallowed(app, monkeypatch):
    """Callers must return the same neutral response either way.

    The reset endpoint would otherwise leak which addresses have accounts.
    """
    import urllib.error

    def boom(*args, **kwargs):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(mailer.urllib.request, "urlopen", boom)
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test_key")

    with app.app_context():
        mailer.send_email("someone@example.com", "Subject", "Body")   # must not raise
