"""Closed local CLI handoff for the TikTok Ads app auth-code token lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import typer
from pydantic import Field

from adhelm import __version__
from adhelm.canonical import canonical_bytes
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import ResultEnvelope, StrictModel
from adhelm.providers.tiktok_ads import TIKTOK_ADS_API_VERSION
from adhelm.tiktok_token_credentials import (
    TikTokTokenLifecycleResult,
    TikTokTokenStatus,
    remove_local_tiktok_token_slots,
)
from adhelm.tiktok_token_workflow import (
    enroll_tiktok_token,
    refresh_tiktok_token,
    tiktok_token_local_status,
)

app = typer.Typer(no_args_is_help=True)

_DIGEST_PATTERN = r"^[a-f0-9]{64}$"
_PSEUDONYM_PATTERN = r"^tiktok_ads_advertiser_v1\.[a-f0-9]{64}$"
# One TikTok application can be granted many advertisers.  The complete count is
# always reported; the inline list is bounded so one status row stays a constant
# size and always fits the smallest saved output bound.  Nothing is hidden: the
# omitted count says exactly how many are not named.
_MAX_INLINE_ADVERTISERS = 16

# Checked against TikTok's own documentation on 2026-10-11 (the "Authorization FAQs" and
# "Revoke a long-term access token" pages of the Business API portal; see
# docs/setup/tiktok_ads.md for the fetch dates).  The advertiser's route is the TikTok
# For Business authorization settings page.  TikTok also documents a revoke endpoint,
# which AdHelm never sends: it does not print the access token and makes no revocation
# request.  Local removal is deliberately described separately: it is never remote
# revocation.
TIKTOK_REVOCATION_GUIDANCE: tuple[str, ...] = (
    "Provider-side: the advertiser removes the developer's authorization on TikTok For "
    "Business at ads.tiktok.com/ac/page/authorizations (TikTok's Authorization FAQs). "
    "AdHelm cannot check that this was done.",
    "TikTok also documents a token revocation endpoint, `/oauth2/revoke_token/`, which takes "
    "the app ID, app secret and the access token. AdHelm does not send it and sends no "
    "revocation request of any kind.",
    "Local-only: `adhelm auth remove-local --profile <id>` deletes the stored access-token entry "
    "for this profile.",
    "Local-only: `adhelm auth tiktok-token remove-local --profile <id>` deletes the stored app "
    "secret, granted advertiser list and lifecycle state.",
    "Local removal never revokes the grant at TikTok and is never evidence that it was "
    "revoked; removing a shared application's grant can also interrupt other uses of it.",
)


class _LifecycleOutput(StrictModel):
    """The only locally safe enrollment result projection."""

    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    purpose: Literal["READ", "EDIT"]
    operation: Literal["ENROLL"]
    environment: Literal["PRODUCTION", "SANDBOX"]
    api_version: Annotated[str, Field(min_length=1, max_length=20)]
    # `None` means TikTok declared no lifetime for this token.  That is missing
    # information, never "does not expire"; `expiry_source` says which.
    access_token_expires_at: datetime | None
    expiry_source: Literal["PROVIDER_DECLARED", "NOT_DOCUMENTED_LOCALLY"]
    advertiser_list_status: Literal["RETURNED", "NOT_RETURNED"]
    advertiser_pseudonyms: Annotated[list[str], Field(max_length=_MAX_INLINE_ADVERTISERS)]
    advertiser_pseudonym_count: Annotated[int, Field(ge=0)]
    advertiser_pseudonyms_omitted: Annotated[int, Field(ge=0)]
    bound_advertiser_pseudonym: Annotated[str, Field(pattern=_PSEUDONYM_PATTERN)]
    advertiser_binding: Literal[
        "PROVEN_IN_EXCHANGE_RESPONSE", "NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"
    ]
    granted_permissions: Literal["NOT_DOCUMENTED_LOCALLY"] = "NOT_DOCUMENTED_LOCALLY"
    app_approval_verified: Literal[False] = False
    refresh_route: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    local_state: Literal["READY"] = "READY"
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False


class _StatusOutput(StrictModel):
    """The only locally safe status projection; it contacts no provider."""

    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    purpose: Literal["READ", "EDIT"]
    local_state: Literal["READY", "MISSING", "UNVERIFIED"]
    environment: Literal["PRODUCTION", "SANDBOX"] | None
    api_version: Annotated[str, Field(min_length=1, max_length=20)] | None
    access_token_expires_at: datetime | None
    # `None` means no expiry was ever declared, so expiry is unknown rather than
    # false.  A boolean here would report an undocumented lifetime as healthy.
    access_token_expired: bool | None
    expiry_source: Literal["PROVIDER_DECLARED", "NOT_DOCUMENTED_LOCALLY"] | None
    advertiser_list_status: Literal["RETURNED", "NOT_RETURNED"] | None
    advertiser_pseudonyms: Annotated[list[str], Field(max_length=_MAX_INLINE_ADVERTISERS)]
    advertiser_pseudonym_count: Annotated[int, Field(ge=0)]
    advertiser_pseudonyms_omitted: Annotated[int, Field(ge=0)]
    bound_advertiser_pseudonym: Annotated[str, Field(pattern=_PSEUDONYM_PATTERN)] | None
    advertiser_binding: (
        Literal["PROVEN_IN_EXCHANGE_RESPONSE", "NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"] | None
    )
    access_token_slot_present: bool
    app_secret_slot_present: bool
    advertiser_slot_present: bool
    revocation_guidance: Annotated[list[str], Field(min_length=1, max_length=8)]
    granted_permissions: Literal["NOT_DOCUMENTED_LOCALLY"] = "NOT_DOCUMENTED_LOCALLY"
    app_approval_verified: Literal[False] = False
    refresh_route: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    provider_authentication_verified: Literal[False] = False
    remote_revocation_verified: Literal[False] = False
    execution_authority: Literal[False] = False


class _LocalRemoveOutput(StrictModel):
    """Local removal only; it makes no claim about provider revocation."""

    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    removed_slot_count: Annotated[int, Field(ge=0, le=3)]
    revocation_guidance: Annotated[list[str], Field(min_length=1, max_length=8)]
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    operation: Literal["LOCAL_REMOVE_ONLY"] = "LOCAL_REMOVE_ONLY"
    local_only: Literal[True] = True
    remote_revocation_verified: Literal[False] = False


def _fits(command: str, data: dict[str, Any], maximum_bytes: int) -> bool:
    return (
        len(
            canonical_bytes(
                ResultEnvelope(tool_version=__version__, command=command, outcome="OK", data=data)
            )
        )
        + 1
        <= maximum_bytes
    )


def _require_result_fit(command: str, maximum_bytes: int, data: dict[str, Any]) -> None:
    if type(maximum_bytes) is not int or not _fits(command, data, maximum_bytes):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "The complete TikTok token result exceeds the saved output bound.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Increase saved max_output_bytes before a new TikTok token action.",
        )


def _far_future() -> datetime:
    return datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


def _probe_pseudonyms() -> list[str]:
    return [f"tiktok_ads_advertiser_v1.{index:064x}" for index in range(_MAX_INLINE_ADVERTISERS)]


def _inline_pseudonyms(values: tuple[str, ...]) -> tuple[list[str], int, int]:
    """Inline the first pseudonyms by content order and say how many are omitted."""
    ordered = sorted(values)
    inline = ordered[:_MAX_INLINE_ADVERTISERS]
    return inline, len(ordered), len(ordered) - len(inline)


def _lifecycle_probe() -> dict[str, Any]:
    return _LifecycleOutput(
        profile_configuration_digest="f" * 64,
        purpose="EDIT",
        operation="ENROLL",
        environment="PRODUCTION",
        api_version=TIKTOK_ADS_API_VERSION,
        access_token_expires_at=_far_future(),
        expiry_source="NOT_DOCUMENTED_LOCALLY",
        advertiser_list_status="NOT_RETURNED",
        advertiser_pseudonyms=_probe_pseudonyms(),
        advertiser_pseudonym_count=999_999,
        advertiser_pseudonyms_omitted=999_999,
        bound_advertiser_pseudonym="tiktok_ads_advertiser_v1." + "f" * 64,
        advertiser_binding="NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED",
    ).model_dump(mode="json")


def _status_probe() -> dict[str, Any]:
    return _StatusOutput(
        profile_configuration_digest="f" * 64,
        purpose="EDIT",
        local_state="UNVERIFIED",
        environment="PRODUCTION",
        api_version=TIKTOK_ADS_API_VERSION,
        access_token_expires_at=_far_future(),
        access_token_expired=True,
        expiry_source="NOT_DOCUMENTED_LOCALLY",
        advertiser_list_status="NOT_RETURNED",
        advertiser_pseudonyms=_probe_pseudonyms(),
        advertiser_pseudonym_count=999_999,
        advertiser_pseudonyms_omitted=999_999,
        bound_advertiser_pseudonym="tiktok_ads_advertiser_v1." + "f" * 64,
        advertiser_binding="NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED",
        access_token_slot_present=True,
        app_secret_slot_present=True,
        advertiser_slot_present=True,
        revocation_guidance=list(TIKTOK_REVOCATION_GUIDANCE),
    ).model_dump(mode="json")


def _remove_probe() -> dict[str, Any]:
    return _LocalRemoveOutput(
        profile_configuration_digest="f" * 64,
        removed_slot_count=3,
        revocation_guidance=list(TIKTOK_REVOCATION_GUIDANCE),
    ).model_dump(mode="json")


def _lifecycle_data(value: object) -> dict[str, Any]:
    if type(value) is not TikTokTokenLifecycleResult:
        raise _result_invalid()
    inline, total, omitted = _inline_pseudonyms(value.advertiser_pseudonyms)
    try:
        return _LifecycleOutput(
            profile_configuration_digest=value.profile_configuration_digest,
            purpose=value.purpose,
            operation=value.operation,
            environment=value.environment,
            api_version=value.api_version,
            access_token_expires_at=value.access_token_expires_at,
            expiry_source=value.expiry_source,
            advertiser_list_status=value.advertiser_list_status,
            advertiser_pseudonyms=inline,
            advertiser_pseudonym_count=total,
            advertiser_pseudonyms_omitted=omitted,
            bound_advertiser_pseudonym=value.bound_advertiser_pseudonym,
            advertiser_binding=value.advertiser_binding,
        ).model_dump(mode="json")
    except Exception:
        raise _result_invalid() from None


def _status_data(value: object) -> dict[str, Any]:
    if type(value) is not TikTokTokenStatus:
        raise _result_invalid()
    inline, total, omitted = _inline_pseudonyms(value.advertiser_pseudonyms)
    try:
        return _StatusOutput(
            profile_configuration_digest=value.profile_configuration_digest,
            purpose=value.purpose,
            local_state=value.local_state,
            environment=value.environment,
            api_version=value.api_version,
            access_token_expires_at=value.access_token_expires_at,
            access_token_expired=value.access_token_expired,
            expiry_source=value.expiry_source,
            advertiser_list_status=value.advertiser_list_status,
            advertiser_pseudonyms=inline,
            advertiser_pseudonym_count=total,
            advertiser_pseudonyms_omitted=omitted,
            bound_advertiser_pseudonym=value.bound_advertiser_pseudonym,
            advertiser_binding=value.advertiser_binding,
            access_token_slot_present=value.access_token_slot_present,
            app_secret_slot_present=value.app_secret_slot_present,
            advertiser_slot_present=value.advertiser_slot_present,
            revocation_guidance=list(TIKTOK_REVOCATION_GUIDANCE),
        ).model_dump(mode="json")
    except Exception:
        raise _result_invalid() from None


def _result_invalid() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_RESULT_INVALID",
        "The TikTok token local result is invalid.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Inspect `auth tiktok-token status` before a deliberate follow-up.",
    )


@app.command(
    "enroll",
    help=(
        "Exchange one operator-supplied TikTok authorization code for this profile; "
        "no TikTok advertising resource is read or changed."
    ),
)
def enroll(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
    environment: Annotated[str, typer.Option("--environment")],
    interactive: Annotated[bool, typer.Option("--interactive")] = False,
) -> None:
    """Enroll one TikTok Ads profile through hidden app-ID, secret and code prompts."""
    from adhelm.cli import emit, guarded, root

    command = "auth tiktok-token enroll"

    def work() -> None:
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        _require_result_fit(command, maximum, _lifecycle_probe())
        result = enroll_tiktok_token(
            directory,
            profile_id,
            # The environment is a declared fact, never inferred from anything.
            # The value is validated against the closed host map before any
            # prompt or request.
            environment=environment,  # type: ignore[arg-type]
            interactive=interactive,
        )
        emit(command, _lifecycle_data(result), maximum_bytes=maximum)

    guarded(command, work)


@app.command(
    "refresh",
    help=(
        "Refuse with a typed diagnostic: TikTok documents no refresh route for an "
        "advertiser access token."
    ),
)
def refresh(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Always refuse; this contacts no provider and returns no credential."""
    from adhelm.cli import guarded, root

    command = "auth tiktok-token refresh"

    def work() -> None:
        # The route never returns a result, so there is nothing to bound or
        # emit.  Fitting an unrelated probe first would replace the documented
        # refusal with `OUTPUT_LIMIT_EXCEEDED` under a small saved output bound.
        refresh_tiktok_token(root(ctx), profile_id)

    guarded(command, work)


