"""Check the configuration without starting the bot, and say what to fix.

Every line names a setting or a certificate alias; no secret is ever printed.
A token is asked for its PIN at most once, under the same guard as the bot.
"""

import sys

from .adapters.whapi import check_channel
from .application.signers import unlock_signers
from .paths import config_path, data_dir
from .signer_config import load_config


def check(*, channel=check_channel, output=print) -> bool:
    """Report on each part of the configuration; return True when the bot can start."""
    try:
        config = load_config(config_path())
    except ValueError as exc:
        output(f"PROBLEM  configuration: {exc}")
        return False
    problems = 0
    for key in ("public_url", "whapi_token", "tunnel_token"):
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
    if config.whapi_token is not None:
        try:
            channel(config.whapi_token, url=config.whapi_url)
            output("ok       Whapi channel is connected")
        except ValueError as exc:
            output(f"PROBLEM  {exc}")
            problems += 1
    output("Ready to start." if not problems else f"{problems} problem(s) to fix in config/sealwharf.yaml.")
    return not problems


def main() -> None:
    sys.exit(0 if check() else 1)
