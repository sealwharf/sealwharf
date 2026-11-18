import json
from copy import deepcopy

import httpx
import pytest

from sealwharf.adapters.whapi_webhook import (
    IncompleteSettingsSnapshot,
    WebhookSettingsError,
    WebhookUpdateUncertain,
    configure_verified,
    prepare_webhook,
)

URL = "https://test.example.com"
SECRET = "synthetic-secret"


def settings():
    return {"pdo_sync": True, "callback_backoff_delay_ms": 3000,
            "max_callback_backoff_delay_ms": 900000, "callback_persist": False,
            "proxy_trust_filters": False, "offline_mode": False, "full_history": False,
            "auto_read_messages": False, "media": {"auto_download": "document"},
            "webhooks": [{"url": "https://third-party.example/hook", "mode": "body",
                          "events": [{"type": "statuses", "method": "post"}],
                          "headers": {"X-Other": "other-secret"}}]}


def test_verified_update_preserves_other_hook_and_settings():
    original = settings()
    stored = deepcopy(original)
    calls = []
    def handler(request):
        nonlocal stored
        calls.append(request.method)
        if request.method == "PATCH":
            stored = json.loads(request.content)
        return httpx.Response(200, json=stored)
    client = httpx.Client(base_url="https://gate.whapi.cloud", transport=httpx.MockTransport(handler))
    configure_verified(client, URL, SECRET)
    assert calls == ["GET", "PATCH", "GET"]
    assert stored["webhooks"][0] == original["webhooks"][0]
    assert stored["media"] == original["media"]
    assert stored["webhooks"][1]["headers"]["X-Sealwharf-Secret"] == SECRET
    assert original == settings()


def test_incomplete_snapshot_rejected_unless_explicitly_allowed():
    snapshot = settings()
    del snapshot["pdo_sync"]
    with pytest.raises(IncompleteSettingsSnapshot):
        prepare_webhook(snapshot, URL, SECRET)
    plan = prepare_webhook(snapshot, URL, SECRET, allow_incomplete_snapshot=True)
    assert plan.missing_fields == {"pdo_sync"}
    assert SECRET not in repr(plan)


def test_owned_webhook_keeps_extra_events_and_headers():
    snapshot = settings()
    snapshot["webhooks"].append({"url": "https://old.example.com/webhook", "mode": "body",
        "events": [{"type": "statuses", "method": "post"}],
        "headers": {"x-sealwharf-secret": SECRET, "X-Extra": "value"}})
    plan = prepare_webhook(snapshot, URL, SECRET)
    own = plan.payload["webhooks"][1]
    assert own["headers"]["X-Extra"] == "value"
    assert {"type": "statuses", "method": "post"} in own["events"]
    assert {"type": "messages", "method": "post"} in own["events"]


def test_rejects_redacted_headers_and_multiple_owned_hooks():
    snapshot = settings()
    snapshot["webhooks"][0]["headers"]["X-Other"] = "********"
    with pytest.raises(WebhookSettingsError):
        prepare_webhook(snapshot, URL, SECRET)
    snapshot = settings()
    snapshot["webhooks"] = [{"headers": {"X-Sealwharf-Secret": SECRET}},
                            {"headers": {"X-Sealwharf-Secret": SECRET}}]
    with pytest.raises(WebhookSettingsError):
        prepare_webhook(snapshot, URL, SECRET)


def test_detects_unrelated_setting_change_after_patch():
    snapshot = settings()
    calls = []
    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(200, json=snapshot)
        if request.method == "PATCH":
            changed = json.loads(request.content)
            changed["offline_mode"] = True
            handler.after = changed
            return httpx.Response(200, json={})
        return httpx.Response(200, json=handler.after)
    client = httpx.Client(base_url="https://gate.whapi.cloud", transport=httpx.MockTransport(handler))
    with pytest.raises(WebhookUpdateUncertain):
        configure_verified(client, URL, SECRET)
    assert calls == ["GET", "PATCH", "GET"]


def test_timeout_does_not_retry_patch():
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=settings())
        raise httpx.ReadTimeout("synthetic", request=request)
    client = httpx.Client(base_url="https://gate.whapi.cloud", transport=httpx.MockTransport(handler))
    with pytest.raises(WebhookUpdateUncertain) as exc:
        configure_verified(client, URL, SECRET)
    assert SECRET not in str(exc.value)
    assert calls == ["GET", "PATCH"]


def test_default_back_off_delays_may_be_absent_from_the_snapshot():
    snapshot = settings()
    del snapshot["callback_backoff_delay_ms"], snapshot["max_callback_backoff_delay_ms"]
    plan = prepare_webhook(snapshot, URL, SECRET)
    assert plan.missing_fields == frozenset() and "callback_backoff_delay_ms" not in plan.payload
