from __future__ import annotations

from .local_server import start_local_server


def main() -> int:
    """Start the local safe trading runtime service."""
    server = start_local_server()
    status = server.health()
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
