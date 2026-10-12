"""Closed local creative registration from retained local evidence.

This workflow is deliberately narrow.  It can construct a local identity-image
or text-only creative graph, but it does not create a provider creative, deploy
anything, or establish current provider state.  Image registrations name two
already-retained records and re-read/re-analyse the local asset before the
fixed Ledger closure is appended.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.assets import read_image, store_image
from adhelm.canonical import canonical_bytes
from adhelm.creative import (
    AssetAnalysisRecord,
    CreativeAssetVersion,
    CreativeComponent,
    CreativeFamilyRecord,
    CreativeMessage,
    analyze_image,
    revalidate_asset_analysis_record,
    revalidate_creative_asset_version,
    revalidate_creative_payload,
    validate_asset_analysis_binding,
)
from adhelm.creative_registration_closure import (
    CreativeIdentityRegistrationInput,
    CreativeRegistrationClosure,
    CreativeTextRegistrationClosure,
    CreativeTextRegistrationInput,
    build_creative_registration_closure,
    build_text_creative_registration_closure,
    closure_records,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import StrictModel
from adhelm.strict_json import validate_json_value


class IdentityImageCreativeSource(StrictModel):
    """The only image source accepted by the installed local workflow."""

    registration_kind: Literal["IDENTITY_IMAGE"] = "IDENTITY_IMAGE"
    asset_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    asset_analysis_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class TextOnlyCreativeSource(StrictModel):
    """A declared, timestamped text-only serving surface."""

    registration_kind: Literal["TEXT_ONLY"] = "TEXT_ONLY"


CreativeRegistrationSource = Annotated[
    IdentityImageCreativeSource | TextOnlyCreativeSource,
    Field(discriminator="registration_kind"),
]


class CreativeRegistrationRequest(StrictModel):
    """Closed customer-local input for one identity image or text-only version.

    The caller supplies declared marketing metadata only.  The workflow derives
    media identifiers, final-rendition identity, fingerprint, family/version
    provenance, and all retained evidence references.
    """

    schema_version: Literal[1] = 1
    creative_family_id: str = Field(min_length=1, max_length=200)
    family_label: str = Field(min_length=1, max_length=200)
    provider_format: str = Field(min_length=1, max_length=120)
    locale: str = Field(min_length=1, max_length=32)
    destination_ref: str = Field(min_length=1, max_length=200)
    components: tuple[CreativeComponent, ...] = Field(min_length=1, max_length=200)
    message: CreativeMessage | None = None
    created_at: datetime
    source: CreativeRegistrationSource

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def declared_component_evidence_only(self) -> CreativeRegistrationRequest:
        for component in self.components:
            if (
                component.provenance != "DECLARED"
                or component.confidence_basis_points != 10_000
                or component.extraction_status != "DECLARED"
            ):
                raise ValueError("creative registration components must be declared evidence")
        return self


class _CreativeImageAnalysisReceiptSurface(StrictModel):
    """Bounded safe result of one local image analysis invocation."""

    schema_version: Literal[1] = 1
    asset_version_id: str = Field(pattern=r"^asset:sha256:[a-f0-9]{64}$")
    asset_analysis_id: str = Field(pattern=r"^asset-analysis:sha256:[a-f0-9]{64}$")
    asset_content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    mime_type: Literal[
        "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"
    ]
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    aspect_ratio: str = Field(min_length=3, max_length=32)
    palette_color_count: int = Field(ge=1, le=512)
    observation_persisted: bool
    asset_record_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    asset_analysis_record_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    asset_availability: Literal["AVAILABLE", "NOT_RETAINED_EPHEMERAL"]
    analysis_provenance: Literal["LOCAL_EXTRACTED"] = "LOCAL_EXTRACTED"
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator(
        "observation_persisted", "remote_mutations", "execution_authority", mode="before"
    )
    @classmethod
    def exact_boolean_controls(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("analysis receipt controls must be JSON booleans")
        return value

    @model_validator(mode="after")
    def retention_fields_match(self) -> _CreativeImageAnalysisReceiptSurface:
        if (self.asset_record_digest is None) != (self.asset_analysis_record_digest is None):
            raise ValueError("analysis receipt source digests must be both present or both absent")
        has_digests = self.asset_record_digest is not None
        if self.observation_persisted != has_digests:
            raise ValueError("analysis receipt persistence fields disagree")
        if self.observation_persisted and self.asset_availability != "AVAILABLE":
            raise ValueError("retained analysis requires available local asset evidence")
        if not self.observation_persisted and self.asset_availability != "NOT_RETAINED_EPHEMERAL":
            raise ValueError("ephemeral analysis cannot claim retained local asset evidence")
        return self


class CreativeImageAnalysisReceipt(_CreativeImageAnalysisReceiptSurface):
    """Bounded public result of one local image analysis invocation."""

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_receipt(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        try:
            _validate_public_receipt_instance(
                self,
                expected_type=CreativeImageAnalysisReceipt,
                surface=_CreativeImageAnalysisReceiptSurface,
            )
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("analysis receipt output")
            validate_json_value(
                _CreativeImageAnalysisReceiptSurface,
                cast(dict[str, Any], _receipt_json_shape(output)),
            )
            return output
        except Exception:
            raise ValueError("Creative image analysis receipt is invalid") from None


class _CreativeRegistrationReceiptSurface(StrictModel):
    """Safe local-only receipt for a verified closed registration graph."""

    schema_version: Literal[1] = 1
    registration_kind: Literal["IDENTITY_IMAGE", "TEXT_ONLY"]
    local_registration_status: Literal["RETAINED_VERIFIED_LOCAL_ONLY"] = (
        "RETAINED_VERIFIED_LOCAL_ONLY"
    )
    registration_recovery_status: Literal["REGISTERED", "RECOVERED_STAGING", "ALREADY_REGISTERED"]
    record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[a-f0-9]{64}$")
    version_created_at: datetime
    version_time_basis: Literal["DECLARED_LOCAL_VERSION_TIME"] = "DECLARED_LOCAL_VERSION_TIME"
    rendition_id: str | None = Field(default=None, pattern=r"^rendition:sha256:[a-f0-9]{64}$")
    fingerprint_id: str | None = Field(default=None, pattern=r"^fingerprint:sha256:[a-f0-9]{64}$")
    remote_mutations: Literal[False] = False
    execution_authority: Literal[False] = False
    deployment_status: Literal["NOT_CREATED"] = "NOT_CREATED"
    provider_currentness: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    recovery_action: Literal["INSPECT_BEFORE_RETRY"] = "INSPECT_BEFORE_RETRY"

    @field_validator("remote_mutations", "execution_authority", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("registration receipt controls must be false JSON booleans")
        return value

    @field_validator("version_created_at")
    @classmethod
    def version_created_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("version_created_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def source_kind_is_exact(self) -> _CreativeRegistrationReceiptSurface:
        image = self.registration_kind == "IDENTITY_IMAGE"
        image_fields = (
            self.rendition_id,
            self.fingerprint_id,
        )
        if image != all(value is not None for value in image_fields):
            raise ValueError("registration receipt source evidence disagrees with its kind")
        if not image and any(value is not None for value in image_fields):
            raise ValueError("text-only registration cannot claim image evidence")
        return self


class CreativeRegistrationReceipt(_CreativeRegistrationReceiptSurface):
    """Safe local-only receipt for a verified closed registration graph."""

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_receipt(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        try:
            _validate_public_receipt_instance(
                self,
                expected_type=CreativeRegistrationReceipt,
                surface=_CreativeRegistrationReceiptSurface,
            )
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("registration receipt output")
            validate_json_value(
                _CreativeRegistrationReceiptSurface,
                cast(dict[str, Any], _receipt_json_shape(output)),
            )
            return output
        except Exception:
            raise ValueError("Creative registration receipt is invalid") from None


_MISSING_RECEIPT_VALUE = object()


def _validate_public_receipt_instance(
    value: object,
    *,
    expected_type: type[BaseModel],
    surface: type[BaseModel],
) -> None:
    """Validate direct attributes without recursively serializing a public result."""
    if type(value) is not expected_type:
        raise ValueError("public receipt type")
    raw: dict[str, object] = {}
    for field_name in surface.model_fields:
        field_value = getattr(value, field_name, _MISSING_RECEIPT_VALUE)
        if field_value is _MISSING_RECEIPT_VALUE:
            raise ValueError("public receipt closed shape")
        raw[field_name] = _receipt_json_shape(field_value)
    validate_json_value(surface, raw)


def _receipt_json_shape(value: object) -> object:
    """Make only receipt-native values JSON-shaped without canonical coercion."""
    if isinstance(value, datetime):
        return value.isoformat()
    if type(value) is tuple:
        return [_receipt_json_shape(item) for item in value]
    if type(value) is list:
        return [_receipt_json_shape(item) for item in value]
    if type(value) is dict:
        return {key: _receipt_json_shape(item) for key, item in value.items()}
    return value


class _ImageSourceEvidence:
    """Private checked source records; never returned to an agent-facing caller."""

    __slots__ = ("asset", "analysis", "asset_record", "analysis_record")

    def __init__(
        self,
        *,
        asset: CreativeAssetVersion,
        analysis: AssetAnalysisRecord,
        asset_record: dict[str, Any],
        analysis_record: dict[str, Any],
    ) -> None:
        self.asset = asset
        self.analysis = analysis
        self.asset_record = asset_record
        self.analysis_record = analysis_record


def revalidate_creative_registration_request(value: object) -> CreativeRegistrationRequest:
    """Reparse raw or constructed requests before any local read or append."""
    try:
        if type(value) is CreativeRegistrationRequest:
            raw = value.model_dump(mode="json", warnings="error")
        elif type(value) is dict:
            raw = value
        else:
            raise ValueError("closed creative registration request type")
        if type(raw) is not dict:
            raise ValueError("closed creative registration request shape")
        return validate_json_value(CreativeRegistrationRequest, raw)
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_registration() from None


def analyze_local_image(data: bytes) -> tuple[CreativeAssetVersion, AssetAnalysisRecord]:
    """Decode one caller-provided local image without storing or sending it."""
    analysis = analyze_image(data)
    asset = CreativeAssetVersion.from_image(data, analysis)
    return asset, AssetAnalysisRecord.from_analysis(asset, analysis)


def retain_local_image_analysis(
    directory: Any,
    data: bytes,
    *,
    prepared: tuple[CreativeAssetVersion, AssetAnalysisRecord] | None = None,
) -> CreativeImageAnalysisReceipt:
    """Store an already decoded image and retain its exact asset/analysis records.

    A retry reuses matching durable source records.  It never overwrites a
    nonmatching retained identity or a vault object.
    """
    # Decode from the exact bytes even when the CLI already decoded them for an
    # output-size preflight.  A caller-provided prepared pair is only a
    # consistency witness; it can never replace this fresh intrinsic binding.
    asset, analysis = analyze_local_image(data)
    if prepared is not None:
        prepared_asset = revalidate_creative_asset_version(prepared[0])
        prepared_analysis = revalidate_asset_analysis_record(prepared[1])
        validate_asset_analysis_binding(prepared_asset, prepared_analysis)
        if canonical_bytes(
            prepared_asset.model_dump(mode="json", warnings="error")
        ) != canonical_bytes(asset.model_dump(mode="json", warnings="error")) or canonical_bytes(
            prepared_analysis.model_dump(mode="json", warnings="error")
        ) != canonical_bytes(analysis.model_dump(mode="json", warnings="error")):
            raise AdhelmError(
                "CREATIVE_ANALYSIS_CHANGED",
                "Local image analysis changed before retention.",
                exit_code=3,
                next_action="Repeat the local analysis before deliberate retention.",
            )
    ledger = Ledger(directory)
    checkpoint = ledger.checkpoint()
    creative_records = checkpoint["records_by_stream"]["creative"]
    asset_record = _matching_creative_record(
        creative_records,
        record_kind="CREATIVE_ASSET_VERSION",
        payload=asset.model_dump(mode="json", warnings="error"),
    )
    analysis_record = _matching_creative_record(
        creative_records,
        record_kind="CREATIVE_ASSET_ANALYSIS",
        payload=analysis.model_dump(mode="json", warnings="error"),
    )
    # The protected vault write comes only after local decoding and caller-side
    # output preflight.  ``store_image`` checks existing objects before reuse.
    store_image(directory, data, asset.content_digest)
    if asset_record is None:
        asset_record = ledger.append(
            "creative", "CREATIVE_ASSET_VERSION", asset.model_dump(mode="json", warnings="error")
        )
    if analysis_record is None:
        analysis_record = ledger.append(
            "creative",
            "CREATIVE_ASSET_ANALYSIS",
            analysis.model_dump(mode="json", warnings="error"),
        )
    _require_exact_record(asset_record, "CREATIVE_ASSET_VERSION", asset)
    _require_exact_record(analysis_record, "CREATIVE_ASSET_ANALYSIS", analysis)
    return _analysis_receipt(
        asset,
        analysis,
        asset_record_digest=asset_record["record_digest"],
        analysis_record_digest=analysis_record["record_digest"],
        persisted=True,
    )


def ephemeral_local_image_analysis(data: bytes) -> CreativeImageAnalysisReceipt:
    """Return a bounded local-only analysis receipt without creating directories."""
    asset, analysis = analyze_local_image(data)
    return _analysis_receipt(
        asset,
        analysis,
        asset_record_digest=None,
        analysis_record_digest=None,
        persisted=False,
    )


def creative_image_analysis_receipt_preview(
    asset: CreativeAssetVersion, analysis: AssetAnalysisRecord, *, persisted: bool
) -> dict[str, Any]:
    """Return exact-size safe output data before any vault or ledger effect."""
    checked_asset = revalidate_creative_asset_version(asset)
    checked_analysis = revalidate_asset_analysis_record(analysis)
    validate_asset_analysis_binding(checked_asset, checked_analysis)
    return _analysis_receipt(
        checked_asset,
        checked_analysis,
        asset_record_digest="0" * 64 if persisted else None,
        analysis_record_digest="0" * 64 if persisted else None,
        persisted=persisted,
    ).model_dump(mode="json", warnings="error")


def prepare_creative_registration(
    directory: Any, request: CreativeRegistrationRequest | dict[str, Any]
) -> CreativeRegistrationClosure | CreativeTextRegistrationClosure:
    """Build a fixed graph after validating retained source evidence.

    This function has no provider or credential surface.  Image source bytes are
    read through the hardened local vault and freshly decoded before a closure
    can be constructed.
    """
    checked = revalidate_creative_registration_request(request)
    ledger = Ledger(directory)
    checkpoint = ledger.checkpoint()
    creative_records = checkpoint["records_by_stream"]["creative"]
    existing_family = _existing_family(creative_records, checked.creative_family_id)
    if isinstance(checked.source, IdentityImageCreativeSource):
        source = _load_image_source(
            directory,
            creative_records,
            asset_record_digest=checked.source.asset_record_digest,
            analysis_record_digest=checked.source.asset_analysis_record_digest,
        )
        version_created_at = checked.created_at
        family_created_at = (
            existing_family.created_at if existing_family is not None else version_created_at
        )
        return build_creative_registration_closure(
            CreativeIdentityRegistrationInput(
                creative_family_id=checked.creative_family_id,
                family_label=checked.family_label,
                provider_format=checked.provider_format,
                locale=checked.locale,
                destination_ref=checked.destination_ref,
                rendition_role="IDENTITY_IMAGE",
                components=checked.components,
                message=checked.message,
                provenance="CUSTOMER_LOCAL",
                family_created_at=family_created_at,
                version_created_at=version_created_at,
            ),
            source_asset_record_digest=checked.source.asset_record_digest,
            source_analysis_record_digest=checked.source.asset_analysis_record_digest,
            asset=source.asset,
            asset_analysis=source.analysis,
            existing_family=existing_family,
        )
    family_created_at = (
        existing_family.created_at if existing_family is not None else checked.created_at
    )
    return build_text_creative_registration_closure(
        CreativeTextRegistrationInput(
            creative_family_id=checked.creative_family_id,
            family_label=checked.family_label,
            provider_format=checked.provider_format,
            locale=checked.locale,
            destination_ref=checked.destination_ref,
            components=checked.components,
            message=checked.message,
            provenance="CUSTOMER_LOCAL",
            family_created_at=family_created_at,
            version_created_at=checked.created_at,
        ),
        existing_family=existing_family,
    )


def register_creative_request(
    directory: Any, request: CreativeRegistrationRequest | dict[str, Any]
) -> CreativeRegistrationReceipt:
    """Append a closed local graph once and return a redacted local-only receipt."""
    closure = prepare_creative_registration(directory, request)
    ledger = Ledger(directory)
    try:
        result = ledger.append_creative_registration(closure)
    except AdhelmError:
        # The fixed Ledger operation distinguishes pre-append source/conflict
        # failures from its own no-repeat post-durable-append diagnostic.
        raise
    except (OSError, OverflowError, RecursionError, TypeError, ValueError):
        raise _unverified_registration() from None
    try:
        checkpoint = ledger.checkpoint()
        return _receipt_from_registration(closure, result, checkpoint)
    except (AdhelmError, OSError, OverflowError, RecursionError, TypeError, ValueError):
        # A post-append readback failure can follow a durable child record.
        # Never classify that as invalid input or invite a blind retry.
        raise _unverified_registration() from None


def creative_registration_receipt_preview() -> dict[str, Any]:
    """Maximum fixed-shape receipt used to prove output fit before retention."""
    return CreativeRegistrationReceipt(
        registration_kind="IDENTITY_IMAGE",
        registration_recovery_status="ALREADY_REGISTERED",
        record_digest="0" * 64,
        creative_version_id="creative:sha256:" + "0" * 64,
        version_created_at=datetime(2000, 1, 1, 0, 0, 0, 999_999, tzinfo=UTC),
        rendition_id="rendition:sha256:" + "0" * 64,
        fingerprint_id="fingerprint:sha256:" + "0" * 64,
    ).model_dump(mode="json", warnings="error")


def creative_catalog_entries(
    directory: Any, *, cursor: int, limit: int
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Build redacted catalog rows from this directory's verified checkpoint."""
    if cursor < 0 or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_PAGE", "Expected limit 1–500 and nonnegative cursor.", exit_code=2
        )
    records = _creative_checkpoint_records(Ledger(directory).checkpoint())
    rows = [
        _safe_version_data(record, records)
        for record in records
        if record["record_kind"] == "CREATIVE_VERSION"
    ]
    page = rows[cursor : cursor + limit]
    next_cursor = str(cursor + limit) if cursor + limit < len(rows) else None
    return page, len(rows), next_cursor


