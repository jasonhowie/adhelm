"""Closed local CLI handoff for the Reddit Ads OAuth token lifecycle."""

from __future__ import annotations

import typer

from adhelm.protected_token_cli import TokenCliBinding, build_token_app
from adhelm.reddit_token_credentials import REDDIT_TOKEN_SPEC, remove_local_reddit_token_slots
from adhelm.reddit_token_workflow import (
    enroll_reddit_token,
    reddit_token_local_status,
    refresh_reddit_token,
)

# Local removal is deliberately described separately: it is never remote
# revocation.  The provider-side step is guidance this build has not checked
# against Reddit's current account pages; it says so, and
# docs/research/reddit-oauth-contract.md lists it as UNVERIFIED.
REDDIT_REVOCATION_GUIDANCE: tuple[str, ...] = (
    "Provider-side (not yet checked against Reddit's current pages; see "
    "docs/research/reddit-oauth-contract.md): while signed in to Reddit as the enrolling "
    "user, look for this application in your Reddit account's authorized-applications "
    "settings and revoke its access, and delete any app registration you created for it "
    "that you no longer need.",
    "Local-only: `adhelm auth remove-local --profile <id>` deletes the stored access-token "
    "entry for this profile.",
    "Local-only: `adhelm auth reddit-token remove-local --profile <id>` deletes the stored "
    "application secret, refresh token and lifecycle state.",
    "Local removal never revokes the grant at Reddit and is never evidence that it was "
    "revoked; revoking a shared application's grant can also interrupt other uses of it.",
)

app = build_token_app(
    TokenCliBinding(
        spec=REDDIT_TOKEN_SPEC,
        revocation_guidance=REDDIT_REVOCATION_GUIDANCE,
        enroll_help=(
            "Authorize one configured Reddit Ads profile through the local browser; "
            "it reads only the fixed ad account identity and changes no Reddit resource."
        ),
        remove_local_help=(
            "Delete the protected local Reddit application secret, refresh token and state; "
            "provider revocation remains unverified."
        ),
        enroll=enroll_reddit_token,
        refresh=refresh_reddit_token,
        local_status=reddit_token_local_status,
        remove_local=remove_local_reddit_token_slots,
    )
)


def register_commands(auth_app: typer.Typer) -> None:
    """Attach the Reddit token lifecycle under the existing ``auth`` group."""
    auth_app.add_typer(app, name=REDDIT_TOKEN_SPEC.command_group)


__all__ = [
    "REDDIT_REVOCATION_GUIDANCE",
    "app",
    "register_commands",
]
