"""Closed local CLI handoff for the OpenAI Ads account-key lifecycle.

Two commands only: a bounded local `status` that contacts no provider, and a
`rotate` that prompts for a replacement key without echo and re-verifies the
same ad account before the stored value is replaced.  There is no `revoke`
command, because OpenAI Ads documents no programmatic key revocation: the
result of every command repeats the provider-side revocation guidance and
states explicitly that this product cannot verify a revocation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import typer
from pydantic import Field

from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import StrictModel
from adhelm.openai_account_key_lifecycle import (
    OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE,
    OpenAIAccountKeyRotation,
    OpenAIAccountKeyStatus,
    openai_account_key_status,
    rotate_openai_account_key,
)

app = typer.Typer(no_args_is_help=True)

STATUS_COMMAND = "auth openai-key status"
ROTATE_COMMAND = "auth openai-key rotate"
_PSEUDONYM_PATTERN = r"^openai_ads_account_v1\.[a-f0-9]{64}$"
_ACCOUNT_REF_PATTERN = r"^openai_ads\.account\.[A-Za-z0-9_-]{1,120}$"


class _StatusOutput(StrictModel):
    """The only locally safe status projection; it contacts no provider."""

    profile_id: Annotated[str, Field(min_length=1, max_length=160)]
    purpose: Literal["READ", "EDIT", "ACTIVATE", "PUBLISH"]
    credential_source: Literal["OS_STORE", "EXPLICIT_CI"]
    local_state: Literal["READY", "MISSING", "UNVERIFIED", "CI_MANAGED"]
    key_slot_present: bool
    account_ref: Annotated[str, Field(pattern=_ACCOUNT_REF_PATTERN)]
    # The masked identity: a salted local label for the ad account the
    # *currently stored* key was last shown to resolve to, and `None` when no
    # retained record binds to that key.  It is a display alias, not a
    # confidentiality measure: the salt sits beside it in the same local state
    # slot, so anyone who can read that slot can recompute the label for a
    # candidate account identifier.  It is not the account ID.
    identity_pseudonym: Annotated[str, Field(pattern=_PSEUDONYM_PATTERN)] | None
    last_verified_at: datetime | None
    last_verified_api_version: Annotated[str, Field(min_length=1, max_length=40)] | None
    revocation_guidance: Annotated[str, Field(min_length=1, max_length=600)]
    provider: Literal["openai_ads"] = "openai_ads"
    provider_scope_status: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    provider_revocation_verified: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False


class _RotationOutput(StrictModel):
    """The only locally safe rotation projection."""

    profile_id: Annotated[str, Field(min_length=1, max_length=160)]
    purpose: Literal["READ", "EDIT", "ACTIVATE", "PUBLISH"]
    operation: Literal["ROTATE"]
    account_ref: Annotated[str, Field(pattern=_ACCOUNT_REF_PATTERN)]
    identity_pseudonym: Annotated[str, Field(pattern=_PSEUDONYM_PATTERN)]
    verified_at: datetime
    verified_api_version: Annotated[str, Field(min_length=1, max_length=40)]
    revocation_guidance: Annotated[str, Field(min_length=1, max_length=600)]
    # The new key answered for the configured ad account before the previous
    # stored value was replaced.
    identity_reverified_before_replacement: Literal[True] = True
    # Rotating locally does not retire the previous key at OpenAI.
    previous_key_revoked_at_provider: Literal[False] = False
    provider: Literal["openai_ads"] = "openai_ads"
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    local_state: Literal["READY"] = "READY"
    provider_revocation_verified: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False


def _far_future() -> datetime:
    return datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


def _status_probe() -> dict[str, Any]:
    return _StatusOutput(
        profile_id="p" * 160,
        purpose="PUBLISH",
        credential_source="OS_STORE",
        local_state="UNVERIFIED",
        key_slot_present=True,
        account_ref="openai_ads.account." + "a" * 120,
        identity_pseudonym="openai_ads_account_v1." + "f" * 64,
        last_verified_at=_far_future(),
        last_verified_api_version="v" * 40,
        revocation_guidance=OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE,
    ).model_dump(mode="json")


def _rotation_probe() -> dict[str, Any]:
    return _RotationOutput(
        profile_id="p" * 160,
        purpose="PUBLISH",
        operation="ROTATE",
        account_ref="openai_ads.account." + "a" * 120,
        identity_pseudonym="openai_ads_account_v1." + "f" * 64,
        verified_at=_far_future(),
        verified_api_version="v" * 40,
        revocation_guidance=OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE,
    ).model_dump(mode="json")


def _status_data(value: object) -> dict[str, Any]:
    if type(value) is not OpenAIAccountKeyStatus:
        raise _result_invalid()
    try:
        return _StatusOutput(
            profile_id=value.profile_id,
            purpose=value.purpose,
            credential_source=value.credential_source,
            local_state=value.local_state,
            key_slot_present=value.key_slot_present,
            account_ref=value.account_ref,
            identity_pseudonym=value.identity_pseudonym,
            last_verified_at=value.last_verified_at,
            last_verified_api_version=value.last_verified_api_version,
            revocation_guidance=value.revocation_guidance,
        ).model_dump(mode="json")
    except Exception:
        raise _result_invalid() from None


def _rotation_data(value: object) -> dict[str, Any]:
    if type(value) is not OpenAIAccountKeyRotation:
        raise _result_invalid()
    try:
        return _RotationOutput(
            profile_id=value.profile_id,
            purpose=value.purpose,
            operation=value.operation,
            account_ref=value.account_ref,
            identity_pseudonym=value.identity_pseudonym,
            verified_at=value.verified_at,
            verified_api_version=value.verified_api_version,
            revocation_guidance=value.revocation_guidance,
        ).model_dump(mode="json")
    except Exception:
        raise _result_invalid() from None


def _result_invalid() -> AdhelmError:
    return AdhelmError(
        "OPENAI_ACCOUNT_KEY_RESULT_INVALID",
        "The OpenAI Ads account-key local result is invalid.",
        exit_code=int(ExitCode.PRECONDITION),
        next_action="Inspect `auth openai-key status` before a deliberate follow-up.",
    )


def _require_result_fit(command: str, maximum_bytes: int, data: dict[str, Any]) -> None:
    from adhelm.protected_token_cli import fits

    if type(maximum_bytes) is not int or not fits(command, data, maximum_bytes):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "The complete OpenAI Ads account-key result exceeds the saved output bound.",
            exit_code=int(ExitCode.PRECONDITION),
            next_action="Increase saved max_output_bytes before a new OpenAI key action.",
        )


@app.command(
    "status",
    help=(
        "Read bounded local OpenAI Ads account-key state; this contacts no provider "
        "and reveals no key material."
    ),
)
def status(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Return slot presence, account reference, masked identity and last verification."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        _require_result_fit(STATUS_COMMAND, maximum, _status_probe())
        emit(
            STATUS_COMMAND,
            _status_data(openai_account_key_status(directory, profile_id)),
            maximum_bytes=maximum,
        )

    guarded(STATUS_COMMAND, work)


@app.command(
    "rotate",
    help=(
        "Replace the stored OpenAI Ads account key after a hidden local prompt; the "
        "replacement must re-verify the same ad account before the old value is replaced."
    ),
)
def rotate(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
    interactive: Annotated[bool, typer.Option("--interactive")] = False,
) -> None:
    """Rotate one OpenAI Ads account key; this reads only the fixed account identity."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        _require_result_fit(ROTATE_COMMAND, maximum, _rotation_probe())
        result = rotate_openai_account_key(directory, profile_id, interactive=interactive)
        emit(ROTATE_COMMAND, _rotation_data(result), maximum_bytes=maximum)

    guarded(ROTATE_COMMAND, work)


def register_commands(auth_app: typer.Typer) -> None:
    """Attach the OpenAI Ads account-key lifecycle under the existing ``auth`` group."""
    auth_app.add_typer(app, name="openai-key")


__all__ = ["ROTATE_COMMAND", "STATUS_COMMAND", "app", "register_commands"]
