"""Closed local creative-registration graph construction.

The installed workflow may register a customer-local image only after an asset
and its local analysis are already durable.  This module has no Ledger import:
it constructs and validates the exact graph that the narrow Ledger operation
will append under one creative-stream lock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from adhelm.canonical import canonical_bytes
from adhelm.creative import (
    AssetAnalysisRecord,
    CreativeAssetVersion,
    CreativeComponent,
    CreativeFamilyRecord,
    CreativeMessage,
    CreativeRendition,
    CreativeVersion,
    CreativeVersionRecord,
    ImageAnalysis,
    ImageFingerprintRecord,
    ServingCreativeManifest,
    ServingRendition,
    revalidate_asset_analysis_record,
    revalidate_creative_asset_version,
    validate_asset_analysis_binding,
    validate_creative_history_graph,
)
from adhelm.errors import AdhelmError
from adhelm.privacy import validate_data
from adhelm.strict_json import validate_json_value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def privacy_screen(self) -> _Strict:
        validate_data(self.model_dump(mode="json", warnings="error"))
        return self


class CreativeIdentityRegistrationInput(_Strict):
    """Declared local serving facts used to build one identity-image closure."""

    schema_version: Literal[1] = 1
    creative_family_id: str = Field(min_length=1, max_length=200)
    family_label: str = Field(min_length=1, max_length=200)
    provider_format: str = Field(min_length=1, max_length=120)
    locale: str = Field(min_length=1, max_length=32)
    destination_ref: str = Field(min_length=1, max_length=200)
    rendition_role: str = Field(min_length=1, max_length=80)
    components: tuple[CreativeComponent, ...] = Field(min_length=1, max_length=200)
    message: CreativeMessage | None = None
    provenance: Literal["CUSTOMER_LOCAL", "HUMAN_ANNOTATED"] = "CUSTOMER_LOCAL"
    family_created_at: datetime
    version_created_at: datetime

    @field_validator("family_created_at", "version_created_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("creative registration timestamps must be timezone-aware")
        return value.astimezone(UTC)


class CreativeRegistrationClosure(_Strict):
    """The complete dependency-ordered identity graph for one local image."""

    schema_version: Literal[1] = 1
    source_asset_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_analysis_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    asset: CreativeAssetVersion
    asset_analysis: AssetAnalysisRecord
    family: CreativeFamilyRecord
    rendition: CreativeRendition
    version: CreativeVersionRecord
    fingerprint: ImageFingerprintRecord

    @model_validator(mode="after")
    def dependency_graph_is_closed(self) -> CreativeRegistrationClosure:
        _validate_closure(self)
        return self


class CreativeTextRegistrationInput(_Strict):
    """Declared serving facts for a text-only creative version."""

    schema_version: Literal[1] = 1
    creative_family_id: str = Field(min_length=1, max_length=200)
    family_label: str = Field(min_length=1, max_length=200)
    provider_format: str = Field(min_length=1, max_length=120)
    locale: str = Field(min_length=1, max_length=32)
    destination_ref: str = Field(min_length=1, max_length=200)
    components: tuple[CreativeComponent, ...] = Field(min_length=1, max_length=200)
    message: CreativeMessage | None = None
    provenance: Literal["CUSTOMER_LOCAL", "HUMAN_ANNOTATED"] = "CUSTOMER_LOCAL"
    family_created_at: datetime
    version_created_at: datetime

    @field_validator("family_created_at", "version_created_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("creative registration timestamps must be timezone-aware")
        return value.astimezone(UTC)


class CreativeTextRegistrationClosure(_Strict):
    """The fixed family-plus-version graph for one text-only registration."""

    schema_version: Literal[1] = 1
    family: CreativeFamilyRecord
    version: CreativeVersionRecord

    @model_validator(mode="after")
    def dependency_graph_is_closed(self) -> CreativeTextRegistrationClosure:
        _validate_text_closure(self)
        return self


def build_creative_registration_closure(
    request: CreativeIdentityRegistrationInput | dict[str, Any],
    *,
    source_asset_record_digest: str,
    source_analysis_record_digest: str,
    asset: CreativeAssetVersion | dict[str, Any],
    asset_analysis: AssetAnalysisRecord | dict[str, Any],
    existing_family: CreativeFamilyRecord | dict[str, Any] | None = None,
) -> CreativeRegistrationClosure:
    """Build the only installed identity-image registration graph.

    This is a local construction operation.  It proves neither vault presence,
    current provider state, deployment, nor any remote write permission.
    """
    checked_request = _revalidate_model(CreativeIdentityRegistrationInput, request)
    checked_asset = revalidate_creative_asset_version(asset)
    checked_analysis = revalidate_asset_analysis_record(asset_analysis)
    validate_asset_analysis_binding(checked_asset, checked_analysis)
    rendition = CreativeRendition.from_asset(
        checked_asset,
        provider_format=checked_request.provider_format,
        locale=checked_request.locale,
    )
    family = _select_family(
        creative_family_id=checked_request.creative_family_id,
        family_label=checked_request.family_label,
        provenance=checked_request.provenance,
        family_created_at=checked_request.family_created_at,
        existing_family=existing_family,
    )
    manifest = ServingCreativeManifest(
        provider_format=checked_request.provider_format,
        locale=checked_request.locale,
        destination_ref=checked_request.destination_ref,
        rendition_refs=(
            ServingRendition(
                rendition_id=rendition.rendition_id,
                ordinal=0,
                role=checked_request.rendition_role,
            ),
        ),
        components=checked_request.components,
    )
    version = CreativeVersion(
        creative_family_id=family.creative_family_id,
        manifest=manifest,
        message=checked_request.message,
        analyzer_refs=(checked_analysis.asset_analysis_id,),
        provenance=checked_request.provenance,
        created_at=checked_request.version_created_at,
    )
    version_record = CreativeVersionRecord.model_validate_json(
        canonical_bytes(version.to_ledger_payload())
    )
    analysis = _analysis_from_record(checked_analysis)
    fingerprint = ImageFingerprintRecord.from_analysis(
        analysis,
        creative_version_id=version_record.creative_version_id,
        asset_version_id=checked_asset.asset_version_id,
        rendition_id=rendition.rendition_id,
        analysis_surface="FINAL_RENDITION",
    )
    return CreativeRegistrationClosure(
        source_asset_record_digest=source_asset_record_digest,
        source_analysis_record_digest=source_analysis_record_digest,
        asset=checked_asset,
        asset_analysis=checked_analysis,
        family=family,
        rendition=rendition,
        version=version_record,
        fingerprint=fingerprint,
    )


def build_text_creative_registration_closure(
    request: CreativeTextRegistrationInput | dict[str, Any],
    *,
    existing_family: CreativeFamilyRecord | dict[str, Any] | None = None,
) -> CreativeTextRegistrationClosure:
    """Build the complete closed graph for a text-only creative version."""
    checked_request = _revalidate_model(CreativeTextRegistrationInput, request)
    family = _select_family(
        creative_family_id=checked_request.creative_family_id,
        family_label=checked_request.family_label,
        provenance=checked_request.provenance,
        family_created_at=checked_request.family_created_at,
        existing_family=existing_family,
    )
    manifest = ServingCreativeManifest(
        provider_format=checked_request.provider_format,
        locale=checked_request.locale,
        destination_ref=checked_request.destination_ref,
        surface_kind="TEXT_ONLY",
        rendition_refs=(),
        components=checked_request.components,
    )
    version = CreativeVersion(
        creative_family_id=family.creative_family_id,
        manifest=manifest,
        message=checked_request.message,
        analyzer_refs=(),
        provenance=checked_request.provenance,
        created_at=checked_request.version_created_at,
    )
    return CreativeTextRegistrationClosure(
        family=family,
        version=CreativeVersionRecord.model_validate_json(
            canonical_bytes(version.to_ledger_payload())
        ),
    )


def revalidate_creative_registration_closure(
    value: object,
) -> CreativeRegistrationClosure | CreativeTextRegistrationClosure:
    """Reparse a raw or constructed closure before the locked Ledger operation."""
    if type(value) is CreativeRegistrationClosure:
        return _revalidate_model(CreativeRegistrationClosure, value)
    if type(value) is CreativeTextRegistrationClosure:
        return _revalidate_model(CreativeTextRegistrationClosure, value)
    if type(value) is not dict:
        raise _invalid_closure()
    keys = set(value)
    if keys == set(CreativeRegistrationClosure.model_fields):
        return _revalidate_model(CreativeRegistrationClosure, value)
    if keys == set(CreativeTextRegistrationClosure.model_fields):
        return _revalidate_model(CreativeTextRegistrationClosure, value)
    raise _invalid_closure()


def closure_records(
    closure: CreativeRegistrationClosure | CreativeTextRegistrationClosure,
) -> tuple[tuple[str, BaseModel], ...]:
    """Return the fixed prerequisite order without exposing a generic append API."""
    checked = revalidate_creative_registration_closure(closure)
    if isinstance(checked, CreativeRegistrationClosure):
        return (
            ("CREATIVE_FAMILY", checked.family),
            ("CREATIVE_RENDITION", checked.rendition),
            ("CREATIVE_VERSION", checked.version),
            ("CREATIVE_FINGERPRINT", checked.fingerprint),
        )
    return (
        ("CREATIVE_FAMILY", checked.family),
        ("CREATIVE_VERSION", checked.version),
    )


def _validate_closure(closure: CreativeRegistrationClosure) -> None:
    asset = revalidate_creative_asset_version(closure.asset)
    analysis = revalidate_asset_analysis_record(closure.asset_analysis)
    validate_asset_analysis_binding(asset, analysis)
    records = (
        ("CREATIVE_ASSET_VERSION", asset),
        ("CREATIVE_ASSET_ANALYSIS", analysis),
        ("CREATIVE_FAMILY", closure.family),
        ("CREATIVE_RENDITION", closure.rendition),
        ("CREATIVE_VERSION", closure.version),
        ("CREATIVE_FINGERPRINT", closure.fingerprint),
    )
    validate_creative_history_graph(
        [
            {
                "record_kind": kind,
                "payload": value.model_dump(mode="json", warnings="error"),
            }
            for kind, value in records
        ]
    )


def _validate_text_closure(closure: CreativeTextRegistrationClosure) -> None:
    validate_creative_history_graph(
        [
            {
                "record_kind": "CREATIVE_FAMILY",
                "payload": closure.family.model_dump(mode="json", warnings="error"),
            },
            {
                "record_kind": "CREATIVE_VERSION",
                "payload": closure.version.model_dump(mode="json", warnings="error"),
            },
        ]
    )


def _select_family(
    *,
    creative_family_id: str,
    family_label: str,
    provenance: Literal["CUSTOMER_LOCAL", "HUMAN_ANNOTATED"],
    family_created_at: datetime,
    existing_family: CreativeFamilyRecord | dict[str, Any] | None,
) -> CreativeFamilyRecord:
    if existing_family is None:
        return CreativeFamilyRecord(
            creative_family_id=creative_family_id,
            family_label=family_label,
            created_at=family_created_at,
            provenance=provenance,
        )
    family = _revalidate_model(CreativeFamilyRecord, existing_family)
    if (
        family.creative_family_id != creative_family_id
        or family.family_label != family_label
        or family.provenance != provenance
        or family.created_at != family_created_at
    ):
        raise AdhelmError(
            "CREATIVE_FAMILY_BINDING_INVALID",
            "The selected creative family does not match the registration facts.",
            exit_code=2,
        )
    return family


def _invalid_closure() -> AdhelmError:
    return AdhelmError(
        "INVALID_CREATIVE_REGISTRATION",
        "Creative registration data is invalid.",
        exit_code=2,
    )


def _analysis_from_record(record: AssetAnalysisRecord) -> ImageAnalysis:
    return ImageAnalysis(
        analyzer_name=record.analyzer_name,
        analyzer_version=record.analyzer_version,
        mime_type=record.mime_type,
        width_px=record.width_px,
        height_px=record.height_px,
        aspect_ratio=record.aspect_ratio,
        palette=record.palette,
        mean_lightness_basis_points=record.mean_lightness_basis_points,
        mean_saturation_basis_points=record.mean_saturation_basis_points,
        contrast_basis_points=record.contrast_basis_points,
        colorfulness_basis_points=record.colorfulness_basis_points,
        extraction_statuses=record.extraction_statuses,
    )


def _revalidate_model[T: BaseModel](model: type[T], value: object) -> T:
    try:
        if type(value) is model:
            raw = cast(BaseModel, value).model_dump(mode="json", warnings="error")
        elif type(value) is dict:
            raw = value
        else:
            raise ValueError("closed model type")
        if type(raw) is not dict:
            raise ValueError("closed model shape")
        return validate_json_value(model, raw)
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise AdhelmError(
            "INVALID_CREATIVE_REGISTRATION",
            "Creative registration data is invalid.",
            exit_code=2,
        ) from None
