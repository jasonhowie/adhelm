"""Agent-discoverable entry points for an explicitly requested local OAuth handoff."""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from pydantic import Field

from adhelm import google_oauth_workflow
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_credentials import GoogleOAuthLocalStatus
from adhelm.google_oauth_workflow import (
    enroll_google_oauth,
    google_oauth_revocation_guidance,
    google_oauth_status,
    reconnect_google_oauth,
)
from adhelm.models import StrictModel
from adhelm.providers.google_ads import GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_NOT_DOCUMENTED

app = typer.Typer(no_args_is_help=True)

_DIGEST_PATTERN = r"^[a-f0-9]{64}$"
_PSEUDONYM_PATTERN = r"^google_oidc_principal_v1\.[a-f0-9]{64}$"
_MAX_SCOPES = 8


class _StatusOutput(StrictModel):
    """The only locally safe status projection; it contacts no provider.

    No access token, refresh token, client secret, or raw Google subject can
    reach this model: the identity is the derived pseudonym, and every other
    field is an expiry, a scope, a state, or a presence flag.
    """

    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    provider: Literal["google_ads", "ga4", "ga4_admin", "gtm"]
    purpose: Literal["READ", "EDIT"]
    local_state: Literal["READY", "REFRESH_STARTED", "RECONNECT_REQUIRED", "MISSING", "UNVERIFIED"]
    identity_pseudonym: Annotated[str, Field(pattern=_PSEUDONYM_PATTERN)] | None
    requested_scopes: Annotated[list[str], Field(max_length=_MAX_SCOPES)]
    granted_scopes: Annotated[list[str], Field(max_length=_MAX_SCOPES)]
    unrequested_granted_scopes: Annotated[list[str], Field(max_length=_MAX_SCOPES)]
    access_token_expires_at: datetime | None
    access_token_expired: bool | None
    refresh_token_expires_at: datetime | None
    refresh_token_expired: bool | None
    # The bundle retains no successful-verification instant, so none is
    # invented.  The bound below is proven: the stored access token was issued
    # by an authorization or a refresh that re-verified this exact Google
    # subject, and it expires at the recorded instant.
    identity_last_verified_at: None = None
    identity_last_verified_not_after: datetime | None
    identity_last_verified_status: Literal["NOT_RETAINED_BOUNDED_BY_ACCESS_TOKEN_EXPIRY"] = (
        "NOT_RETAINED_BOUNDED_BY_ACCESS_TOKEN_EXPIRY"
    )
    identity_reverified_on_refresh: Literal[True] = True
    primary_slot_present: bool
    developer_token_slot_required: bool
    # The typed diagnostic for a credential store that could not answer. It is
    # never `ABSENT` in that case, because an unanswered store and an
    # incomplete enrolment are different facts with different next actions.
    developer_token_slot_state: Literal[
        "PRESENT", "ABSENT", "UNKNOWN_STORE_FAILURE", "NOT_REQUIRED"
    ]
    developer_token_slot_present: bool | None
    developer_token_slot_next_action: Annotated[str, Field(min_length=1, max_length=300)] | None
    developer_token_access_level: Literal["NOT_DOCUMENTED_LOCALLY"] = (
        GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_NOT_DOCUMENTED
    )
    revocation_guidance: Annotated[list[str], Field(min_length=1, max_length=8)]
    credential_source: Literal["OS_OAUTH_GOOGLE"] = "OS_OAUTH_GOOGLE"
    provider_authentication_verified: Literal[False] = False
    remote_revocation_verified: Literal[False] = False
    resource_access_verified: Literal[False] = False
    execution_authority: Literal[False] = False


# Actionable next steps for the two developer-token states that need one. A
# settled `PRESENT`, `ABSENT`, or `NOT_REQUIRED` needs none.
_DEVELOPER_TOKEN_NEXT_ACTION: dict[str, str] = {
    "UNKNOWN_STORE_FAILURE": (
        "The local credential store did not answer for the developer-token reference, so "
        "its presence is unknown and is not reported as absent. Unlock the OS credential "
        "store in a local session and read this status again."
    ),
    "ABSENT": (
        "No developer token is stored for this profile. Store one with "
        "`adhelm auth store` against the profile's secondary credential reference before any "
        "Google Ads read."
    ),
}


