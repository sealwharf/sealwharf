import httpx
import pytest

from sealwharf.adapters.whapi_webhook import IncompleteSettingsSnapshot, WebhookSettingsError
from sealwharf.adapters.whapi_webhook import configure_verified as configure_webhook


def test_public_health_is_checked_without_a_fixed_wait(monkeypatch):
    from sealwharf.session import wait_for_public_health
    sleeps = []
    calls = []
    monkeypatch.setattr("sealwharf.session.time.sleep", lambda seconds: sleeps.append(seconds))
    def get(url, timeout):
        calls.append((url, timeout))
        assert sleeps == []
        return httpx.Response(200, json={"status": "ok"})
    monkeypatch.setattr("sealwharf.session.httpx.get", get)
    wait_for_public_health("https://synthetic.example", attempts=1)
    assert calls == [("https://synthetic.example/health", 3)]


def test_configure_webhook_preserves_existing_settings():
    calls = []
    stored = {"callback_persist": False, "media": {"auto_download": "document"}, "webhooks": []}
    def handler(request):
        nonlocal stored
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=stored)
        import json
        data = json.loads(request.content)
        assert data["callback_persist"] is False
        assert data["media"] == {"auto_download": "document"}
        assert data["webhooks"][0]["headers"]["X-Sealwharf-Secret"] == "secret"
        assert data["webhooks"][0]["url"] == "https://test.example.com/webhook"
        stored = data
        return httpx.Response(200, json={})
    client = httpx.Client(base_url="https://gate.whapi.cloud", transport=httpx.MockTransport(handler))
    configure_webhook(client, "https://test.example.com", "secret", allow_incomplete_snapshot=True)
    assert [request.method for request in calls] == ["GET", "PATCH", "GET"]


def test_does_not_replace_other_hooks_or_accept_arbitrary_url():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"webhooks": [{"headers": {"other": "secret"}}]})
    client = httpx.Client(base_url="https://gate.whapi.cloud", transport=httpx.MockTransport(handler))
    with pytest.raises(IncompleteSettingsSnapshot):
        configure_webhook(client, "https://test.example.com", "secret")
    with pytest.raises(WebhookSettingsError):
        configure_webhook(client, "http://test.example.com", "secret")
    assert [request.method for request in calls] == ["GET"]
