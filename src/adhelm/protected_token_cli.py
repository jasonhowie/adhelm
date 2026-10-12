"""Provider-neutral local CLI handoff for protected OAuth token lifecycles.

A provider's CLI module supplies a :class:`TokenCliBinding` (its lifecycle spec,
revocation guidance, command help and the four workflow callables) and gets back
the closed ``enroll`` / ``refresh`` / ``status`` / ``remove-local`` command group
and the only locally safe output projections.  Nothing here reads a secret or
contacts a provider.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from pydantic import Field

from adhelm import __version__
from adhelm.canonical import canonical_bytes
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import ResultEnvelope, StrictModel
from adhelm.protected_token_lifecycle import TokenLifecycleResult, TokenProviderSpec, TokenStatus

_DIGEST_PATTERN = r"^[a-f0-9]{64}$"
_PSEUDONYM_PATTERN = r"^[a-z]+(_[a-z]+)*_account_v1\.[a-f0-9]{64}$"
_PROVIDER_PATTERN = r"^[a-z]+(_[a-z]+)*$"
_MAX_SCOPES = 8
_MAX_PRIVATE_SLOTS = 3


class LifecycleOutput(StrictModel):
    """The only locally safe enrollment/refresh result projection."""

    provider: Annotated[str, Field(pattern=_PROVIDER_PATTERN)]
    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    purpose: Literal["READ", "EDIT"]
    operation: Literal["ENROLL", "REFRESH"]
    granted_scopes: Annotated[list[str], Field(min_length=1, max_length=_MAX_SCOPES)]
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None
    refresh_status: Literal["GRANTED", "REFRESH_NOT_GRANTED"]
    identity_pseudonym: Annotated[str, Field(pattern=_PSEUDONYM_PATTERN)]
    # Permissions the provider echoed that this enrollment never requested. They
    # are reported, never recorded as local authority.
    unrequested_granted_scopes: Annotated[list[str], Field(max_length=_MAX_SCOPES)]
    account_identity_verified: Literal[True] = True
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    local_state: Literal["READY"] = "READY"
    app_review_verified: Literal[False] = False
    account_role_verified: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False


class StatusOutput(StrictModel):
    """The only locally safe status projection; it contacts no provider."""

    provider: Annotated[str, Field(pattern=_PROVIDER_PATTERN)]
    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    purpose: Literal["READ", "EDIT"]
    local_state: Literal["READY", "MISSING", "UNVERIFIED", "REAUTHORIZATION_REQUIRED"]
    granted_scopes: Annotated[list[str], Field(max_length=_MAX_SCOPES)]
    access_token_expires_at: datetime | None
    access_token_expired: bool | None
    refresh_token_expires_at: datetime | None
    refresh_status: Literal["GRANTED", "REFRESH_NOT_GRANTED"] | None
    identity_pseudonym: Annotated[str, Field(pattern=_PSEUDONYM_PATTERN)] | None
    bearer_slot_present: bool
    refresh_slot_present: bool
    revocation_guidance: Annotated[list[str], Field(min_length=1, max_length=8)]
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    provider_authentication_verified: Literal[False] = False
    remote_revocation_verified: Literal[False] = False
    app_review_verified: Literal[False] = False
    execution_authority: Literal[False] = False


class LocalRemoveOutput(StrictModel):
    """Local removal only; it makes no claim about provider revocation."""

    provider: Annotated[str, Field(pattern=_PROVIDER_PATTERN)]
    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    removed_slot_count: Annotated[int, Field(ge=0, le=_MAX_PRIVATE_SLOTS)]
    revocation_guidance: Annotated[list[str], Field(min_length=1, max_length=8)]
    operation: Literal["LOCAL_REMOVE_ONLY"] = "LOCAL_REMOVE_ONLY"
    local_only: Literal[True] = True
    remote_revocation_verified: Literal[False] = False


@dataclass(frozen=True, slots=True, eq=False)
class TokenCliBinding:
    """Everything one provider contributes to the shared token command group."""

    spec: TokenProviderSpec
    revocation_guidance: tuple[str, ...]
    enroll_help: str
    remove_local_help: str
    enroll: Callable[..., TokenLifecycleResult]
    refresh: Callable[[Path, str], TokenLifecycleResult]
    local_status: Callable[[Path, str], TokenStatus]
    remove_local: Callable[[Path, str], tuple[str, int]]


def fits(command: str, data: dict[str, Any], maximum_bytes: int) -> bool:
    """Return whether one complete result envelope fits the saved output bound."""
    return (
        len(
            canonical_bytes(
                ResultEnvelope(tool_version=__version__, command=command, outcome="OK", data=data)
            )
        )
        + 1
        <= maximum_bytes
    )


def require_result_fit(
    binding: TokenCliBinding, command: str, maximum_bytes: int, data: dict[str, Any]
) -> None:
    label = binding.spec.label
    if type(maximum_bytes) is not int or not fits(command, data, maximum_bytes):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            f"The complete {label} token result exceeds the saved output bound.",
            exit_code=ExitCode.PRECONDITION,
            next_action=f"Increase saved max_output_bytes before a new {label} token action.",
        )


def _far_future() -> datetime:
    return datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


def _probe_pseudonym(spec: TokenProviderSpec) -> str:
    return spec.pseudonym_prefix + "f" * 64


def _lifecycle_probe(binding: TokenCliBinding) -> dict[str, Any]:
    spec = binding.spec
    scopes = sorted(spec.contract.supported_scopes)
    return LifecycleOutput(
        provider=spec.provider,
        profile_configuration_digest="f" * 64,
        purpose="EDIT",
        operation="ENROLL",
        granted_scopes=scopes,
        access_token_expires_at=_far_future(),
        refresh_token_expires_at=_far_future(),
        refresh_status="REFRESH_NOT_GRANTED",
        identity_pseudonym=_probe_pseudonym(spec),
        unrequested_granted_scopes=scopes,
    ).model_dump(mode="json")


def _status_probe(binding: TokenCliBinding) -> dict[str, Any]:
    spec = binding.spec
    return StatusOutput(
        provider=spec.provider,
        profile_configuration_digest="f" * 64,
        purpose="EDIT",
        local_state="REAUTHORIZATION_REQUIRED",
        granted_scopes=sorted(spec.contract.supported_scopes),
        access_token_expires_at=_far_future(),
        access_token_expired=True,
        refresh_token_expires_at=_far_future(),
        refresh_status="REFRESH_NOT_GRANTED",
        identity_pseudonym=_probe_pseudonym(spec),
        bearer_slot_present=True,
        refresh_slot_present=True,
        revocation_guidance=list(binding.revocation_guidance),
    ).model_dump(mode="json")


def _remove_probe(binding: TokenCliBinding) -> dict[str, Any]:
    return LocalRemoveOutput(
        provider=binding.spec.provider,
        profile_configuration_digest="f" * 64,
        removed_slot_count=len(binding.spec.private_slots),
        revocation_guidance=list(binding.revocation_guidance),
    ).model_dump(mode="json")


def _result_invalid(spec: TokenProviderSpec) -> AdhelmError:
    return AdhelmError(
        f"{spec.code_prefix}_RESULT_INVALID",
        f"The {spec.label} token local result is invalid.",
        exit_code=ExitCode.PRECONDITION,
        next_action=f"Inspect `auth {spec.command_group} status` before a deliberate follow-up.",
    )


def lifecycle_data(spec: TokenProviderSpec, value: object) -> dict[str, Any]:
    """Project one enrollment/refresh outcome, refusing another provider's result."""
    if type(value) is not TokenLifecycleResult or value.provider != spec.provider:
        raise _result_invalid(spec)
    try:
        return LifecycleOutput(
            provider=value.provider,
            profile_configuration_digest=value.profile_configuration_digest,
            purpose=value.purpose,
            operation=value.operation,
            granted_scopes=list(value.granted_scopes),
            access_token_expires_at=value.access_token_expires_at,
            refresh_token_expires_at=value.refresh_token_expires_at,
            refresh_status=value.refresh_status,
            identity_pseudonym=_own_pseudonym(spec, value.identity_pseudonym),
            unrequested_granted_scopes=list(value.unrequested_granted_scopes),
        ).model_dump(mode="json")
    except Exception:
        raise _result_invalid(spec) from None


