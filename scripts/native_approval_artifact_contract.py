"""Closed build-only selectors for an unsigned macOS approval-host artifact.

This module accepts the one versioned JSON contract used to generate reviewed
Swift constants in an isolated source tree.  It does not load configuration at
host runtime, inspect a signing identity, invoke Swift, or perform any OS,
credential, network, installation, signing, or launch operation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final, cast

import rfc8785

_SCHEMA_VERSION: Final = 1
_MAXIMUM_CONTRACT_BYTES: Final = 16_384
_DESCRIPTOR_SCOPE: Final = "UNSIGNED_BUILD_METADATA_ONLY_NOT_OS_ATTESTATION"
_ARTIFACT_STATUS: Final = "UNSIGNED_UNVERIFIED"
_EXACT_KEYS: Final = frozenset(
    {
        "schema_version",
        "host_ref",
        "signing_key_ref",
        "mach_service_name",
        "client_bundle_identifier",
        "client_team_identifier",
        "host_bundle_identifier",
        "host_team_identifier",
        "signing_key_application_tag_hex",
        "signing_key_access_group",
        "key_storage",
        "provenance_service",
        "provenance_account",
        "maximum_challenge_bytes",
        "maximum_challenge_lifetime_seconds",
        "maximum_issued_at_skew_seconds",
        "maximum_subplans",
        "maximum_policy_items",
    }
)
_METADATA_FIELD_ORDER: Final = (
    "schema_version",
    "host_ref",
    "signing_key_ref",
    "mach_service_name",
    "client_bundle_identifier",
    "client_team_identifier",
    "host_bundle_identifier",
    "host_team_identifier",
    "signing_key_application_tag_hex",
    "signing_key_access_group",
    "key_storage",
    "provenance_service",
    "provenance_account",
    "maximum_challenge_bytes",
    "maximum_challenge_lifetime_seconds",
    "maximum_issued_at_skew_seconds",
    "maximum_subplans",
    "maximum_policy_items",
)
_KEY_STORAGES: Final = frozenset({"KEYCHAIN_P256", "SECURE_ENCLAVE_P256"})
_HEX = re.compile(r"^[0-9a-f]+$")


class NativeApprovalArtifactContractError(ValueError):
    """One bounded failure for an invalid build-only selector contract."""

    def __init__(self) -> None:
        super().__init__("Native approval-host artifact contract is invalid.")


@dataclass(frozen=True, slots=True)
class NativeApprovalHostArtifactContractV1:
    """The closed non-secret source for generated native build constants."""

    schema_version: int
    host_ref: str
    signing_key_ref: str
    mach_service_name: str
    client_bundle_identifier: str
    client_team_identifier: str
    host_bundle_identifier: str
    host_team_identifier: str
    signing_key_application_tag_hex: str
    signing_key_access_group: str | None
    key_storage: str
    provenance_service: str
    provenance_account: str
    maximum_challenge_bytes: int
    maximum_challenge_lifetime_seconds: int
    maximum_issued_at_skew_seconds: int
    maximum_subplans: int
    maximum_policy_items: int

    def __post_init__(self) -> None:
        _validate_contract_data(_contract_data(self))

    def to_data(self) -> dict[str, str | int | None]:
        """Return newly rebuilt, type-checked data for canonical serialization."""
        try:
            data = _contract_data(self)
            _validate_contract_data(data)
            return data
        except NativeApprovalArtifactContractError:
            raise
        except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError):
            raise NativeApprovalArtifactContractError() from None

    @property
    def signing_key_application_tag(self) -> bytes:
        checked = _checked_contract(self)
        try:
            return bytes.fromhex(checked.signing_key_application_tag_hex)
        except ValueError as error:  # pragma: no cover - checked before construction
            raise NativeApprovalArtifactContractError() from error

    @property
    def protocol_configuration(self) -> dict[str, str | int]:
        checked = _checked_contract(self)
        return _protocol_configuration(checked)

    @property
    def protocol_configuration_canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.protocol_configuration)

    @property
    def protocol_configuration_digest(self) -> str:
        return _sha256(self.protocol_configuration_canonical_bytes)


@dataclass(frozen=True, slots=True)
class NativeApprovalHostArtifactDescriptorV1:
    """Rebuildable unsigned metadata evidence; never a trust or install claim."""

    contract: NativeApprovalHostArtifactContractV1

    @property
    def metadata_canonical_bytes(self) -> bytes:
        return canonical_native_approval_artifact_contract(_checked_descriptor_contract(self))

    @property
    def metadata_digest(self) -> str:
        return _sha256(self.metadata_canonical_bytes)

    @property
    def protocol_configuration(self) -> dict[str, str | int]:
        return _checked_descriptor_contract(self).protocol_configuration

    @property
    def protocol_configuration_digest(self) -> str:
        return _sha256(_canonical_bytes(self.protocol_configuration))

    @property
    def expected_client_code_signing_requirement(self) -> str:
        checked = _checked_descriptor_contract(self)
        return _code_signing_requirement(
            checked.client_bundle_identifier,
            checked.client_team_identifier,
        )

    @property
    def expected_host_code_signing_requirement(self) -> str:
        checked = _checked_descriptor_contract(self)
        return _code_signing_requirement(
            checked.host_bundle_identifier,
            checked.host_team_identifier,
        )

    def to_data(self) -> dict[str, object]:
        checked = _checked_descriptor_contract(self)
        metadata = checked.to_data()
        protocol_configuration = checked.protocol_configuration
        return {
            "schema_version": _SCHEMA_VERSION,
            "scope": _DESCRIPTOR_SCOPE,
            "artifact_status": _ARTIFACT_STATUS,
            "metadata": metadata,
            "metadata_digest": _sha256(_canonical_bytes(metadata)),
            "protocol_configuration": protocol_configuration,
            "protocol_configuration_digest": _sha256(_canonical_bytes(protocol_configuration)),
            "expected_client_code_signing_requirement": (
                self.expected_client_code_signing_requirement
            ),
            "expected_host_code_signing_requirement": (self.expected_host_code_signing_requirement),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_data())

    @property
    def digest(self) -> str:
        return _sha256(self.canonical_bytes)


def parse_native_approval_artifact_contract_json(
    raw: str | bytes,
) -> NativeApprovalHostArtifactContractV1:
    """Parse one bounded JSON object before any generated-source operation."""
    try:
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        if type(encoded) is not bytes or not encoded or len(encoded) > _MAXIMUM_CONTRACT_BYTES:
            raise ValueError
        decoded = encoded.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise NativeApprovalArtifactContractError() from None
    return parse_native_approval_artifact_contract(value)


def parse_native_approval_artifact_contract(
    raw: object,
) -> NativeApprovalHostArtifactContractV1:
    """Rebuild the closed contract from original JSON-shaped scalar values."""
    try:
        if type(raw) is not dict:
            raise ValueError
        _validate_contract_data(raw)
        return NativeApprovalHostArtifactContractV1(**raw)
    except NativeApprovalArtifactContractError:
        raise
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise NativeApprovalArtifactContractError() from None


def canonical_native_approval_artifact_contract(
    contract: object,
) -> bytes:
    """Return the deterministic closed metadata bytes for one rebuilt contract."""
    return _canonical_bytes(_checked_contract(contract).to_data())


def native_approval_artifact_descriptor(
    contract: object,
) -> NativeApprovalHostArtifactDescriptorV1:
    """Build unsigned, non-attesting descriptor data from validated selectors."""
    return NativeApprovalHostArtifactDescriptorV1(_checked_contract(contract))


def render_generated_approval_host_build_metadata(contract: object) -> str:
    """Render only fixed Swift syntax into an isolated generated source file."""
    checked = _checked_contract(contract)
    access_group = (
        "nil"
        if checked.signing_key_access_group is None
        else _swift_string_literal(checked.signing_key_access_group)
    )
    key_storage = {
        "KEYCHAIN_P256": ".keychainP256",
        "SECURE_ENCLAVE_P256": ".secureEnclaveP256",
    }[checked.key_storage]
    tag = ", ".join(f"0x{byte:02x}" for byte in checked.signing_key_application_tag)
    return "\n".join(
        (
            "import Foundation",
            "",
            "package enum GeneratedApprovalHostBuildMetadata {",
            "  package static func make() throws -> ApprovalHostBuildMetadataV1 {",
            "    let client = try ApprovalHostCodeIdentitySelectorV1(",
            "      role: .client,",
            f"      bundleIdentifier: {_swift_string_literal(checked.client_bundle_identifier)},",
            f"      teamIdentifier: {_swift_string_literal(checked.client_team_identifier)}",
            "    )",
            "    let host = try ApprovalHostCodeIdentitySelectorV1(",
            "      role: .host,",
            f"      bundleIdentifier: {_swift_string_literal(checked.host_bundle_identifier)},",
            f"      teamIdentifier: {_swift_string_literal(checked.host_team_identifier)}",
            "    )",
            "    return try ApprovalHostBuildMetadataV1(",
            f"      hostReference: {_swift_string_literal(checked.host_ref)},",
            f"      signingKeyReference: {_swift_string_literal(checked.signing_key_ref)},",
            f"      machServiceName: {_swift_string_literal(checked.mach_service_name)},",
            "      clientCodeIdentity: client,",
            "      hostCodeIdentity: host,",
            f"      signingKeyApplicationTag: Data([{tag}]),",
            f"      signingKeyAccessGroup: {access_group},",
            f"      keyStorage: {key_storage},",
            f"      provenanceService: {_swift_string_literal(checked.provenance_service)},",
            f"      provenanceAccount: {_swift_string_literal(checked.provenance_account)},",
            "      limits: ApprovalHostLimits(",
            f"        maximumChallengeBytes: {checked.maximum_challenge_bytes},",
            f"        maximumChallengeLifetime: {checked.maximum_challenge_lifetime_seconds},",
            f"        maximumIssuedAtSkew: {checked.maximum_issued_at_skew_seconds},",
            f"        maximumSubplans: {checked.maximum_subplans},",
            f"        maximumPolicyItems: {checked.maximum_policy_items}",
            "      )",
            "    )",
            "  }",
            "}",
            "",
        )
    )


def render_generated_installed_approval_bridge_binding(contract: object) -> str:
    """Render the sole configured Python bridge-binding replacement module.

    The resulting source is for an isolated artifact tree only.  It contains
    no loader, runtime selector, receipt, public-key pin, or process behavior;
    the M34 private factory reconstructs and checks every literal before it can
    issue its opaque binding.
    """
    checked = _checked_contract(contract)
    metadata = _contract_data(checked)
    metadata_digest = _sha256(_canonical_bytes(metadata))
    protocol_digest = _sha256(_canonical_bytes(_protocol_configuration(checked)))
    fields = tuple((key, metadata[key]) for key in _METADATA_FIELD_ORDER)
    rendered_fields = tuple(
        f"    ({_python_literal(key)}, {_python_literal(value)})," for key, value in fields
    )
    return "\n".join(
        (
            '"""Build-generated static installed approval bridge binding."""',
            "",
            "from __future__ import annotations",
            "",
            "from typing import Final",
            "",
            "from adhelm._installed_native_approval_bridge import (",
            "    _issue_compiled_installed_approval_bridge_binding_from_generated_metadata,",
            ")",
            "",
            "_METADATA_FIELDS: Final = (",
            *rendered_fields,
            ")",
            f"_METADATA_DIGEST: Final = {_python_literal(metadata_digest)}",
            f"_PROTOCOL_CONFIGURATION_DIGEST: Final = {_python_literal(protocol_digest)}",
            "",
            "",
            "def _resolve_generated_installed_approval_bridge_binding() -> object:",
            "    return _issue_compiled_installed_approval_bridge_binding_from_generated_metadata(",
            "        metadata_fields=_METADATA_FIELDS,",
            "        expected_metadata_digest=_METADATA_DIGEST,",
            "        expected_protocol_configuration_digest=_PROTOCOL_CONFIGURATION_DIGEST,",
            "    )",
            "",
        )
    )


def render_generated_meta_campaign_pause_approval_bridge_binding_v2(contract: object) -> str:
    """Render a separate static Meta binding for an explicitly selected artifact.

    This does not alter the existing V1 renderer or ordinary-wheel defaults.
    A later artifact profile must pair these bytes with the native route
    binding and fixed Meta helper; this pure function performs no build or
    installation and does not attest an installed host.
    """
    checked = _checked_contract(contract)
    metadata = _contract_data(checked)
    metadata_digest = _sha256(_canonical_bytes(metadata))
    protocol_digest = _sha256(_canonical_bytes(_protocol_configuration(checked)))
    fields = tuple((key, metadata[key]) for key in _METADATA_FIELD_ORDER)
    rendered_fields = tuple(
        f"    ({_python_literal(key)}, {_python_literal(value)})," for key, value in fields
    )
    return "\n".join(
        (
            '"""Build-generated static Meta campaign PAUSE approval bridge V2 binding."""',
            "",
            "from __future__ import annotations",
            "",
            "from typing import Final",
            "",
            "from adhelm._compiled_meta_campaign_pause_approval_bridge_v2 import (",
            "    _issue_compiled_meta_campaign_pause_approval_bridge_binding_v2_"
            "from_generated_metadata,",
            ")",
            "",
            "_METADATA_FIELDS: Final = (",
            *rendered_fields,
            ")",
            f"_METADATA_DIGEST: Final = {_python_literal(metadata_digest)}",
            f"_PROTOCOL_CONFIGURATION_DIGEST: Final = {_python_literal(protocol_digest)}",
            "",
            "",
            "def _resolve_generated_meta_campaign_pause_approval_bridge_binding_v2() -> object:",
            "    return _issue_compiled_meta_campaign_pause_approval_bridge_binding_v2_"
            "from_generated_metadata(",
            "        metadata_fields=_METADATA_FIELDS,",
            "        expected_metadata_digest=_METADATA_DIGEST,",
            "        expected_protocol_configuration_digest=_PROTOCOL_CONFIGURATION_DIGEST,",
            "    )",
            "",
        )
    )


def render_generated_meta_campaign_pause_approval_route_binding_v2(contract: object) -> str:
    """Render a closed native Meta route binding against the unchanged metadata.

    The configured resolver must revalidate the supplied bootstrap metadata
    and configuration against these static expected digests. Absence is
    represented only by the separate checked-in default source; a malformed
    configured binding is an error, never an implicit V1 fallback.
    """
    checked = _checked_contract(contract)
    metadata_digest = _sha256(_canonical_bytes(_contract_data(checked)))
    protocol_digest = _sha256(_canonical_bytes(_protocol_configuration(checked)))
    return "\n".join(
        (
            "package enum GeneratedMetaCampaignPauseApprovalRouteBinding {",
            "  package static func resolve(",
            "    metadata: ApprovalHostBuildMetadataV1,",
            "    configuration: TrustedHostConfiguration",
            "  ) throws -> NativeMetaCampaignPauseApprovalRouteBindingV2? {",
            "    try NativeMetaCampaignPauseApprovalRouteBindingV2(",
            "      metadata: metadata,",
            "      configuration: configuration,",
            f"      expectedMetadataDigest: {_swift_string_literal(metadata_digest)},",
            f"      expectedProtocolConfigurationDigest: {_swift_string_literal(protocol_digest)}",
            "    )",
            "  }",
            "}",
            "",
        )
    )


def _checked_contract(value: object) -> NativeApprovalHostArtifactContractV1:
    if type(value) is not NativeApprovalHostArtifactContractV1:
        raise NativeApprovalArtifactContractError()
    try:
        return parse_native_approval_artifact_contract(value.to_data())
    except NativeApprovalArtifactContractError:
        raise
    except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError):
        raise NativeApprovalArtifactContractError() from None


def _checked_descriptor_contract(
    value: object,
) -> NativeApprovalHostArtifactContractV1:
    if type(value) is not NativeApprovalHostArtifactDescriptorV1:
        raise NativeApprovalArtifactContractError()
    try:
        return _checked_contract(value.contract)
    except NativeApprovalArtifactContractError:
        raise
    except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError):
        raise NativeApprovalArtifactContractError() from None


def _contract_data(contract: NativeApprovalHostArtifactContractV1) -> dict[str, str | int | None]:
    return {
        "schema_version": contract.schema_version,
        "host_ref": contract.host_ref,
        "signing_key_ref": contract.signing_key_ref,
        "mach_service_name": contract.mach_service_name,
        "client_bundle_identifier": contract.client_bundle_identifier,
        "client_team_identifier": contract.client_team_identifier,
        "host_bundle_identifier": contract.host_bundle_identifier,
        "host_team_identifier": contract.host_team_identifier,
        "signing_key_application_tag_hex": contract.signing_key_application_tag_hex,
        "signing_key_access_group": contract.signing_key_access_group,
        "key_storage": contract.key_storage,
        "provenance_service": contract.provenance_service,
        "provenance_account": contract.provenance_account,
        "maximum_challenge_bytes": contract.maximum_challenge_bytes,
        "maximum_challenge_lifetime_seconds": contract.maximum_challenge_lifetime_seconds,
        "maximum_issued_at_skew_seconds": contract.maximum_issued_at_skew_seconds,
        "maximum_subplans": contract.maximum_subplans,
        "maximum_policy_items": contract.maximum_policy_items,
    }


def _validate_contract_data(data: object) -> None:
    if type(data) is not dict or set(data) != _EXACT_KEYS:
        raise NativeApprovalArtifactContractError()
    _require_exact_int(data["schema_version"], minimum=_SCHEMA_VERSION, maximum=_SCHEMA_VERSION)
    _require_safe_text(data["host_ref"], maximum_bytes=256)
    _require_safe_text(data["signing_key_ref"], maximum_bytes=256)
    _require_bundle_identifier(data["mach_service_name"])
    _require_bundle_identifier(data["client_bundle_identifier"])
    _require_team_identifier(data["client_team_identifier"])
    _require_bundle_identifier(data["host_bundle_identifier"])
    _require_team_identifier(data["host_team_identifier"])
    if data["client_bundle_identifier"] == data["host_bundle_identifier"]:
        raise NativeApprovalArtifactContractError()
    _require_tag_hex(data["signing_key_application_tag_hex"])
    _require_access_group(data["signing_key_access_group"], data["host_team_identifier"])
    if type(data["key_storage"]) is not str or data["key_storage"] not in _KEY_STORAGES:
        raise NativeApprovalArtifactContractError()
    _require_fixed_selector(data["provenance_service"])
    _require_fixed_selector(data["provenance_account"])
    _require_exact_int(data["maximum_challenge_bytes"], minimum=1, maximum=65_536)
    _require_exact_int(data["maximum_challenge_lifetime_seconds"], minimum=1, maximum=300)
    _require_exact_int(data["maximum_issued_at_skew_seconds"], minimum=0, maximum=300)
    _require_exact_int(data["maximum_subplans"], minimum=1, maximum=6)
    _require_exact_int(data["maximum_policy_items"], minimum=1, maximum=64)


def _require_safe_text(value: object, *, maximum_bytes: int) -> None:
    if type(value) is not str:
        raise NativeApprovalArtifactContractError()
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise NativeApprovalArtifactContractError() from None
    if (
        not value
        or len(encoded) > maximum_bytes
        or any(ord(item) < 32 or ord(item) == 127 for item in value)
    ):
        raise NativeApprovalArtifactContractError()


def _require_bundle_identifier(value: object) -> None:
    _require_fixed_ascii(value, maximum_bytes=255)
    assert type(value) is str  # narrowed by _require_fixed_ascii
    labels = value.split(".")
    if len(labels) < 3 or any(
        not label
        or len(label.encode("ascii")) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(
            not (character.isascii() and (character.isalnum() or character == "-"))
            for character in label
        )
        for label in labels
    ):
        raise NativeApprovalArtifactContractError()


def _require_team_identifier(value: object) -> None:
    if (
        type(value) is not str
        or len(value) != 10
        or any(not ("A" <= character <= "Z" or "0" <= character <= "9") for character in value)
    ):
        raise NativeApprovalArtifactContractError()


def _require_tag_hex(value: object) -> None:
    if (
        type(value) is not str
        or len(value) > 256
        or len(value) % 2
        or _HEX.fullmatch(value) is None
    ):
        raise NativeApprovalArtifactContractError()
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        raise NativeApprovalArtifactContractError() from None
    if not decoded or len(decoded) > 128:
        raise NativeApprovalArtifactContractError()


def _require_access_group(value: object, host_team_identifier: object) -> None:
    if value is None:
        return
    if type(value) is not str or type(host_team_identifier) is not str:
        raise NativeApprovalArtifactContractError()
    prefix = f"{host_team_identifier}."
    if not value.startswith(prefix):
        raise NativeApprovalArtifactContractError()
    _require_bundle_identifier(value[len(prefix) :])


def _require_fixed_selector(value: object) -> None:
    _require_fixed_ascii(value, maximum_bytes=256)


def _require_fixed_ascii(value: object, *, maximum_bytes: int) -> None:
    if type(value) is not str:
        raise NativeApprovalArtifactContractError()
    try:
        encoded = value.encode("ascii")
    except UnicodeError:
        raise NativeApprovalArtifactContractError() from None
    lowered = value.lower()
    if (
        not value
        or len(encoded) > maximum_bytes
        or "*" in value
        or "${" in value
        or "placeholder" in lowered
        or "todo" in lowered
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise NativeApprovalArtifactContractError()


def _require_exact_int(value: object, *, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise NativeApprovalArtifactContractError()


def _protocol_configuration(
    contract: NativeApprovalHostArtifactContractV1,
) -> dict[str, str | int]:
    return {
        "host_ref": contract.host_ref,
        "signing_key_ref": contract.signing_key_ref,
        "maximum_challenge_bytes": contract.maximum_challenge_bytes,
        "maximum_challenge_lifetime_seconds": contract.maximum_challenge_lifetime_seconds,
        "maximum_issued_at_skew_seconds": contract.maximum_issued_at_skew_seconds,
        "maximum_subplans": contract.maximum_subplans,
        "maximum_policy_items": contract.maximum_policy_items,
    }


def _code_signing_requirement(bundle_identifier: str, team_identifier: str) -> str:
    return (
        f'anchor apple generic and identifier "{bundle_identifier}"'
        f' and certificate leaf[subject.OU] = "{team_identifier}"'
    )


def _swift_string_literal(value: str) -> str:
    return '"' + "".join(f"\\u{{{ord(character):x}}}" for character in value) + '"'


def _python_literal(value: object) -> str:
    """Render one closed scalar as source without admitting executable syntax."""
    if value is None:
        return "None"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    raise NativeApprovalArtifactContractError()


def _canonical_bytes(value: object) -> bytes:
    try:
        return rfc8785.dumps(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        raise NativeApprovalArtifactContractError() from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError
