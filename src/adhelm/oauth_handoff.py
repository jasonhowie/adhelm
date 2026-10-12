"""Deliberate terminal-only OAuth handoff; never part of a JSON result or log."""

import sys
from urllib.parse import parse_qs, urlsplit


def show_authorization_handoff(url: str, *, provider: str) -> None:
    """Called only after the provider validates its constructed authorization URL."""
    if not sys.stderr.isatty():
        return
    redirect = parse_qs(urlsplit(url).query).get("redirect_uri", [""])[0]
    host = urlsplit(redirect).netloc
    sys.stderr.write(
        f"Open this {provider} authorization URL in your local browser if no tab appears:\n"
        f"{url}\nWaiting for the {provider} callback on {host} (up to 5 minutes). "
        "Ctrl-C cancels. Keep this URL in your local terminal; do not paste it into chat.\n"
    )
    sys.stderr.flush()