def status_data(
    spec: TokenProviderSpec, revocation_guidance: tuple[str, ...], value: object
) -> dict[str, Any]:
    """Project one local status, refusing another provider's result."""
    if type(value) is not TokenStatus or value.provider != spec.provider:
        raise _result_invalid(spec)
    try:
        return StatusOutput(
            provider=value.provider,
            profile_configuration_digest=value.profile_configuration_digest,
            purpose=value.purpose,
            local_state=value.local_state,
            granted_scopes=list(value.granted_scopes),
            access_token_expires_at=value.access_token_expires_at,
            access_token_expired=value.access_token_expired,
            refresh_token_expires_at=value.refresh_token_expires_at,
            refresh_status=value.refresh_status,
            identity_pseudonym=(
                None
                if value.identity_pseudonym is None
                else _own_pseudonym(spec, value.identity_pseudonym)
            ),
            bearer_slot_present=value.bearer_slot_present,
            refresh_slot_present=value.refresh_slot_present,
            revocation_guidance=list(revocation_guidance),
        ).model_dump(mode="json")
    except Exception:
        raise _result_invalid(spec) from None


def _own_pseudonym(spec: TokenProviderSpec, pseudonym: str) -> str:
    if not pseudonym.startswith(spec.pseudonym_prefix):
        raise ValueError("pseudonym belongs to another provider")
    return pseudonym


