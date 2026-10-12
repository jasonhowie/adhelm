"""Closed local CLI handoff for the fixed Meta token lifecycle and inspection."""

from __future__ import annotations

import hmac
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from pydantic import Field

from adhelm import __version__, credential_setup
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.input_files import validate_json_file
from adhelm.meta_token_credentials import (
    MetaTokenCredentialLifecycleResult,
    MetaTokenCredentialStatus,
    MetaTokenLocalRemovalResult,
    install_meta_token_credentials,
    meta_token_credential_status,
    prepare_meta_token_enrollment,
    prepare_meta_token_rotation,
    remove_meta_token_local,
)
from adhelm.meta_token_inspection import MetaTokenObservation
from adhelm.meta_token_observation_records import (
    META_TOKEN_APP_REVIEW_DISCLOSURE,
    META_TOKEN_REVOCATION_GUIDANCE,
    OBSERVATIONS_BUSY_CODE,
    MetaTokenObservationOutcome,
    load_meta_token_observation,
    meta_token_purpose_allowed_scopes,
    retain_meta_token_observation,
)
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.meta_token_workflow import inspect_configured_meta_token
from adhelm.models import Configuration, MetaAccountTokenProfile, ResultEnvelope, StrictModel

app = typer.Typer(no_args_is_help=True)

_DIGEST_PATTERN = r"^[a-f0-9]{64}$"
_MAX_ADDITIONAL_SCOPES = 99
_MAX_PURPOSE_SCOPES = 2
_RetainedInspection = Literal["MATCHES_INSTALLED_BUNDLE", "NOT_RETAINED", "UNREADABLE"]
_SUBJECT_PROMPT_LABEL = "Meta USER token: "
_INSPECTOR_PROMPT_LABEL = "Meta inspector token: "


class _LifecycleOutput(StrictModel):
    """The only locally safe enrollment/rotation result projection."""

    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    purpose: Literal["READ", "EDIT"]
    operation: Literal["ENROLL", "ROTATE"]
    provider: Literal["meta_ads"] = "meta_ads"
    credential_source: Literal["OS_META_TOKEN"] = "OS_META_TOKEN"
    local_state: Literal["READY"] = "READY"
    provider_authentication_verified: Literal[False] = False
    account_access_verified: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False


class _StatusOutput(StrictModel):
    """The only locally safe status projection.

    The expiry, scope, revocation, and review fields all describe **retained
    local facts**.  `status` makes no provider request: `token_validity` is
    what the last local inspection observed, never a current provider claim,
    and `app_review_status` / `business_verification_status` are
    `NOT_DOCUMENTED_LOCALLY` because no command in this build reads either one.
    """

    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    purpose: Literal["READ", "EDIT"]
    local_state: Literal["READY", "MISSING", "UNVERIFIED"]
    token_validity: Literal["VALID_AT_LAST_INSPECTION", "NOT_INSPECTED_LOCALLY"]
    expires_at: datetime | None
    data_access_expires_at: datetime | None
    # The granted scopes clamped to this purpose's closed allowlist, and the
    # complete count of what the token carried.  A READ profile can therefore
    # never record `ads_management`; a wider grant is counted, never named.
    purpose_allowed_scopes: Annotated[list[str], Field(max_length=_MAX_PURPOSE_SCOPES)]
    purpose_clamped_scopes: Annotated[list[str], Field(max_length=_MAX_PURPOSE_SCOPES)]
    # Null, not zero, when the installed token has never been inspected here.
    # A token nothing has looked at does not carry zero scopes.
    granted_scope_count: Annotated[int, Field(ge=0, le=100)] | None
    retained_inspection: _RetainedInspection
    provider: Literal["meta_ads"] = "meta_ads"
    credential_source: Literal["OS_META_TOKEN"] = "OS_META_TOKEN"
    # The guidance itself, not a code for it: an agent reading this route has
    # to be able to tell the marketer how revocation actually works.
    revocation_guidance: Annotated[str, Field(min_length=1, max_length=400)] = (
        META_TOKEN_REVOCATION_GUIDANCE
    )
    app_review_status: Literal["NOT_DOCUMENTED_LOCALLY"] = META_TOKEN_APP_REVIEW_DISCLOSURE
    business_verification_status: Literal["NOT_DOCUMENTED_LOCALLY"] = (
        META_TOKEN_APP_REVIEW_DISCLOSURE
    )
    provider_authentication_verified: Literal[False] = False
    account_access_verified: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False