def _status_data(value: object) -> dict[str, Any]:
    if type(value) is not GoogleOAuthLocalStatus:
        raise AdhelmError(
            "GOOGLE_OAUTH_STATUS_RESULT_INVALID",
            "The Google OAuth local status result is invalid.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Inspect the local configuration before a deliberate follow-up.",
        )
    return _StatusOutput(
        profile_configuration_digest=value.profile_configuration_digest,
        provider=value.provider,
        purpose=value.purpose,
        local_state=value.local_state,
        identity_pseudonym=value.identity_pseudonym,
        requested_scopes=list(value.requested_scopes),
        granted_scopes=list(value.granted_scopes),
        unrequested_granted_scopes=list(value.unrequested_granted_scopes),
        access_token_expires_at=value.access_token_expires_at,
        access_token_expired=value.access_token_expired,
        refresh_token_expires_at=value.refresh_token_expires_at,
        refresh_token_expired=value.refresh_token_expired,
        identity_last_verified_not_after=value.identity_last_verified_not_after,
        primary_slot_present=value.primary_slot_present,
        developer_token_slot_required=value.developer_token_slot_required,
        developer_token_slot_state=value.developer_token_slot_state,
        developer_token_slot_present=value.developer_token_slot_present,
        developer_token_slot_next_action=_DEVELOPER_TOKEN_NEXT_ACTION.get(
            value.developer_token_slot_state
        ),
        revocation_guidance=list(google_oauth_revocation_guidance(value.provider)),
    ).model_dump(mode="json")


@app.command(
    "enroll",
    help="Enroll one fixed Google OAuth profile; no Google resource read or mutation occurs.",
)
def enroll(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    client_file: Annotated[Path, typer.Option("--client-config")],
    interactive: Annotated[bool, typer.Option("--interactive")] = False,
) -> None:
    """Enroll one fixed Google OAuth profile through the local system browser."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        google_oauth_workflow._require_interactive(interactive)
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        result = enroll_google_oauth(directory, input_file, client_file, interactive=interactive)
        emit("auth google-oauth enroll", result, maximum_bytes=maximum)

    guarded("auth google-oauth enroll", work)


@app.command(
    "reconnect",
    help=(
        "Reconnect the exact configured Google subject/client/scope; "
        "no Google resource read or mutation occurs."
    ),
)
def reconnect(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
    client_file: Annotated[Path, typer.Option("--client-config")],
    interactive: Annotated[bool, typer.Option("--interactive")] = False,
) -> None:
    """Reconnect the exact configured Google subject/client/scope; no Google resource operation."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        google_oauth_workflow._require_interactive(interactive)
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        result = reconnect_google_oauth(directory, profile_id, client_file, interactive=interactive)
        emit("auth google-oauth reconnect", result, maximum_bytes=maximum)

    guarded("auth google-oauth reconnect", work)


@app.command(
    "status",
    help=(
        "Read bounded local Google OAuth credential state for one profile; "
        "this contacts no Google endpoint and reads no advertising resource."
    ),
)
def status(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Return local scope, expiry, identity pseudonym and revocation guidance only."""
    from adhelm.cli import emit, guarded, root

    command = "auth google-oauth status"

    def work() -> None:
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        result = google_oauth_status(directory, profile_id)
        data = _status_data(result)
        # A store that could not be questioned leaves one declared fact
        # unread, so the envelope says so rather than reporting a clean OK.
        outcome = (
            "INCOMPLETE_COVERAGE"
            if data["developer_token_slot_state"] == "UNKNOWN_STORE_FAILURE"
            else "OK"
        )
        emit(command, data, outcome=outcome, maximum_bytes=maximum)

    guarded(command, work)