@app.command(
    "status", help="Read bounded local TikTok credential state; this contacts no provider."
)
def status(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Return local expiry, advertiser pseudonyms and revocation guidance only."""
    from adhelm.cli import emit, guarded, root

    command = "auth tiktok-token status"

    def work() -> None:
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        _require_result_fit(command, maximum, _status_probe())
        result = tiktok_token_local_status(directory, profile_id)
        emit(command, _status_data(result), maximum_bytes=maximum)

    guarded(command, work)


@app.command(
    "remove-local",
    help=(
        "Delete the protected local TikTok app secret, advertiser list and state; "
        "this never revokes the grant at TikTok."
    ),
)
def remove_local(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Remove only the TikTok-owned protected slots; this never revokes at TikTok."""
    from adhelm.cli import emit, guarded, root

    command = "auth tiktok-token remove-local"

    def work() -> None:
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        _require_result_fit(command, maximum, _remove_probe())
        profile_digest, removed = remove_local_tiktok_token_slots(directory, profile_id)
        emit(
            command,
            _LocalRemoveOutput(
                profile_configuration_digest=profile_digest,
                removed_slot_count=removed,
                revocation_guidance=list(TIKTOK_REVOCATION_GUIDANCE),
            ).model_dump(mode="json"),
            maximum_bytes=maximum,
        )

    guarded(command, work)


def register_commands(auth_app: typer.Typer) -> None:
    """Attach the TikTok token lifecycle under the existing ``auth`` group."""
    auth_app.add_typer(app, name="tiktok-token")


__all__ = [
    "TIKTOK_REVOCATION_GUIDANCE",
    "app",
    "register_commands",
]