class _RemoveOutput(StrictModel):
    """What a local removal did, and the fixed statement that it revoked nothing.

    `bundle_removed` and `retained_inspection_removed` are `false` when there
    was nothing of that kind to delete, so a repeated removal is not reported
    as a deletion.  `local_state` is what `status` reads after the removal.
    """

    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    purpose: Literal["READ", "EDIT"]
    bundle_removed: bool
    retained_inspection_removed: bool
    revocation_guidance: Annotated[str, Field(min_length=1, max_length=400)] = (
        META_TOKEN_REVOCATION_GUIDANCE
    )
    operation: Literal["LOCAL_REMOVE_ONLY"] = "LOCAL_REMOVE_ONLY"
    local_state: Literal["MISSING"] = "MISSING"
    local_only: Literal[True] = True
    provider: Literal["meta_ads"] = "meta_ads"
    credential_source: Literal["OS_META_TOKEN"] = "OS_META_TOKEN"
    remote_revocation_verified: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False


class _InspectionOutput(StrictModel):
    """A redacted statement of only the fixed inspected token properties."""

    profile_configuration_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    declared_purpose: Literal["READ", "EDIT"]
    provider: Literal["meta_ads"] = "meta_ads"
    credential_source: Literal["OS_META_TOKEN"] = "OS_META_TOKEN"
    expected_application: Literal["VERIFIED"] = "VERIFIED"
    subject_type: Literal["USER"] = "USER"
    required_scope: Literal["OBSERVED"] = "OBSERVED"
    additional_granted_scope_count: Annotated[int, Field(ge=0, le=_MAX_ADDITIONAL_SCOPES)]
    expires_at: datetime
    data_access_expires_at: datetime
    account_access_verified: Literal[False] = False
    app_review_verified: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False


def _failure(code: str, message: str, *, exit_code: int = ExitCode.PRECONDITION) -> AdhelmError:
    return AdhelmError(
        code,
        message,
        exit_code=exit_code,
        next_action=(
            "Inspect the installed Meta token command contract before a deliberate follow-up."
        ),
    )


def _require_interactive_terminal(interactive: object) -> None:
    """Reject machine/default paths before a slot, prompt, or store access."""
    if interactive is not True:
        raise AdhelmError(
            "EXPLICIT_TERMINAL_HANDOFF_REQUIRED",
            "Meta token entry requires an explicitly requested local hidden terminal prompt.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Run locally with --interactive; never provide Meta tokens in chat or files."
            ),
        )
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AdhelmError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "Meta token entry requires a local interactive terminal.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Run setup in a local terminal; do not provide a token in chat.",
        )


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
            "The complete Meta token result exceeds the saved output bound.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Increase saved max_output_bytes before a new local Meta token action.",
        )


def _lifecycle_probe() -> dict[str, Any]:
    return _LifecycleOutput(
        profile_configuration_digest="f" * 64,
        purpose="EDIT",
        operation="ROTATE",
    ).model_dump(mode="json")


def _status_probe() -> dict[str, Any]:
    """The largest status result this command can produce.

    Every bounded field is at its maximum here -- the wider `EDIT` allowlist,
    both clamped scopes, a three-digit grant count, the longer validity value,
    and the widest timestamps -- so a configured output bound that cannot hold
    the worst case is refused before any credential-store work.
    """
    return _StatusOutput(
        profile_configuration_digest="f" * 64,
        purpose="EDIT",
        local_state="UNVERIFIED",
        token_validity="VALID_AT_LAST_INSPECTION",
        expires_at=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        data_access_expires_at=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        purpose_allowed_scopes=list(meta_token_purpose_allowed_scopes("EDIT")),
        purpose_clamped_scopes=list(meta_token_purpose_allowed_scopes("EDIT")),
        granted_scope_count=100,
        retained_inspection="MATCHES_INSTALLED_BUNDLE",
    ).model_dump(mode="json")


def _remove_probe() -> dict[str, Any]:
    return _RemoveOutput(
        profile_configuration_digest="f" * 64,
        purpose="EDIT",
        bundle_removed=True,
        retained_inspection_removed=True,
    ).model_dump(mode="json")


def _inspection_probe() -> dict[str, Any]:
    return _InspectionOutput(
        profile_configuration_digest="f" * 64,
        declared_purpose="EDIT",
        additional_granted_scope_count=_MAX_ADDITIONAL_SCOPES,
        expires_at=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        data_access_expires_at=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
    ).model_dump(mode="json")