def creative_show_data(directory: Any, record_digest: str) -> dict[str, Any]:
    """Build one redacted historical creative-version review from local history."""
    if re.fullmatch(r"[a-f0-9]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_RECORD_DIGEST", "Creative record digest is invalid.", exit_code=2
        )
    records = _creative_checkpoint_records(Ledger(directory).checkpoint())
    matches = [
        record
        for record in records
        if record["record_kind"] == "CREATIVE_VERSION" and record["record_digest"] == record_digest
    ]
    if len(matches) != 1:
        raise AdhelmError(
            "CREATIVE_RECORD_NOT_FOUND",
            "The selected retained creative version is unavailable at the verified checkpoint.",
            exit_code=3,
        )
    return _safe_version_data(matches[0], records)


def _creative_checkpoint_records(checkpoint: object) -> list[dict[str, Any]]:
    try:
        if type(checkpoint) is not dict:
            raise ValueError("checkpoint shape")
        raw = cast(dict[str, Any], checkpoint)
        if raw.get("status") != "VERIFIED_CHECKPOINT":
            raise ValueError("checkpoint status")
        by_stream = raw.get("records_by_stream")
        if type(by_stream) is not dict or type(by_stream.get("creative")) is not list:
            raise ValueError("creative checkpoint records")
        records = cast(list[dict[str, Any]], by_stream["creative"])
        for record in records:
            if type(record) is not dict:
                raise ValueError("creative record shape")
        return records
    except (TypeError, ValueError):
        raise AdhelmError(
            "CREATIVE_HISTORY_UNVERIFIED",
            "Local creative history could not be verified for review.",
            exit_code=3,
        ) from None


