"""Gateway webhook: persist minimal metadata before responding."""

import hmac
import json
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .adapters.gateway import Incoming
from .adapters.gateways import open_gateway
from .adapters.inbox import Inbox, accepted_events
from .authorization import parse_senders
from .paths import config_path, data_dir, webhook_secret
from .signer_config import load_config

MAX_WEBHOOK_BYTES = 2 * 1024 * 1024  # some gateways attach their raw message, thumbnails included
HEARTBEAT_SECONDS = 90


def create_app(*, secret: str, sender: str | frozenset[str], db_path: Path,
               read: Callable[[object], list[Incoming]]) -> FastAPI:
    """`read` is the gateway adapter's reader of a webhook call."""
    senders = parse_senders(sender if isinstance(sender, str) else ",".join(sorted(sender)))
    if len(secret) < 32:
        raise ValueError("Configure a secret of at least 32 characters.")
    inbox = Inbox(db_path)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request):
        """Report whether the signing worker is alive, for the container health check, which runs inside the container.

        Asked from anywhere else, such as through the tunnel, it does not exist.
        """
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=404)
        beat = inbox.get_mark("heartbeat")
        if beat is None or time.time() - beat > HEARTBEAT_SECONDS:
            raise HTTPException(status_code=503)
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request):
        supplied = request.headers.get("x-sealwharf-secret", "")
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            # A body nested too deep to read is as unreadable as a broken one.
            raise HTTPException(status_code=400) from None
        # An adapter may ask its gateway who a hidden sender is, so it runs off the event loop.
        inbox.add(accepted_events(await run_in_threadpool(read, payload), senders))
        return {"status": "ok"}

    return app


def from_environment() -> FastAPI:
    config = load_config(config_path())
    return create_app(secret=webhook_secret(), sender=config.senders, db_path=data_dir() / "inbox.db",
                      read=open_gateway(config).incoming)