def _lifecycle_data(value: object) -> dict[str, Any]:
    if type(value) is not MetaTokenCredentialLifecycleResult:
        raise _failure("META_TOKEN_RESULT_INVALID", "The Meta token local result is invalid.")
    try:
        return _LifecycleOutput(
            profile_configuration_digest=value.profile_configuration_digest,
            purpose=value.purpose,
            operation=value.operation,
            provider=value.provider,
            credential_source=value.credential_source,
            local_state=value.local_state,
            provider_authentication_verified=value.provider_authentication_verified,
            account_access_verified=value.account_access_verified,
            execution_authority=value.execution_authority,
        ).model_dump(mode="json")
    except Exception:
        raise _failure(
            "META_TOKEN_RESULT_INVALID", "The Meta token local result is invalid."
        ) from None


_RETAINED_INSPECTION_STATE: dict[str, _RetainedInspection] = {
    "RETAINED": "MATCHES_INSTALLED_BUNDLE",
    "NOT_RETAINED": "NOT_RETAINED",
    "UNREADABLE": "UNREADABLE",
}


def _status_data(value: object, outcome: object) -> dict[str, Any]:
    if type(value) is not MetaTokenCredentialStatus or type(outcome) is not (
        MetaTokenObservationOutcome
    ):
        raise _failure("META_TOKEN_RESULT_INVALID", "The Meta token local status is invalid.")
    record = outcome.record
    if (outcome.state == "RETAINED") != (record is not None) or (
        record is not None and record.purpose != value.purpose
    ):
        raise _failure("META_TOKEN_RESULT_INVALID", "The Meta token local status is invalid.")
    try:
        return _StatusOutput(
            profile_configuration_digest=value.profile_configuration_digest,
            purpose=value.purpose,
            local_state=value.local_state,
            # A retained inspection describes the bundle installed now, or it
            # is not answered at all.  A rotation replaces the bundle, so the
            # newly installed token reads as never inspected rather than
            # inheriting the replaced token's expiry and grant.
            token_validity=(
                "VALID_AT_LAST_INSPECTION" if record is not None else "NOT_INSPECTED_LOCALLY"
            ),
            retained_inspection=_RETAINED_INSPECTION_STATE[outcome.state],
            expires_at=None if record is None else record.expires_at,
            data_access_expires_at=None if record is None else record.data_access_expires_at,
            purpose_allowed_scopes=list(meta_token_purpose_allowed_scopes(value.purpose)),
            purpose_clamped_scopes=[] if record is None else list(record.purpose_clamped_scopes),
            granted_scope_count=None if record is None else record.granted_scope_count,
            provider=value.provider,
            credential_source=value.credential_source,
            provider_authentication_verified=value.provider_authentication_verified,
            account_access_verified=value.account_access_verified,
            execution_authority=value.execution_authority,
        ).model_dump(mode="json")
    except AdhelmError:
        raise
    except Exception:
        raise _failure(
            "META_TOKEN_RESULT_INVALID", "The Meta token local status is invalid."
        ) from None


def _remove_data(value: object) -> dict[str, Any]:
    if type(value) is not MetaTokenLocalRemovalResult:
        raise _failure("META_TOKEN_RESULT_INVALID", "The Meta token local result is invalid.")
    try:
        return _RemoveOutput(
            profile_configuration_digest=value.profile_configuration_digest,
            purpose=value.purpose,
            bundle_removed=value.bundle_removed,
            retained_inspection_removed=value.retained_inspection_removed,
            provider=value.provider,
            credential_source=value.credential_source,
            remote_revocation_verified=value.remote_revocation_verified,
        ).model_dump(mode="json")
    except Exception:
        raise _failure(
            "META_TOKEN_RESULT_INVALID", "The Meta token local result is invalid."
        ) from None