def _safe_version_data(record: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        digest_value = record.get("record_digest")
        if type(digest_value) is not str or re.fullmatch(r"[a-f0-9]{64}", digest_value) is None:
            raise ValueError("record digest")
        version = revalidate_creative_payload("CREATIVE_VERSION", record.get("payload"))
        # The explicit cast stays inside this private conversion after the
        # closed record-kind revalidator has selected the model.
        version_data = cast(Any, version)
        if type(record.get("recorded_at")) is not str:
            raise ValueError("recorded time")
        expected_renditions = {item.rendition_id for item in version_data.rendition_refs}
        final_fingerprint_renditions = {
            item["payload"].get("rendition_id")
            for item in records
            if item.get("record_kind") == "CREATIVE_FINGERPRINT"
            and type(item.get("payload")) is dict
            and item["payload"].get("creative_version_id") == version_data.creative_version_id
            and item["payload"].get("analysis_surface") == "FINAL_RENDITION"
            and type(item["payload"].get("rendition_id")) is str
        }
        if version_data.surface_kind == "MEDIA":
            if expected_renditions <= final_fingerprint_renditions:
                final_evidence = "FINAL_RENDITION_EVIDENCE_RETAINED"
            elif final_fingerprint_renditions:
                final_evidence = "FINAL_RENDITION_EVIDENCE_PARTIAL"
            else:
                final_evidence = "STAGED_FINAL_RENDITION_EVIDENCE_MISSING"
        else:
            final_evidence = "NOT_APPLICABLE_TEXT_ONLY"
        return {
            "record_digest": digest_value,
            "creative_version_id": version_data.creative_version_id,
            "serving_manifest_digest": version_data.serving_manifest_digest,
            "surface_kind": version_data.surface_kind,
            "provider_format_status": "DECLARED_NOT_PROVIDER_VALIDATED",
            "locale_status": "DECLARED_NOT_PROVIDER_VALIDATED",
            "component_count": len(version_data.components),
            "rendition_count": len(version_data.rendition_refs),
            "message_metadata_present": version_data.message is not None,
            "created_at": version_data.created_at.isoformat().replace("+00:00", "Z"),
            "recorded_at": record["recorded_at"],
            "final_rendition_evidence": final_evidence,
            "historical_evidence_status": "LOCAL_RETAINED_HISTORY",
            "remote_mutations": False,
            "execution_authority": False,
            "deployment_status": "NOT_EVALUATED_BY_THIS_VIEW",
            "provider_currentness": "NOT_EVALUATED",
        }
    except AdhelmError:
        raise AdhelmError(
            "CREATIVE_HISTORY_UNVERIFIED",
            "Local creative history could not be verified for review.",
            exit_code=3,
        ) from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise AdhelmError(
            "CREATIVE_HISTORY_UNVERIFIED",
            "Local creative history could not be verified for review.",
            exit_code=3,
        ) from None


def _analysis_receipt(
    asset: CreativeAssetVersion,
    analysis: AssetAnalysisRecord,
    *,
    asset_record_digest: str | None,
    analysis_record_digest: str | None,
    persisted: bool,
) -> CreativeImageAnalysisReceipt:
    return CreativeImageAnalysisReceipt(
        asset_version_id=asset.asset_version_id,
        asset_analysis_id=analysis.asset_analysis_id,
        asset_content_digest=asset.content_digest,
        mime_type=analysis.mime_type,
        width_px=analysis.width_px,
        height_px=analysis.height_px,
        aspect_ratio=analysis.aspect_ratio,
        palette_color_count=len(analysis.palette),
        observation_persisted=persisted,
        asset_record_digest=asset_record_digest,
        asset_analysis_record_digest=analysis_record_digest,
        asset_availability="AVAILABLE" if persisted else "NOT_RETAINED_EPHEMERAL",
    )


def _load_image_source(
    directory: Any,
    records: list[dict[str, Any]],
    *,
    asset_record_digest: str,
    analysis_record_digest: str,
) -> _ImageSourceEvidence:
    asset_record = _record_by_digest(records, asset_record_digest, "CREATIVE_ASSET_VERSION")
    analysis_record = _record_by_digest(records, analysis_record_digest, "CREATIVE_ASSET_ANALYSIS")
    asset = revalidate_creative_asset_version(asset_record["payload"])
    analysis = revalidate_asset_analysis_record(analysis_record["payload"])
    validate_asset_analysis_binding(asset, analysis)
    # ``read_image`` proves the selected blob currently hashes to the retained
    # content digest.  Reconstruct both records from fresh decoder output so a
    # retained source cannot be paired with later changed bytes or analyzer data.
    data = read_image(directory, asset.content_digest)
    fresh_asset, fresh_analysis = analyze_local_image(data)
    if canonical_bytes(fresh_asset.model_dump(mode="json", warnings="error")) != canonical_bytes(
        asset.model_dump(mode="json", warnings="error")
    ) or canonical_bytes(
        fresh_analysis.model_dump(mode="json", warnings="error")
    ) != canonical_bytes(analysis.model_dump(mode="json", warnings="error")):
        raise AdhelmError(
            "CREATIVE_REGISTRATION_SOURCE_CHANGED",
            "The selected local image no longer matches its retained analysis.",
            exit_code=3,
            next_action="Analyze the current local image again before a deliberate registration.",
        )
    return _ImageSourceEvidence(
        asset=asset,
        analysis=analysis,
        asset_record=asset_record,
        analysis_record=analysis_record,
    )


def _existing_family(
    records: list[dict[str, Any]], creative_family_id: str
) -> CreativeFamilyRecord | None:
    matches = [
        record
        for record in records
        if record["record_kind"] == "CREATIVE_FAMILY"
        and record["payload"].get("creative_family_id") == creative_family_id
    ]
    if not matches:
        return None
    if len(matches) != 1:
        # Ledger graph validation permits exact duplicate records, but an
        # existing family must have one immutable payload.  Retain the last
        # exact instance after confirming every duplicate is identical.
        payloads = {canonical_bytes(record["payload"]) for record in matches}
        if len(payloads) != 1:
            raise AdhelmError(
                "CREATIVE_REGISTRATION_CONFLICT",
                "The requested creative family has conflicting retained facts.",
                exit_code=3,
            )
    try:
        return cast(
            CreativeFamilyRecord,
            revalidate_creative_payload("CREATIVE_FAMILY", matches[-1]["payload"]),
        )
    except AdhelmError:
        raise AdhelmError(
            "CREATIVE_REGISTRATION_CONFLICT",
            "The requested creative family cannot be used for local registration.",
            exit_code=3,
        ) from None


def _record_by_digest(
    records: list[dict[str, Any]], record_digest: str, record_kind: str
) -> dict[str, Any]:
    matches = [
        record
        for record in records
        if record["record_digest"] == record_digest and record["record_kind"] == record_kind
    ]
    if len(matches) != 1:
        raise AdhelmError(
            "CREATIVE_REGISTRATION_SOURCE_REQUIRED",
            "The selected retained creative source is unavailable.",
            exit_code=3,
            next_action="Analyze and retain the local image before registering it.",
        )
    return matches[0]


def _matching_creative_record(
    records: list[dict[str, Any]], *, record_kind: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    matches = [
        record
        for record in records
        if record["record_kind"] == record_kind
        and canonical_bytes(record["payload"]) == canonical_bytes(payload)
    ]
    return matches[-1] if matches else None


def _require_exact_record(record: object, kind: str, expected: BaseModel) -> None:
    if type(record) is not dict:
        raise _unverified_registration()
    raw = cast(dict[str, Any], record)
    if (
        raw.get("record_kind") != kind
        or type(raw.get("record_digest")) is not str
        or canonical_bytes(raw.get("payload"))
        != canonical_bytes(expected.model_dump(mode="json", warnings="error"))
    ):
        raise _unverified_registration()


def _recorded_at(record: dict[str, Any]) -> datetime:
    raw = record.get("recorded_at")
    if type(raw) is not str:
        raise _invalid_registration()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid_registration() from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise _invalid_registration()
    return value.astimezone(UTC)


def _verify_retained_registration(
    closure: CreativeRegistrationClosure | CreativeTextRegistrationClosure,
    checkpoint: object,
    *,
    returned_version_record_digest: object,
) -> tuple[str, str]:
    """Require the exact closed graph in a fresh verified checkpoint."""
    if type(returned_version_record_digest) is not str:
        raise ValueError("returned version record digest")
    raw_checkpoint = cast(dict[str, Any], checkpoint)
    checkpoint_digest = raw_checkpoint.get("checkpoint_digest")
    if (
        type(checkpoint_digest) is not str
        or re.fullmatch(r"[a-f0-9]{64}", checkpoint_digest) is None
    ):
        raise ValueError("checkpoint digest")
    records = _creative_checkpoint_records(checkpoint)
    expected_records = closure_records(closure)
    version_record_digest: str | None = None
    for kind, model in expected_records:
        payload = model.model_dump(mode="json", warnings="error")
        matches = [
            record
            for record in records
            if record["record_kind"] == kind
            and canonical_bytes(record.get("payload")) == canonical_bytes(payload)
        ]
        if not matches:
            raise ValueError("retained registration graph")
        if kind == "CREATIVE_VERSION":
            version_matches = [
                record
                for record in matches
                if record.get("record_digest") == returned_version_record_digest
            ]
            if len(version_matches) != 1:
                raise ValueError("retained version record binding")
            version_record_digest = returned_version_record_digest
    if isinstance(closure, CreativeRegistrationClosure):
        _require_exact_source_checkpoint_record(
            records,
            record_digest=closure.source_asset_record_digest,
            record_kind="CREATIVE_ASSET_VERSION",
            payload=closure.asset.model_dump(mode="json", warnings="error"),
        )
        _require_exact_source_checkpoint_record(
            records,
            record_digest=closure.source_analysis_record_digest,
            record_kind="CREATIVE_ASSET_ANALYSIS",
            payload=closure.asset_analysis.model_dump(mode="json", warnings="error"),
        )
    if version_record_digest is None:
        raise ValueError("missing retained version record")
    return checkpoint_digest, version_record_digest


def _require_exact_source_checkpoint_record(
    records: list[dict[str, Any]],
    *,
    record_digest: str,
    record_kind: str,
    payload: dict[str, Any],
) -> None:
    matches = [
        record
        for record in records
        if record.get("record_digest") == record_digest
        and record.get("record_kind") == record_kind
        and canonical_bytes(record.get("payload")) == canonical_bytes(payload)
    ]
    if len(matches) != 1:
        raise ValueError("retained source record binding")


def _receipt_from_registration(
    closure: CreativeRegistrationClosure | CreativeTextRegistrationClosure,
    result: object,
    checkpoint: object,
) -> CreativeRegistrationReceipt:
    try:
        if type(result) is not dict:
            raise ValueError("registration result shape")
        raw = cast(dict[str, Any], result)
        status = raw.get("status")
        record_digests = raw.get("record_digests")
        if (
            status not in {"REGISTERED", "RECOVERED_STAGING", "ALREADY_REGISTERED"}
            or type(record_digests) is not dict
            or type(record_digests.get("CREATIVE_VERSION")) is not str
            or raw.get("creative_version_id") != closure.version.creative_version_id
        ):
            raise ValueError("registration result binding")
        _, actual_version_record_digest = _verify_retained_registration(
            closure,
            checkpoint,
            returned_version_record_digest=record_digests["CREATIVE_VERSION"],
        )
        if isinstance(closure, CreativeRegistrationClosure):
            if (
                raw.get("rendition_id") != closure.rendition.rendition_id
                or raw.get("fingerprint_id") != closure.fingerprint.fingerprint_id
                or raw.get("source_asset_record_digest") != closure.source_asset_record_digest
                or raw.get("source_analysis_record_digest") != closure.source_analysis_record_digest
            ):
                raise ValueError("image registration result binding")
            receipt = CreativeRegistrationReceipt(
                registration_kind="IDENTITY_IMAGE",
                registration_recovery_status=status,
                record_digest=actual_version_record_digest,
                creative_version_id=closure.version.creative_version_id,
                version_created_at=closure.version.created_at,
                rendition_id=closure.rendition.rendition_id,
                fingerprint_id=closure.fingerprint.fingerprint_id,
            )
        else:
            receipt = CreativeRegistrationReceipt(
                registration_kind="TEXT_ONLY",
                registration_recovery_status=status,
                record_digest=actual_version_record_digest,
                creative_version_id=closure.version.creative_version_id,
                version_created_at=closure.version.created_at,
            )
        return _revalidate_receipt(receipt)
    except AdhelmError:
        raise
    except (
        AttributeError,
        KeyError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise _unverified_registration() from None


def _revalidate_receipt(value: CreativeRegistrationReceipt) -> CreativeRegistrationReceipt:
    try:
        return validate_json_value(
            CreativeRegistrationReceipt, value.model_dump(mode="json", warnings="error")
        )
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError):
        raise _unverified_registration() from None


def _invalid_registration() -> AdhelmError:
    return AdhelmError(
        "INVALID_CREATIVE_REGISTRATION",
        "Creative registration data is invalid.",
        exit_code=2,
    )


def _unverified_registration() -> AdhelmError:
    return AdhelmError(
        "CREATIVE_REGISTRATION_UNVERIFIED",
        "Local creative registration could not be verified after retention.",
        exit_code=6,
        next_action="Inspect creative history before any deliberate retry.",
    )
