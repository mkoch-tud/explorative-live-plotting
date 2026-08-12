"""Small stdout logger used by the local server."""

from datetime import datetime


def log(message: str) -> None:
    """Print a timestamped message immediately."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)
