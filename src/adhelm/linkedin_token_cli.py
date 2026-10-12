"""Closed local CLI handoff for the LinkedIn Ads OAuth token lifecycle."""

from __future__ import annotations

import typer

from adhelm.linkedin_token_credentials import LINKEDIN_TOKEN_SPEC, remove_local_linkedin_token_slots
from adhelm.linkedin_token_workflow import (
    enroll_linkedin_token,
    linkedin_token_local_status,
    local_redirect_uri,
    refresh_linkedin_token,
)
from adhelm.protected_token_cli import TokenCliBinding, build_token_app

# Checked against LinkedIn's published member help page on 2026-09-08. Local
# removal is deliberately described separately: it is never remote revocation.
LINKEDIN_REVOCATION_GUIDANCE: tuple[str, ...] = (
    "Provider-side: LinkedIn Settings & Privacy -> Data privacy -> Other applications -> "
    "Permitted Services, then remove the developer application you enrolled.",
    "Provider-side reference: "
    "https://www.linkedin.com/help/linkedin/answer/a519947/third-party-applications-data-use",
    "Local-only: `adhelm auth remove-local --profile <id>` deletes the stored access-token "
    "entry for this profile.",
    "Local-only: `adhelm auth linkedin-token remove-local --profile <id>` deletes the stored "
    "client secret, refresh token and lifecycle state.",
    "Local removal never revokes the grant at LinkedIn and is never evidence that it was "
    "revoked; removing a shared application's grant can also interrupt other uses of it.",
)

app = build_token_app(
    TokenCliBinding(
        spec=LINKEDIN_TOKEN_SPEC,
        revocation_guidance=LINKEDIN_REVOCATION_GUIDANCE,
        enroll_help=(
            "Authorize one configured LinkedIn Ads profile through the local browser; "
            "no LinkedIn advertising resource is read or changed."
        ),
        remove_local_help=(
            "Delete the protected local LinkedIn client secret, refresh token and state; "
            "provider revocation remains unverified."
        ),
        enroll=enroll_linkedin_token,
        refresh=refresh_linkedin_token,
        local_status=linkedin_token_local_status,
        remove_local=remove_local_linkedin_token_slots,
    )
)


def register_commands(auth_app: typer.Typer) -> None:
    """Attach the LinkedIn token lifecycle under the existing ``auth`` group."""
    auth_app.add_typer(app, name=LINKEDIN_TOKEN_SPEC.command_group)


__all__ = [
    "LINKEDIN_REVOCATION_GUIDANCE",
    "app",
    "local_redirect_uri",
    "register_commands",
]
