"""Container entry point: tunnel connector, webhook server and signing worker."""

import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

from .adapters.audit import operational_logger
from .adapters.gateway import GatewayError
from .adapters.gateways import open_gateway
from .adapters.inbox import Inbox
from .adapters.locking import WorkerLock
from .adapters.model_runtime import LocalModelSession
from .application.signers import unlock_signers, warn_expiring
from .application.worker import Runtime, process_one
from .logging_config import suppress_document_library_logs
from .paths import config_path, data_dir, webhook_secret
from .signer_config import load_config

BACKUPS_KEPT = 20
SECRET_SETTINGS = frozenset({"gateway_token", "whapi_token", "tunnel_token", "password"})


def _redacted(value):
    if isinstance(value, dict):
        return {key: "REDACTED" if key in SECRET_SETTINGS else _redacted(item) for key, item in value.items()}
    return [_redacted(item) for item in value] if isinstance(value, list) else value


def backup_configuration(source: Path, data: Path) -> Path:
    """Keep a copy of each distinct configuration, with every secret removed."""
    content = yaml.safe_dump(_redacted(yaml.safe_load(source.read_text(encoding="utf-8"))), sort_keys=False, allow_unicode=True)
    folder = data / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"sealwharf-{hashlib.sha256(content.encode()).hexdigest()[:12]}.yaml"
    if not target.exists():
        target.write_text(content, encoding="utf-8", newline="\n")
        copies = sorted(folder.glob("sealwharf-*.yaml"), key=lambda item: item.stat().st_mtime)
        for old in copies[:-BACKUPS_KEPT]:
            old.unlink(missing_ok=True)
    return target


def wait_for_public_health(url: str, *, attempts: int = 60) -> None:
    """Wait until the tunnel serves this bot at its public address."""
    for _ in range(attempts):
        try:
            response = httpx.get(url + "/health", timeout=3)
            if response.status_code == 200 and response.json() == {"status": "ok"}:
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError("The public address does not reach this bot")


def allow_token_retry() -> None:
    """Permit one more login on tokens that refused their PIN, after the PIN was corrected."""
    removed = [path.unlink() or path.name for path in data_dir().glob("token-pin-rejected-*")]
    print(f"Cleared {len(removed)} token guard(s). Each wrong PIN brings the device closer to locking.")


def _start(command: list[str], **environment: str) -> subprocess.Popen:
    """Start a helper process; secrets travel in its environment, never on its command line."""
    return subprocess.Popen(command, env={**os.environ, **environment}, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop_process(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _stop(*_):
    raise KeyboardInterrupt


def main() -> None:
    suppress_document_library_logs()
    log = operational_logger()
    signal.signal(signal.SIGTERM, _stop)
    data = data_dir()
    allow_defaults = os.environ.get("SEALWHARF_ALLOW_SETTINGS_DEFAULTS", "false").casefold()
    if allow_defaults not in {"true", "false"}:
        sys.exit("SEALWHARF_ALLOW_SETTINGS_DEFAULTS must be true or false.")
    try:
        config = load_config(config_path())
        for key in ("public_url", "gateway_token", "tunnel_token"):
            if getattr(config, key) is None:
                raise ValueError(f"{key} is missing from the configuration.")
        signers = unlock_signers(config.signers, data_dir=data)
        # A short pause between messages keeps a large batch from looking like a flood to WhatsApp.
        client = open_gateway(config, addresses=Inbox(data / "inbox.db"), pace=1.0)
    except ValueError as exc:
        # These messages carry fixed text, setting names and certificate aliases only.
        sys.exit(f"Could not start: {exc} Fix config/sealwharf.yaml; the check command lists every problem.")
    secret = webhook_secret(create=True)
    backup_configuration(config_path(), data)
    runtime = Runtime(data, config.requests_per_hour, config.confirmation_minutes, config.languages, config.names,
                      config.request_limits)
    server = tunnel = None
    model = LocalModelSession()
    db_path = data / "inbox.db"
    worker_lock = WorkerLock.for_database(db_path)
    stage = "acquiring the worker lock"
    try:
        worker_lock.acquire()
        inbox = Inbox(db_path)
        inbox.recover_interrupted(worker_lock)
        if any(signer.visual_review == "ollama" for signer in config.signers):
            stage = "reaching the local visual model"
            model.ensure()
        stage = "starting the webhook"
        server = _start([sys.executable, "-m", "uvicorn", "sealwharf.app:from_environment", "--factory",
                         "--host", "0.0.0.0", "--port", "8000", "--no-access-log"])
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("The webhook could not start")
            try:
                if httpx.get("http://127.0.0.1:8000/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("The webhook did not become ready")
        stage = "connecting the tunnel"
        tunnel = _start(["cloudflared", "tunnel", "--no-autoupdate", "run"], TUNNEL_TOKEN=config.tunnel_token)
        wait_for_public_health(config.public_url)
        stage = "configuring the gateway webhook"
        client.connect_webhook(config.public_url, secret, allow_incomplete_snapshot=allow_defaults == "true")
        log.info("session started certificates=%d", len(signers))
        stage = "processing documents"
        last_warning = 0.0
        while True:
            if server.poll() is not None or tunnel.poll() is not None:
                raise RuntimeError("The webhook or the tunnel stopped")
            inbox.set_mark("heartbeat")
            if time.time() - last_warning >= 3600:
                warn_expiring(inbox, client, signers, config.languages)
                last_warning = time.time()
            if not process_one(inbox, client, signers, model.review_contextual, model.suggest_general, runtime):
                time.sleep(2)
    except KeyboardInterrupt:
        log.info("session stopped")
    except Exception as exc:
        status = f" (HTTP {exc.response.status_code})" if isinstance(exc, httpx.HTTPStatusError) else ""
        log.info("session failed while %s%s", stage, status)
        if isinstance(exc, GatewayError):
            log.info("%s", exc)
        raise SystemExit(1) from None
    finally:
        client.close()
        try:
            _stop_process(tunnel)
            _stop_process(server)
        finally:
            worker_lock.release()


if __name__ == "__main__":
    main()
