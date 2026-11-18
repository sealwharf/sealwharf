"""Keep library diagnostics from disclosing sensitive document data."""

import logging


def suppress_document_library_logs() -> None:
    for name in ("pypdf", "pyhanko", "pyhanko_certvalidator"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.propagate = False
