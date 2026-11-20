"""Print the record of signatures for whoever runs the bot: `log [PERIOD] [--certificate ID] [--number NUMBER]`."""

import argparse
from datetime import UTC, datetime, timedelta

from .adapters.audit import DEFAULT_PERIOD, AuditLog, period_hours
from .paths import data_dir


def main() -> None:
    parser = argparse.ArgumentParser(prog="sealwharf log", description="Print the record of signatures, oldest first.")
    parser.add_argument("period", nargs="?", default=DEFAULT_PERIOD, help="how far back to look, such as 12h or 30d (default: 7d)")
    parser.add_argument("--certificate", help="only signatures made with this certificate id")
    parser.add_argument("--number", help="only signatures this number asked for or approved")
    arguments = parser.parse_args()
    hours = period_hours(arguments.period)
    if hours is None:
        parser.error("the period is a number followed by h or d, such as 12h or 30d")
    entries = [entry for entry in AuditLog(data_dir() / "audit.jsonl").entries(datetime.now(UTC) - timedelta(hours=hours))
               if arguments.certificate in (None, entry.get("certificate_id"))
               and (arguments.number is None or arguments.number in (entry.get("requester"), entry.get("confirmed_by")))]
    for entry in entries:
        print(entry["time"], entry.get("event"), f"certificate={entry.get('certificate_id')}", f"requester={entry.get('requester')}",
              f"approval={entry.get('approval')}", f"confirmed_by={entry.get('confirmed_by') or '-'}",
              f"sha256={(entry.get('signed_sha256') or '')[:8]}", f"file={entry.get('filename') or '-'}")
    print(f"{len(entries)} signature(s) in the last {arguments.period}.")


if __name__ == "__main__":
    main()