def _selected_profile(configuration: object, profile_id: object) -> MetaAccountTokenProfile:
    if type(configuration) is not Configuration or type(profile_id) is not str:
        raise _failure(
            "META_TOKEN_PROFILE_REQUIRED",
            "Select one configured Meta token profile.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    selected = [profile for profile in configuration.profiles if profile.profile_id == profile_id]
    if len(selected) != 1 or type(selected[0]) is not MetaAccountTokenProfile:
        raise _failure(
            "META_TOKEN_PROFILE_REQUIRED",
            "Select one configured Meta token profile.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return selected[0]


def _checked_time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _failure("META_TOKEN_RESULT_INVALID", "The Meta token inspection result is invalid.")
    return value.astimezone(UTC)


def _inspection_data(
    value: object,
    *,
    profile: MetaAccountTokenProfile,
    expected_configuration_digest: str,
) -> dict[str, Any]:
    if type(value) is not MetaTokenObservation:
        raise _failure("META_TOKEN_RESULT_INVALID", "The Meta token inspection result is invalid.")
    try:
        scopes = value.scopes
        if (
            type(scopes) is not tuple
            or not 1 <= len(scopes) <= 100
            or any(type(scope) is not str for scope in scopes)
            or value.profile_ref != profile.profile_id
            or value.client_application_ref != profile.client_application_ref
            or profile.required_scope not in scopes
        ):
            raise ValueError("observation")
        observed_at = _checked_time(value.observed_at)
        expires_at = _checked_time(value.expires_at)
        data_access_expires_at = _checked_time(value.data_access_expires_at)
        if not observed_at < expires_at or not observed_at < data_access_expires_at:
            raise ValueError("timestamps")
        return _InspectionOutput(
            profile_configuration_digest=expected_configuration_digest,
            declared_purpose=profile.purpose,
            additional_granted_scope_count=len(scopes) - 1,
            expires_at=expires_at,
            data_access_expires_at=data_access_expires_at,
        ).model_dump(mode="json")
    except AdhelmError:
        raise
    except Exception:
        raise _failure(
            "META_TOKEN_RESULT_INVALID", "The Meta token inspection result is invalid."
        ) from None


def _prompt_subject_token() -> str:
    """Prompt only for the subject token through the fixed no-echo boundary."""
    return credential_setup.prompt_secret_from_terminal(prompt="Meta USER token: ")


def _prompt_inspector_token() -> str:
    """Prompt only for the inspector token through the fixed no-echo boundary."""
    return credential_setup.prompt_secret_from_terminal(prompt="Meta inspector token: ")


@app.command(
    "enroll",
    help="Enroll one nonsecret Meta profile through two explicit local hidden token prompts.",
)
def enroll(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    interactive: Annotated[bool, typer.Option("--interactive")] = False,
) -> None:
    """Create one fixed local Meta bundle; this neither reads an account nor mutates Meta."""
    from adhelm.cli import emit, guarded, root

    command = "auth meta-token enroll"

    def work() -> None:
        # Parse the complete nonsecret draft before deciding whether an explicit
        # terminal handoff is available.  The strict schema rejects accidental
        # token fields before any profile/store work.
        draft = validate_json_file(MetaAccountTokenProfileDraft, input_file)
        directory = root(ctx)
        configuration = load_configuration(directory)
        _require_result_fit(command, configuration.output.max_output_bytes, _lifecycle_probe())
        _require_interactive_terminal(interactive)
        slot = prepare_meta_token_enrollment(directory, draft)
        result = install_meta_token_credentials(
            directory,
            slot,
            subject_token_supplier=_prompt_subject_token,
            inspector_token_supplier=_prompt_inspector_token,
        )
        emit(
            command,
            _lifecycle_data(result),
            maximum_bytes=configuration.output.max_output_bytes,
        )

    guarded(command, work)


@app.command(
    "rotate",
    help="Replace one complete Meta token bundle through two explicit local hidden prompts.",
)
def rotate(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
    interactive: Annotated[bool, typer.Option("--interactive")] = False,
) -> None:
    """Replace only the existing exact local bundle; it never refreshes a Meta token."""
    from adhelm.cli import emit, guarded, root

    command = "auth meta-token rotate"

    def work() -> None:
        directory = root(ctx)
        configuration = load_configuration(directory)
        _require_result_fit(command, configuration.output.max_output_bytes, _lifecycle_probe())
        _require_interactive_terminal(interactive)
        slot = prepare_meta_token_rotation(directory, profile_id)
        result = install_meta_token_credentials(
            directory,
            slot,
            subject_token_supplier=_prompt_subject_token,
            inspector_token_supplier=_prompt_inspector_token,
        )
        emit(
            command,
            _lifecycle_data(result),
            maximum_bytes=configuration.output.max_output_bytes,
        )

    guarded(command, work)


@app.command("status", help="Read one bounded local Meta bundle status without prompting.")
def status(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Return local bundle state only; it does not contact Meta."""
    from adhelm.cli import emit, guarded, root

    command = "auth meta-token status"

    def work() -> None:
        directory = root(ctx)
        configuration = load_configuration(directory)
        _require_result_fit(command, configuration.output.max_output_bytes, _status_probe())
        profile = _selected_profile(configuration, profile_id)
        result = meta_token_credential_status(directory, profile_id)
        # Retained inspection facts only, and only when they describe the
        # bundle installed now.  Nothing here contacts Meta, so an expiry this
        # record proves has passed is reported as an authorization failure
        # rather than as a token that might still work.
        outcome = load_meta_token_observation(
            directory, profile, credential_binding_ref=result.credential_binding_ref
        )
        record = outcome.record
        if record is not None and record.is_expired_at(datetime.now(UTC)):
            raise AdhelmError(
                "META_TOKEN_EXPIRED",
                "The last local inspection of this Meta token records an expiry that has passed.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Rotate the local Meta bundle with `adhelm auth meta-token rotate --profile "
                    "<id> --interactive`, then inspect it again. " + META_TOKEN_REVOCATION_GUIDANCE
                ),
            )
        data = _status_data(result, outcome)
        emit(
            command,
            data,
            # A retained inspection file this product cannot parse leaves a
            # declared fact unread, so the envelope says so rather than
            # reporting a clean OK for a status it could not complete.
            outcome="INCOMPLETE_COVERAGE" if outcome.state == "UNREADABLE" else "OK",
            maximum_bytes=configuration.output.max_output_bytes,
        )

    guarded(command, work)


@app.command(
    "inspect", help="Inspect one configured Meta token through the fixed redacted boundary."
)
def inspect(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Verify only expected app, USER type, required scope, and expiry facts."""
    from adhelm.cli import emit, guarded, root

    command = "auth meta-token inspect"

    def work() -> None:
        directory = root(ctx)
        configuration = load_configuration(directory)
        profile = _selected_profile(configuration, profile_id)
        expected_configuration_digest = digest(configuration)
        _require_result_fit(command, configuration.output.max_output_bytes, _inspection_probe())
        observation = inspect_configured_meta_token(
            directory,
            profile_id,
            expected_configuration_digest=expected_configuration_digest,
        )
        # The private workflow binds its credential reads around the fixed HTTP
        # call.  Also reject a configuration change before constructing a public
        # redacted observation from those facts.
        current = load_configuration(directory)
        if not hmac.compare_digest(digest(current), expected_configuration_digest):
            raise AdhelmError(
                "CONFIGURATION_CHANGED",
                "Local configuration changed during Meta token inspection.",
                exit_code=ExitCode.AMBIGUOUS,
                next_action="Inspect local Meta token status before a deliberate follow-up.",
            )
        data = _inspection_data(
            observation,
            profile=profile,
            expected_configuration_digest=digest(profile),
        )
        # Retain only the nonsecret facts a later `status` read needs, with the
        # granted scopes clamped to this profile's purpose allowlist.  No
        # token, inspector token, binding key, principal, or application
        # identifier is written, and nothing here is a grant.
        try:
            retain_meta_token_observation(
                directory,
                profile,
                granted_scopes=tuple(observation.scopes),
                credential_binding_ref=observation.credential_binding_ref,
                observed_at=observation.observed_at,
                expires_at=observation.expires_at,
                data_access_expires_at=observation.data_access_expires_at,
            )
        except AdhelmError as error:
            if error.code != OBSERVATIONS_BUSY_CODE:
                raise
            raise AdhelmError(
                OBSERVATIONS_BUSY_CODE,
                "The Meta token inspection completed but its result was not retained, because "
                "another AdHelm command is updating the retained inspection file.",
                next_action=(
                    f"Run `adhelm auth meta-token inspect --profile {profile_id}` again once "
                    "that command has finished; `status` does not show this inspection until then."
                ),
            ) from None
        emit(command, data, maximum_bytes=configuration.output.max_output_bytes)

    guarded(command, work)


@app.command(
    "remove-local",
    help=(
        "Delete one profile's protected local Meta bundle and its retained inspection; "
        "this never revokes the token at Meta."
    ),
)
def remove_local(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Remove only this profile's local Meta state; it does not contact Meta or revoke."""
    from adhelm.cli import emit, guarded, root

    command = "auth meta-token remove-local"

    def work() -> None:
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        _require_result_fit(command, maximum, _remove_probe())
        result = remove_meta_token_local(directory, profile_id)
        emit(command, _remove_data(result), maximum_bytes=maximum)

    guarded(command, work)
