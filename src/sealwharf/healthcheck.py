"""Exit successfully only while the webhook answers and the worker is alive.

Uses only the standard library so the check starts quickly every time.
"""

import sys
import urllib.error
import urllib.request


def healthy(url: str = "http://127.0.0.1:8000/ready", *, opener=urllib.request.urlopen) -> bool:
    try:
        with opener(url, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main() -> None:
    sys.exit(0 if healthy() else 1)


if __name__ == "__main__":
    main()
