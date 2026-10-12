"""One local OAuth-bound GA4 Admin EDIT inventory observation.

This module prepares a later key-event CREATE plan.  It has no CLI, ledger,
plan, authorization, or mutation surface.  The only provider traffic is the
existing fixed GET inventory reader, with one bearer acquired through the
Google OAuth execution-binding lifecycle.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_admin_edit_workflow import validate_ga4_admin_edit_inventory
from adhelm.google_oauth_execution_binding import (
    Ga4AdminEditOAuthExecutionBinding,
    bearer_for_ga4_admin_edit,
    resolve_ga4_admin_edit_execution_binding,
)
from adhelm.measurement_records import Digest
from adhelm.models import Configuration, Profile, SafeRef, StrictModel, validate_configuration
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
    _inventory_json_mapping,
    revalidate_ga4_admin_edit_key_event_inventory,
)
from adhelm.strict_json import validate_json_value
from adhelm.transport import SafeHttpClient

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_EDIT_SCOPE: Literal["https://www.googleapis.com/auth/analytics.edit"] = (
    "https://www.googleapis.com/auth/analytics.edit"
)
_SOURCE: Literal["OS_OAUTH_GOOGLE"] = "OS_OAUTH_GOOGLE"
_HOST = "analyticsadmin.googleapis.com"
_OBSERVATION_MARKER_CONTEXT = b"adhelm-ga4-admin-edit-execution-observation-v1\x00"
_OBSERVATION_MARKER_PURPOSE = b"observation-content-marker"
_MISSING = object()


class Ga4AdminEditExecutionInventoryRequest(StrictModel):
    """Closed public arguments for one local execution-bound observation."""

    schema_version: Literal[1] = 1
    profile_id: SafeRef
    stream_id: str = Field(pattern=r"^[0-9]{1,20}$")
    expected_configuration_digest: Digest | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("expected_configuration_digest", mode="before")
    @classmethod
    def exact_optional_digest(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("configuration digest must be lowercase SHA-256 hex")
        return value


class _Ga4AdminEditExecutionOAuthBindingFactsSurface(StrictModel):
    """Closed serialized surface for nonsecret lifecycle binding facts."""

    schema_version: Literal[1] = 1
    profile_ref: SafeRef
    profile_configuration_digest: Digest
    provider: Literal["ga4_admin"] = "ga4_admin"
    purpose: Literal["EDIT"] = "EDIT"
    credential_source: Literal["OS_OAUTH_GOOGLE"] = _SOURCE
    property_ref: SafeRef
    property_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    principal_ref: SafeRef
    client_ref: SafeRef
    api_scope: Literal["https://www.googleapis.com/auth/analytics.edit"] = _EDIT_SCOPE
    protected_grant_provenance: Literal["SEALED_LOCAL_OAUTH_BUNDLE_ASSOCIATION"] = (
        "SEALED_LOCAL_OAUTH_BUNDLE_ASSOCIATION"
    )
    provider_create_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"
    execution_authority: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("profile_configuration_digest", mode="before")
    @classmethod
    def exact_digest(cls, value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("profile digest must be lowercase SHA-256 hex")
        return value

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution authority must be false")
        return value

    @model_validator(mode="after")
    def exact_property_binding(self) -> _Ga4AdminEditExecutionOAuthBindingFactsSurface:
        if self.property_ref != f"ga4_admin.property.{self.property_client_ref}":
            raise ValueError("OAuth binding property references must match native IDs")
        return self


class Ga4AdminEditExecutionOAuthBindingFacts(_Ga4AdminEditExecutionOAuthBindingFactsSurface):
    """Nonsecret facts copied from one lifecycle-issued OAuth binding.

    This is a local typed handoff, not a provider permission, authorization,
    or generic OAuth-credential result. Its serializer rejects malformed
    constructed or copied values before they can become a downstream output
    surface.
    """

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_facts(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        try:
            _validate_binding_facts_instance(self)
            output = handler(self)
            if type(output) is not dict or set(output) != _BINDING_FACT_FIELDS:
                raise ValueError("binding facts serialization")
            return output
        except Exception:
            raise ValueError("GA4 Admin EDIT OAuth binding facts are invalid") from None


_BINDING_FACT_FIELDS = frozenset(
    {
        "schema_version",
        "profile_ref",
        "profile_configuration_digest",
        "provider",
        "purpose",
        "credential_source",
        "property_ref",
        "property_client_ref",
        "principal_ref",
        "client_ref",
        "api_scope",
        "protected_grant_provenance",
        "provider_create_permission",
        "execution_authority",
    }
)


def _validate_binding_facts_instance(value: object) -> None:
    if type(value) is not Ga4AdminEditExecutionOAuthBindingFacts:
        raise ValueError("binding facts type")
    raw: dict[str, object] = {}
    for field_name in _BINDING_FACT_FIELDS:
        field_value = getattr(value, field_name, _MISSING)
        if field_value is _MISSING:
            raise ValueError("binding facts shape")
        raw[field_name] = field_value
    validate_json_value(_Ga4AdminEditExecutionOAuthBindingFactsSurface, raw)


class _Ga4AdminEditExecutionInventoryObservationSurface(StrictModel):
    """Closed serialized surface, kept separate from the public guard.

    ``StrictModel.safe_data`` serializes a model while it is validating it.
    The public result therefore cannot use itself as the serializer's
    revalidation target: that would recurse through the same serializer.  This
    private surface has identical fields and validators but no custom
    serializer, so it safely checks the final JSON-shaped output.
    """

    schema_version: Literal[1] = 1
    observation_kind: Literal["GA4_ADMIN_EDIT_EXECUTION_INVENTORY"] = (
        "GA4_ADMIN_EDIT_EXECUTION_INVENTORY"
    )
    configuration_digest: Digest
    oauth_binding: Ga4AdminEditExecutionOAuthBindingFacts
    target: Ga4AdminEditInventoryTarget
    read_started_at: datetime
    read_finished_at: datetime
    inventory: Ga4AdminEditKeyEventInventory
    inventory_digest: Digest
    observation_content_marker: str = Field(pattern=r"^[a-f0-9]{64}$")
    inventory_scope: Literal["FRESH_FIXED_GA4_ADMIN_EDIT_INVENTORY"] = (
        "FRESH_FIXED_GA4_ADMIN_EDIT_INVENTORY"
    )
    fingerprint_key_continuity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"
    )
    inventory_content_integrity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    )
    observation_content_integrity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    )
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"
    granted_scopes: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    provider_create_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"
    current_provider_state: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    reservation_status: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator(
        "configuration_digest", "inventory_digest", "observation_content_marker", mode="before"
    )
    @classmethod
    def exact_digest(cls, value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("observation digests and markers must be lowercase SHA-256 hex")
        return value

    @field_validator("inventory", mode="before")
    @classmethod
    def exact_normalized_inventory(cls, value: object) -> Ga4AdminEditKeyEventInventory:
        try:
            return validate_json_value(
                Ga4AdminEditKeyEventInventory,
                _inventory_json_mapping(value),
            )
        except Exception:
            raise ValueError("GA4 Admin EDIT inventory is invalid") from None

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def exact_utc_time(cls, value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("read timestamps must be timezone-aware datetimes")
        return value.astimezone(UTC)

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution and mutation controls must be false")
        return value

    @model_validator(mode="after")
    def exact_snapshot_binding(self) -> _Ga4AdminEditExecutionInventoryObservationSurface:
        if (
            self.read_finished_at < self.read_started_at
            or self.inventory_digest != digest(self.inventory)
            or self.inventory.target != self.target
            or self.oauth_binding.property_ref != self.target.property_ref
            or self.oauth_binding.property_client_ref != self.target.property_client_ref
            or self.oauth_binding.api_scope != self.target.required_scope
            or self.target.profile_purpose != "EDIT"
            or self.target.required_scope != _EDIT_SCOPE
            or self.oauth_binding.provider_create_permission != self.provider_create_permission
        ):
            raise ValueError("GA4 Admin EDIT execution observation is inconsistent")
        return self


class Ga4AdminEditExecutionInventoryObservation(_Ga4AdminEditExecutionInventoryObservationSurface):
    """One nonsecret, non-authorizing fixed-GET EDIT inventory snapshot."""

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_snapshot(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Reject malformed bypassed local snapshots before serialization."""

        try:
            _validate_observation_instance(self)
            output = handler(self)
            if type(output) is not dict or set(output) != _OBSERVATION_FIELDS:
                raise ValueError("snapshot serialization")
            return output
        except Exception:
            raise ValueError("GA4 Admin EDIT execution observation is invalid") from None


