"""The gateway adapters the bot can use, by the name written in `gateway_type`.

To support another gateway, write its adapter as a subclass of `Gateway`, add
it here and to `GATEWAY_TYPES` in the configuration loader, and cover with a
test the case where the sender's number is unknown.
"""

from .gateway import Gateway
from .waha import Waha
from .whapi import Whapi

GATEWAYS: dict[str, type[Gateway]] = {"whapi": Whapi, "waha": Waha}


def open_gateway(config, *, transport=None, addresses=None, pace: float = 0.0) -> Gateway:
    """Open the gateway named in the configuration; raise ValueError when a needed setting is missing."""
    return GATEWAYS[config.gateway_type](config.gateway_token, transport, addresses, pace, config.gateway_url,
                                         config.gateway_session)
