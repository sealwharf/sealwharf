"""The limits each installation chooses under `limits` in the configuration, with their defaults and ranges.

The bot reads them from here at the moment it needs them, so the values set at
startup apply everywhere. Limits that only protect against malicious files
(how deep a package may unpack, how many parts or pixels it may hold) stay in
the code: they are not a matter of taste.
"""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Limits:
    requests_per_hour: int = 20      # documents accepted from each number in an hour
    confirmation_minutes: int = 5    # how long a question stays open for an answer
    log_days: int = 90               # how long the record of signatures is kept
    waiting_per_owner: int = 20      # documents that may wait in line for one owner's approval
    batch_seconds: int = 5           # pause after a document, so the ones sent with it are answered together
    max_file_mb: int = 20            # largest document accepted, in megabytes
    max_pages: int = 200             # most pages a document may have
    conversion_seconds: int = 60     # longest a Word or LibreOffice conversion may take
    model_seconds: int = 300         # longest the AI model may take to answer
    expiry_warning_days: int = 30    # how early an owner hears that a certificate is about to expire
    log_lines: int = 30              # most signatures listed by bot log

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


RANGES = {
    "requests_per_hour": (1, 10000), "confirmation_minutes": (1, 1440), "log_days": (1, 3650),
    "waiting_per_owner": (1, 1000), "batch_seconds": (0, 120), "max_file_mb": (1, 200), "max_pages": (1, 5000),
    "conversion_seconds": (10, 1800), "model_seconds": (10, 3600), "expiry_warning_days": (1, 365), "log_lines": (1, 500),
}
NAMES = tuple(item.name for item in fields(Limits))

current = Limits()


def parse(raw: object) -> Limits:
    """Read the `limits` section, refusing unknown names and values out of range."""
    if raw is None:
        return Limits()
    if not isinstance(raw, dict) or set(raw) - set(NAMES):
        raise ValueError("limits may contain only " + ", ".join(NAMES) + ".")
    values = {}
    for name, value in raw.items():
        low, high = RANGES[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"limits.{name} must be a whole number from {low} to {high}.")
        values[name] = value
    return Limits(**values)


def use(limits: Limits) -> None:
    """Make these limits the ones the whole bot follows."""
    global current
    current = limits