_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "observation_kind",
        "configuration_digest",
        "oauth_binding",
        "target",
        "read_started_at",
        "read_finished_at",
        "inventory",
        "inventory_digest",
        "observation_content_marker",
        "inventory_scope",
        "fingerprint_key_continuity",
        "inventory_content_integrity",
        "observation_content_integrity",
        "consistency",
        "granted_scopes",
        "provider_create_permission",
        "current_provider_state",
        "reservation_status",
        "execution_authority",
        "remote_mutations",
    }
)


def _validate_observation_instance(value: object) -> None:
    """Validate a public instance without invoking its serializer recursively."""

    if type(value) is not Ga4AdminEditExecutionInventoryObservation:
        raise ValueError("snapshot type")
    raw: dict[str, object] = {}
    for field_name in _OBSERVATION_FIELDS:
        field_value = getattr(value, field_name, _MISSING)
        if field_value is _MISSING:
            raise ValueError("snapshot shape")
        raw[field_name] = field_value
    for field_name, expected_type in (
        ("oauth_binding", Ga4AdminEditExecutionOAuthBindingFacts),
        ("target", Ga4AdminEditInventoryTarget),
        ("inventory", Ga4AdminEditKeyEventInventory),
    ):
        if type(raw[field_name]) is not expected_type:
            raise ValueError("snapshot nested type")
    for field_name in ("read_started_at", "read_finished_at"):
        timestamp = raw[field_name]
        if (
            type(timestamp) is not datetime
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise ValueError("snapshot timestamp")
        raw[field_name] = timestamp.astimezone(UTC).isoformat()
    for field_name in ("oauth_binding", "target", "inventory"):
        nested = raw[field_name]
        assert isinstance(nested, StrictModel)
        raw[field_name] = nested.model_dump(mode="json", warnings="error")
    validate_json_value(_Ga4AdminEditExecutionInventoryObservationSurface, raw)


def validate_ga4_admin_edit_execution_observation(
    directory: Path,
    profile_id: str,
    *,
    stream_id: str,
    expected_configuration_digest: str | None = None,
) -> tuple[Configuration, Profile, Ga4AdminEditInventoryTarget, str]:
    """Run the key-free, source-specific preflight for the fixed GET sequence."""

    try:
        if not isinstance(directory, Path):
            raise ValueError("directory")
        request = Ga4AdminEditExecutionInventoryRequest(
            profile_id=profile_id,
            stream_id=stream_id,
            expected_configuration_digest=expected_configuration_digest,
        )
    except Exception:
        raise _invalid_input() from None
    try:
        # The existing pure target validator needs CI enabled only to inspect a
        # possible CI profile. This function rejects every non-OAuth source
        # immediately afterward and never resolves a CI credential.
        configuration, profile, target = validate_ga4_admin_edit_inventory(
            directory,
            str(request.profile_id),
            stream_id=request.stream_id,
            ci_enabled=True,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_input() from None
    if (
        type(profile) is not Profile
        or profile.credential_source != _SOURCE
        or profile.provider != "ga4_admin"
        or profile.purpose != "EDIT"
        or profile.scopes != [_EDIT_SCOPE]
    ):
        raise _oauth_profile_required()
    observed = digest(configuration)
    expected = request.expected_configuration_digest or observed
    if not hmac.compare_digest(observed, expected):
        raise _configuration_drift()
    return configuration, profile, target, expected


def observe_ga4_admin_edit_execution_inventory(
    directory: Path,
    profile_id: str,
    *,
    stream_id: str,
    fingerprint_key: bytes,
    expected_configuration_digest: str | None = None,
) -> Ga4AdminEditExecutionInventoryObservation:
    """Read one full fixed EDIT inventory through one lifecycle-issued bearer.

    The supplied profile is revalidated before the fingerprint key or protected
    credential lifecycle is accessed. Configuration is compared repeatedly but
    never locked through HTTP. The returned snapshot contains no bearer, raw
    OIDC subject, refresh token, private key, or raw provider response.
    """

    configuration, profile, target, expected = validate_ga4_admin_edit_execution_observation(
        directory,
        profile_id,
        stream_id=stream_id,
        expected_configuration_digest=expected_configuration_digest,
    )
    _require_fingerprint_key(fingerprint_key)
    _require_configuration_unchanged(directory, expected)
    binding = _resolve_binding(profile)
    _require_configuration_unchanged(directory, expected)
    facts = _binding_facts(binding, profile)
    _require_configuration_unchanged(directory, expected)
    token = _consume_binding(binding, profile)
    _require_configuration_unchanged(directory, expected)
    started = _checked_now()
    try:
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            reader = Ga4AdminEditKeyEventInventoryClient(
                SafeHttpClient(client, {_HOST}),
                lambda: token,
                fingerprint_key=fingerprint_key,
            )
            inventory = reader.read_inventory(target)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "GA4_ADMIN_EDIT_INVENTORY_UNAVAILABLE",
            "GA4 Admin EDIT inventory could not be collected.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Check the configured GA4 Admin EDIT profile and collect a new inventory.",
        ) from None
    finished = _checked_now()
    if finished < started:
        raise _clock_unavailable()
    _require_configuration_unchanged(directory, expected)
    checked_inventory = _checked_inventory(inventory, fingerprint_key=fingerprint_key)
    if checked_inventory.target != target:
        raise _identity_mismatch()
    _require_configuration_unchanged(directory, expected)
    try:
        surface = _Ga4AdminEditExecutionInventoryObservationSurface(
            configuration_digest=expected,
            oauth_binding=facts,
            target=target,
            read_started_at=started,
            read_finished_at=finished,
            inventory=checked_inventory,
            inventory_digest=digest(checked_inventory),
            # The marker deliberately excludes itself. This placeholder never
            # escapes the factory before the complete surface is sealed.
            observation_content_marker="0" * 64,
        )
        observation = _public_observation(
            surface,
            observation_content_marker=_observation_content_marker(fingerprint_key, surface),
        )
    except Exception:
        raise _invalid_observation() from None
    checked_observation = revalidate_ga4_admin_edit_execution_observation(
        observation,
        fingerprint_key=fingerprint_key,
    )
    _require_configuration_unchanged(directory, expected)
    return checked_observation


def revalidate_ga4_admin_edit_execution_observation(
    value: object, *, fingerprint_key: bytes
) -> Ga4AdminEditExecutionInventoryObservation:
    """Rebuild a returned local snapshot and recheck both current-key markers."""

    _require_fingerprint_key(fingerprint_key)
    if type(value) is not Ga4AdminEditExecutionInventoryObservation:
        raise _invalid_observation()
    try:
        raw = value.model_dump(mode="json", warnings="error")
        checked = validate_json_value(_Ga4AdminEditExecutionInventoryObservationSurface, raw)
    except Exception:
        raise _invalid_observation() from None
    checked_inventory = _checked_inventory(checked.inventory, fingerprint_key=fingerprint_key)
    if checked_inventory.target != checked.target:
        raise _identity_mismatch()
    try:
        expected_marker = _observation_content_marker(fingerprint_key, checked)
        if not hmac.compare_digest(checked.observation_content_marker, expected_marker):
            raise ValueError("observation marker")
        return _public_observation(checked, observation_content_marker=expected_marker)
    except Exception:
        raise _invalid_observation() from None


def _observation_content_marker(
    fingerprint_key: bytes, value: _Ga4AdminEditExecutionInventoryObservationSurface
) -> str:
    """Authenticate the entire closed observation except this self-marker.

    This separate domain avoids treating the EDIT inventory's provider-content
    marker as a seal over local execution metadata. It proves only that the
    supplied transient local key authenticated this exact closed surface.
    """

    try:
        raw = value.model_dump(mode="json", warnings="error")
        if type(raw) is not dict:
            raise ValueError("observation mapping")
        marker = raw.pop("observation_content_marker", _MISSING)
        if type(marker) is not str or _DIGEST.fullmatch(marker) is None:
            raise ValueError("observation marker")
        material = (
            _OBSERVATION_MARKER_CONTEXT
            + _OBSERVATION_MARKER_PURPOSE
            + b"\x00"
            + canonical_bytes(raw)
        )
        return hmac.new(fingerprint_key, material, hashlib.sha256).hexdigest()
    except Exception:
        raise ValueError("observation marker") from None


def _public_observation(
    surface: _Ga4AdminEditExecutionInventoryObservationSurface,
    *,
    observation_content_marker: str,
) -> Ga4AdminEditExecutionInventoryObservation:
    """Convert an already checked surface without Python-mode scalar laundering."""

    return Ga4AdminEditExecutionInventoryObservation(
        schema_version=surface.schema_version,
        observation_kind=surface.observation_kind,
        configuration_digest=surface.configuration_digest,
        oauth_binding=surface.oauth_binding,
        target=surface.target,
        read_started_at=surface.read_started_at,
        read_finished_at=surface.read_finished_at,
        inventory=surface.inventory,
        inventory_digest=surface.inventory_digest,
        observation_content_marker=observation_content_marker,
        inventory_scope=surface.inventory_scope,
        fingerprint_key_continuity=surface.fingerprint_key_continuity,
        inventory_content_integrity=surface.inventory_content_integrity,
        observation_content_integrity=surface.observation_content_integrity,
        consistency=surface.consistency,
        granted_scopes=surface.granted_scopes,
        provider_create_permission=surface.provider_create_permission,
        current_provider_state=surface.current_provider_state,
        reservation_status=surface.reservation_status,
        execution_authority=surface.execution_authority,
        remote_mutations=surface.remote_mutations,
    )


def _binding_facts(binding: object, profile: Profile) -> Ga4AdminEditExecutionOAuthBindingFacts:
    """Copy only allowlisted facts from a lifecycle-issued, nonsecret binding."""

    if type(binding) is not Ga4AdminEditOAuthExecutionBinding:
        raise _identity_mismatch()
    profile_digest = digest(profile)
    try:
        values = (
            binding.profile_ref,
            binding.profile_digest,
            binding.provider,
            binding.purpose,
            binding.credential_source,
            binding.account_ref,
            binding.principal_ref,
            binding.client_ref,
            binding.api_scope,
        )
    except Exception:
        raise _identity_mismatch() from None
    expected = (
        profile.profile_id,
        profile_digest,
        "ga4_admin",
        "EDIT",
        _SOURCE,
        profile.account_ref,
        profile.principal_ref,
        profile.client_ref,
        _EDIT_SCOPE,
    )
    if not all(
        type(actual) is str
        and type(expected_value) is str
        and hmac.compare_digest(actual, expected_value)
        for actual, expected_value in zip(values, expected, strict=True)
    ):
        raise _identity_mismatch()
    try:
        return Ga4AdminEditExecutionOAuthBindingFacts(
            profile_ref=profile.profile_id,
            profile_configuration_digest=profile_digest,
            property_ref=f"ga4_admin.property.{profile.account_ref}",
            property_client_ref=profile.account_ref,
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        )
    except Exception:
        raise _identity_mismatch() from None


def _resolve_binding(profile: Profile) -> Ga4AdminEditOAuthExecutionBinding:
    """Preserve lifecycle diagnostics while hiding unexpected local failures."""

    try:
        return resolve_ga4_admin_edit_execution_binding(profile)
    except AdhelmError:
        raise
    except Exception:
        raise _oauth_unavailable() from None


def _consume_binding(binding: object, profile: Profile) -> str:
    """Consume exactly one lifecycle-issued bearer without exposing raw errors."""

    if type(binding) is not Ga4AdminEditOAuthExecutionBinding:
        raise _identity_mismatch()
    try:
        return bearer_for_ga4_admin_edit(binding, profile)
    except AdhelmError:
        raise
    except Exception:
        raise _oauth_unavailable() from None


def _checked_inventory(value: object, *, fingerprint_key: bytes) -> Ga4AdminEditKeyEventInventory:
    try:
        return revalidate_ga4_admin_edit_key_event_inventory(value, fingerprint_key=fingerprint_key)
    except AdhelmError as error:
        if error.code in {
            "FINGERPRINT_KEY_REQUIRED",
            "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH",
            "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH",
        }:
            raise
        raise _invalid_observation() from None
    except Exception:
        raise _invalid_observation() from None


def _require_fingerprint_key(value: object) -> None:
    if type(value) is not bytes or not 32 <= len(value) <= 4096:
        raise AdhelmError(
            "FINGERPRINT_KEY_REQUIRED",
            "A private local fingerprint key is required.",
            exit_code=ExitCode.AUTHORIZATION,
        )


def _require_configuration_unchanged(directory: Path, expected: str) -> None:
    try:
        current = digest(validate_configuration(load_configuration(directory)))
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None
    if not hmac.compare_digest(current, expected):
        raise _configuration_drift()


def _utc_now() -> datetime:
    """Private trusted-time seam for deterministic fake-only tests."""

    return datetime.now(UTC)


def _checked_now() -> datetime:
    try:
        value = _utc_now()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except Exception:
        raise _clock_unavailable() from None


def _invalid_input() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_ADMIN_EDIT_EXECUTION_INPUT",
        "GA4 Admin EDIT execution-observation input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _oauth_profile_required() -> AdhelmError:
    return AdhelmError(
        "GA4_ADMIN_EDIT_OAUTH_PROFILE_REQUIRED",
        "A configured Google OAuth GA4 Admin EDIT profile is required.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reconnect the configured Google OAuth GA4 Admin EDIT profile locally.",
    )


def _oauth_unavailable() -> AdhelmError:
    return AdhelmError(
        "GA4_ADMIN_EDIT_OAUTH_UNAVAILABLE",
        "The configured Google OAuth GA4 Admin EDIT credential is unavailable.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reconnect the configured Google OAuth GA4 Admin EDIT profile locally.",
    )


def _configuration_drift() -> AdhelmError:
    return AdhelmError(
        "CONFIGURATION_DRIFT",
        "Configuration changed during the GA4 Admin EDIT execution observation.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Review configuration and collect a new GA4 Admin EDIT inventory.",
    )


def _identity_mismatch() -> AdhelmError:
    return AdhelmError(
        "GA4_ADMIN_EDIT_EXECUTION_IDENTITY_MISMATCH",
        "GA4 Admin EDIT execution facts do not match the configured target.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Collect a new inventory with the configured Google OAuth GA4 Admin EDIT profile."
        ),
    )


def _invalid_observation() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_ADMIN_EDIT_EXECUTION_OBSERVATION",
        "GA4 Admin EDIT execution observation is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _clock_unavailable() -> AdhelmError:
    return AdhelmError(
        "GA4_ADMIN_EDIT_OBSERVATION_CLOCK_UNAVAILABLE",
        "A trusted local clock is unavailable for the GA4 Admin EDIT observation.",
        exit_code=ExitCode.PRECONDITION,
    )


__all__ = [
    "Ga4AdminEditExecutionInventoryObservation",
    "Ga4AdminEditExecutionInventoryRequest",
    "Ga4AdminEditExecutionOAuthBindingFacts",
    "observe_ga4_admin_edit_execution_inventory",
    "revalidate_ga4_admin_edit_execution_observation",
    "validate_ga4_admin_edit_execution_observation",
]
