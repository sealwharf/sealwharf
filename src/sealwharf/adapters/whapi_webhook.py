"""Prepare and verify a Whapi webhook update without discarding other hooks.

Whapi documents that omitted settings with defaults may reset on PATCH. This
module sends the complete snapshot returned by GET and refuses an incomplete
snapshot by default. Verification detects observable changes, but the API has
no documented compare-and-swap: concurrent edits or omitted server fields can
still be lost. Never log the plan, URLs, headers or API responses.
"""

import hmac
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx

PUBLIC_URL = re.compile(r"https://[a-z0-9]([a-z0-9.-]{0,250}[a-z0-9])?")
HEADER = "X-Sealwharf-Secret"
# These settings have documented defaults or control callback reliability.
# If GET omits any of them, the previous values cannot be proven from the
# snapshot. An explicit opt-in is necessary to accept that residual risk.
# The two callback back-off delays are not listed: Whapi leaves them out while
# they hold their defaults, so an update cannot move them anywhere else.
SNAPSHOT_FIELDS = frozenset({
    "pdo_sync", "callback_persist", "proxy_trust_filters", "offline_mode", "full_history",
    "auto_read_messages", "media", "webhooks",
})


class WebhookSettingsError(Exception):
    """A webhook update could not safely be prepared."""


class IncompleteSettingsSnapshot(WebhookSettingsError):
    """The provider did not return all settings needed for a safe update."""


class WebhookUpdateUncertain(WebhookSettingsError):
    """The update may have taken effect, but its result could not be verified."""


@dataclass(frozen=True, repr=False)
class WebhookPlan:
    payload: dict[str, Any]
    original: dict[str, Any]
    other_hooks: list[dict[str, Any]]
    missing_fields: frozenset[str]
    secret: str
    url: str


def _secret_from_hook(hook: dict[str, Any]) -> str | None:
    headers = hook.get("headers", {})
    if not isinstance(headers, dict):
        raise WebhookSettingsError("Webhook headers have an unsupported format.")
    if any(not isinstance(name, str) or not isinstance(value, str)
           or value.strip().casefold() in {"[redacted]", "<redacted>", "redacted", "***", "********"}
           for name, value in headers.items()):
        raise WebhookSettingsError("Webhook headers are incomplete or redacted.")
    matches = [value for name, value in headers.items()
               if isinstance(name, str) and name.casefold() == HEADER.casefold()]
    if len(matches) > 1 or (matches and not isinstance(matches[0], str)):
        raise WebhookSettingsError("Webhook ownership cannot be determined safely.")
    return matches[0] if matches else None


def _owned(hook: dict[str, Any], secret: str) -> bool:
    value = _secret_from_hook(hook)
    return value is not None and hmac.compare_digest(value.encode(), secret.encode())


def prepare_webhook(settings: object, url: str, secret: str, *,
                    allow_incomplete_snapshot: bool = False) -> WebhookPlan:
    """Build an update without changing the input or unrelated webhook entries.

    ``allow_incomplete_snapshot`` accepts that omitted default-valued settings
    may be reset. It does not remove post-update verification or make an
    incomplete response safe. The caller decides whether this risk is allowed.
    """
    if not isinstance(url, str) or PUBLIC_URL.fullmatch(url) is None:
        raise WebhookSettingsError("Unexpected public address.")
    if not isinstance(secret, str) or not secret:
        raise WebhookSettingsError("A webhook secret is required.")
    if not isinstance(settings, dict):
        raise WebhookSettingsError("Unexpected channel settings format.")
    missing = SNAPSHOT_FIELDS.difference(settings)
    if missing and not allow_incomplete_snapshot:
        raise IncompleteSettingsSnapshot("Channel settings snapshot is incomplete; a safe update cannot be guaranteed.")
    hooks = settings.get("webhooks")
    if not isinstance(hooks, list) or not all(isinstance(hook, dict) for hook in hooks):
        raise WebhookSettingsError("Unexpected webhook settings format.")
    original = deepcopy(settings)
    payload = deepcopy(settings)
    own = [i for i, hook in enumerate(hooks) if _owned(hook, secret)]
    if len(own) > 1:
        raise WebhookSettingsError("Multiple owned webhooks are ambiguous.")
    other_hooks = [deepcopy(hook) for i, hook in enumerate(hooks) if i not in own]
    if own:
        hook = payload["webhooks"][own[0]]
        events = hook.get("events", [])
        headers = hook.get("headers", {})
        if not isinstance(events, list) or not isinstance(headers, dict):
            raise WebhookSettingsError("Owned webhook settings have an unsupported format.")
        if not any(isinstance(event, dict) and event.get("type") == "messages"
                   and event.get("method") == "post" for event in events):
            events.append({"type": "messages", "method": "post"})
        hook.update({"url": url + "/webhook", "mode": "body", "events": events})
    else:
        hook = {"url": url + "/webhook", "mode": "body",
                "events": [{"type": "messages", "method": "post"}],
                "headers": {HEADER: secret}}
        payload["webhooks"].append(hook)
    return WebhookPlan(payload, original, other_hooks, frozenset(missing), secret, url)