def _checked_scopes(scopes: list[str] | None) -> tuple[str, ...] | None:
    if scopes is None or not scopes:
        return None
    return tuple(scopes)


def build_token_app(binding: TokenCliBinding) -> typer.Typer:
    """Build the closed ``enroll`` / ``refresh`` / ``status`` / ``remove-local`` group."""
    spec = binding.spec
    label = spec.label
    prefix = f"auth {spec.command_group}"
    app = typer.Typer(no_args_is_help=True)

    @app.command("enroll", help=binding.enroll_help)
    def enroll(
        ctx: typer.Context,
        profile_id: Annotated[str, typer.Option("--profile")],
        redirect_port: Annotated[int, typer.Option("--redirect-port")],
        scopes: Annotated[list[str] | None, typer.Option("--scopes")] = None,
        interactive: Annotated[bool, typer.Option("--interactive")] = False,
    ) -> None:
        from adhelm.cli import emit, guarded, root

        command = f"{prefix} enroll"

        def work() -> None:
            directory = root(ctx)
            maximum = load_configuration(directory).output.max_output_bytes
            require_result_fit(binding, command, maximum, _lifecycle_probe(binding))
            result = binding.enroll(
                directory,
                profile_id,
                redirect_port=redirect_port,
                scopes=_checked_scopes(scopes),
                interactive=interactive,
            )
            emit(command, lifecycle_data(spec, result), maximum_bytes=maximum)

        guarded(command, work)

    @app.command(
        "refresh",
        help=(
            f"Exchange the stored {label} refresh token for a new bearer and re-verify "
            "the same ad account identity."
        ),
    )
    def refresh(ctx: typer.Context, profile_id: Annotated[str, typer.Option("--profile")]) -> None:
        from adhelm.cli import emit, guarded, root

        command = f"{prefix} refresh"

        def work() -> None:
            directory = root(ctx)
            maximum = load_configuration(directory).output.max_output_bytes
            require_result_fit(binding, command, maximum, _lifecycle_probe(binding))
            result = binding.refresh(directory, profile_id)
            emit(command, lifecycle_data(spec, result), maximum_bytes=maximum)

        guarded(command, work)

    @app.command(
        "status",
        help=f"Read bounded local {label} credential state; this contacts no provider.",
    )
    def status(ctx: typer.Context, profile_id: Annotated[str, typer.Option("--profile")]) -> None:
        from adhelm.cli import emit, guarded, root

        command = f"{prefix} status"

        def work() -> None:
            directory = root(ctx)
            maximum = load_configuration(directory).output.max_output_bytes
            require_result_fit(binding, command, maximum, _status_probe(binding))
            result = binding.local_status(directory, profile_id)
            emit(
                command,
                status_data(spec, binding.revocation_guidance, result),
                maximum_bytes=maximum,
            )

        guarded(command, work)

    @app.command("remove-local", help=binding.remove_local_help)
    def remove_local(
        ctx: typer.Context, profile_id: Annotated[str, typer.Option("--profile")]
    ) -> None:
        from adhelm.cli import emit, guarded, root

        command = f"{prefix} remove-local"

        def work() -> None:
            directory = root(ctx)
            maximum = load_configuration(directory).output.max_output_bytes
            require_result_fit(binding, command, maximum, _remove_probe(binding))
            profile_digest, removed = binding.remove_local(directory, profile_id)
            if removed > len(spec.private_slots):
                raise _result_invalid(spec)
            emit(
                command,
                LocalRemoveOutput(
                    provider=spec.provider,
                    profile_configuration_digest=profile_digest,
                    removed_slot_count=removed,
                    revocation_guidance=list(binding.revocation_guidance),
                ).model_dump(mode="json"),
                maximum_bytes=maximum,
            )

        guarded(command, work)

    return app
