"""Check the configuration without starting the bot, and say what to fix.

Every line names a setting or a certificate alias; no secret is ever printed.
A token is asked for its PIN at most once, under the same guard as the bot.
"""

import sys

from .adapters.gateways import open_gateway
from .adapters.local_ai import MODEL
from .adapters.model_runtime import model_status
from .application.signers import unlock_signers
from .paths import config_path, data_dir
from .signer_config import load_config


def check_gateway(config) -> None:
    """Raise ValueError unless the configured gateway accepts its token and is linked to WhatsApp."""
    gateway = open_gateway(config)
    try:
        gateway.check()
    finally:
        gateway.close()


def check(*, channel=check_gateway, model=model_status, output=print) -> bool:
    """Report on each part of the configuration; return True when the bot can start."""
    try:
        config = load_config(config_path())
    except ValueError as exc:
        output(f"PROBLEM  configuration: {exc}")
        return False
    problems = 0
    for key in ("public_url", "gateway_token"):
        if getattr(config, key) is None:
            output(f"PROBLEM  {key} is missing")
            problems += 1
    output(f"ok       {len(config.signers)} certificate(s), {len(config.senders)} number(s)")
    for signer in config.signers:
        try:
            unlocked = unlock_signers((signer,), data_dir=data_dir())[0]
            output(f"ok       certificate {signer.id}: opens and matches its name; valid until {unlocked.expires_at.date()}")
        except ValueError as exc:
            output(f"PROBLEM  {exc}")
            problems += 1
    if config.gateway_token is not None:
        try:
            channel(config)
            output("ok       WhatsApp gateway is connected")
        except ValueError as exc:
            output(f"PROBLEM  {exc}")
            problems += 1
    status = model()
    if status:
        output(f"ok       AI model {MODEL} is ready")
    else:
        output("PROBLEM  the AI model service cannot be reached; start it with: docker compose up -d ollama" if status is None
               else f"PROBLEM  the AI model {MODEL} is not installed; download it with: docker compose up model")
        problems += 1
    output("Ready to start." if not problems else f"{problems} problem(s) to fix in config/sealwharf.yaml.")
    return not problems


def main() -> None:
    sys.exit(0 if check() else 1)
