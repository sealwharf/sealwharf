"""Container commands: run the bot, check the configuration, or report health."""

import sys

COMMANDS = ("run", "check", "healthcheck", "token-driver", "token-check", "token-retry")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    if command == "run":
        from .session import main as run
    elif command == "check":
        from .check import main as run
    elif command == "healthcheck":
        from .healthcheck import main as run
    elif command == "token-driver":
        from .token_tools import driver_command as run
    elif command == "token-check":
        from .token_tools import check_command as run
    elif command == "token-retry":
        from .session import allow_token_retry as run
    else:
        sys.exit("Usage: sealwharf [" + "|".join(COMMANDS) + "]")
    run()


if __name__ == "__main__":
    main()