def verify_webhook(plan: WebhookPlan, observed: object) -> None:
    """Check the new webhook and all previously visible unrelated settings."""
    if not isinstance(observed, dict):
        raise WebhookUpdateUncertain("Updated channel settings could not be verified.")
    for key, value in plan.original.items():
        if key != "webhooks" and (key not in observed or observed[key] != value):
            raise WebhookUpdateUncertain("Other channel settings changed during the update.")
    hooks = observed.get("webhooks")
    if not isinstance(hooks, list) or not all(isinstance(hook, dict) for hook in hooks):
        raise WebhookUpdateUncertain("Updated webhook settings could not be verified.")
    try:
        own = [hook for hook in hooks if _owned(hook, plan.secret)]
    except WebhookSettingsError:
        raise WebhookUpdateUncertain("Updated webhook ownership could not be verified.") from None
    if len(own) != 1:
        raise WebhookUpdateUncertain("The updated webhook could not be uniquely verified.")
    hook = own[0]
    events = hook.get("events")
    if (hook.get("url") != plan.url + "/webhook" or hook.get("mode") != "body"
            or not isinstance(events, list) or not any(isinstance(event, dict)
                and event.get("type") == "messages" and event.get("method") == "post" for event in events)):
        raise WebhookUpdateUncertain("The updated webhook configuration does not match.")
    expected = next(item for item in plan.payload["webhooks"] if _owned(item, plan.secret))
    for key, value in expected.items():
        if key == "events":
            if not all(item in events for item in value):
                raise WebhookUpdateUncertain("Owned webhook events changed during the update.")
        elif key == "headers":
            actual_headers = hook.get("headers")
            if not isinstance(actual_headers, dict):
                raise WebhookUpdateUncertain("Owned webhook headers could not be verified.")
            actual = {name.casefold(): item for name, item in actual_headers.items()}
            if not all(actual.get(name.casefold()) == item for name, item in value.items()):
                raise WebhookUpdateUncertain("Owned webhook headers changed during the update.")
        elif hook.get(key) != value:
            raise WebhookUpdateUncertain("Owned webhook settings changed during the update.")
    remaining = [candidate for candidate in hooks if candidate is not hook]
    for previous in plan.other_hooks:
        try:
            remaining.remove(previous)
        except ValueError:
            raise WebhookUpdateUncertain("An unrelated webhook changed during the update.") from None
    if remaining:
        raise WebhookUpdateUncertain("Unexpected webhook changes occurred during the update.")


def configure_verified(client: httpx.Client, url: str, secret: str, *,
                       allow_incomplete_snapshot: bool = False) -> None:
    """Apply and read back an update once; never retry an uncertain PATCH."""
    if not isinstance(url, str) or PUBLIC_URL.fullmatch(url) is None:
        raise WebhookSettingsError("Unexpected public address.")
    response = client.get("/settings")
    response.raise_for_status()
    plan = prepare_webhook(response.json(), url, secret,
                           allow_incomplete_snapshot=allow_incomplete_snapshot)
    try:
        response = client.patch("/settings", json=plan.payload)
        response.raise_for_status()
        response = client.get("/settings")
        response.raise_for_status()
        observed = response.json()
    except (httpx.HTTPError, ValueError):
        raise WebhookUpdateUncertain("The webhook update could not be verified; do not retry blindly.") from None
    verify_webhook(plan, observed)
