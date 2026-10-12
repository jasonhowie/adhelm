"""Local, deterministic creative identity and image metadata extraction.

The module intentionally does not fetch remote media, run OCR/transcription, or
infer people or protected traits.  Unsupported fields remain explicit states.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, Literal, NamedTuple, NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.privacy import validate_data
from adhelm.strict_json import validate_json_value

ANALYZER_NAME: Literal["adhelm-local-image"] = "adhelm-local-image"
ANALYZER_VERSION: Literal["1"] = "1"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 10_000_000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        """Do not let a ``Literal`` normalize bools or integer-looking floats."""
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def privacy_screen(self) -> _Strict:
        validate_data(self.model_dump(mode="json"))
        return self


class ServingRendition(_Strict):
    rendition_id: str = Field(min_length=1, max_length=200)
    ordinal: int = Field(ge=0, le=999)
    role: str = Field(min_length=1, max_length=80)


class CreativeComponent(_Strict):
    component_id: str = Field(min_length=1, max_length=200)
    component_role: Literal[
        "PRIMARY_TEXT",
        "HEADLINE",
        "LONG_HEADLINE",
        "DESCRIPTION",
        "ON_VISUAL_TEXT",
        "CAPTION",
        "VOICEOVER",
        "CTA_BUTTON",
        "CTA_VISUAL",
        "CTA_SPOKEN",
        "PATH_TEXT",
    ]
    ordinal: int = Field(ge=0, le=999)
    exact_text: str = Field(min_length=1, max_length=10_000)
    normalized_text: str = Field(min_length=1, max_length=10_000)
    language: str = Field(min_length=1, max_length=32)
    provider_slot: str = Field(min_length=1, max_length=120)
    pinned_position: str | None = Field(default=None, max_length=120)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    normalized_cta_intent: (
        Literal[
            "SIGN_UP",
            "BOOK_DEMO",
            "CONTACT_SALES",
            "LEARN_MORE",
            "DOWNLOAD",
            "INSTALL",
            "BUY",
            "OTHER",
        ]
        | None
    ) = None
    cta_destination_ref: str | None = Field(default=None, max_length=200)
    provenance: Literal["DECLARED", "PROVIDER_READ", "LOCAL_EXTRACTED", "HUMAN_ANNOTATED"]
    confidence_basis_points: int = Field(ge=0, le=10_000)
    extraction_status: Literal[
        "DECLARED", "PROVIDER_READ", "LOCAL_EXTRACTED", "NOT_SUPPORTED_BY_ANALYZER"
    ]

    @model_validator(mode="after")
    def component_interval_is_valid(self) -> CreativeComponent:
        if self.end_ms is not None and self.start_ms is None:
            raise ValueError("A component end time requires a start time")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("A component end time cannot precede its start time")
        if self.component_role.startswith("CTA_") and self.normalized_cta_intent is None:
            raise ValueError("CTA components require a normalized CTA intent")
        return self


class ServingCreativeManifest(_Strict):
    """The complete serving surface and *only* the creative-version digest domain."""

    schema_version: Literal[1] = 1
    provider_format: str = Field(min_length=1, max_length=120)
    locale: str = Field(min_length=1, max_length=32)
    destination_ref: str = Field(min_length=1, max_length=200)
    surface_kind: Literal["MEDIA", "TEXT_ONLY"] = "MEDIA"
    rendition_refs: tuple[ServingRendition, ...] = Field(max_length=100)
    components: tuple[CreativeComponent, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def ordering_is_complete(self) -> ServingCreativeManifest:
        renditions = [item.ordinal for item in self.rendition_refs]
        components = [item.ordinal for item in self.components]
        if self.surface_kind == "MEDIA" and not renditions:
            raise ValueError("Media serving formats require at least one rendition")
        if self.surface_kind == "TEXT_ONLY" and renditions:
            raise ValueError("Text-only serving formats cannot name visual renditions")
        if renditions != list(range(len(renditions))) or len(set(renditions)) != len(renditions):
            raise ValueError("Rendition ordinals must be contiguous and unique")
        if components != list(range(len(components))) or len(set(components)) != len(components):
            raise ValueError("Component ordinals must be contiguous and unique")
        return self

    @property
    def serving_manifest_digest(self) -> str:
        return digest(self.semantic_projection())

    def semantic_projection(self) -> dict[str, Any]:
        """Only facts that change the exact rendered/served creative surface."""
        return {
            "schema_version": self.schema_version,
            "provider_format": self.provider_format,
            "locale": self.locale,
            "destination_ref": self.destination_ref,
            "surface_kind": self.surface_kind,
            "rendition_refs": [
                {"rendition_id": item.rendition_id, "ordinal": item.ordinal, "role": item.role}
                for item in self.rendition_refs
            ],
            "components": [
                {
                    "component_role": item.component_role,
                    "ordinal": item.ordinal,
                    "exact_text": item.exact_text,
                    "provider_slot": item.provider_slot,
                    "pinned_position": item.pinned_position,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "normalized_cta_intent": item.normalized_cta_intent,
                    "cta_destination_ref": item.cta_destination_ref,
                }
                for item in self.components
            ],
        }

    @property
    def creative_version_id(self) -> str:
        return f"creative:sha256:{self.serving_manifest_digest}"


class CreativeMessage(_Strict):
    hook: (
        Literal[
            "accusation",
            "diagnosis",
            "proof",
            "curiosity",
            "offer",
            "social_proof",
            "demonstration",
            "comparison",
            "other",
        ]
        | None
    ) = None
    posture: Literal["operator", "creative", "customer", "expert", "product", "other"] | None = None
    objection: (
        Literal[
            "price",
            "complexity",
            "existing_system",
            "trust",
            "switching_cost",
            "urgency",
            "none",
            "other",
        ]
        | None
    ) = None
    specificity: Literal["named_number", "concrete_example", "general_claim"] | None = None
    offer: (
        Literal[
            "free_trial",
            "free_signup",
            "demo",
            "assessment",
            "content",
            "contact_sales",
            "learn_more",
            "none",
            "other",
        ]
        | None
    ) = None
    claim: str | None = Field(default=None, max_length=2_000)
    mechanism: str | None = Field(default=None, max_length=2_000)
    audience_frame: str | None = Field(default=None, max_length=500)
    tone: tuple[
        Literal["direct", "technical", "friendly", "urgent", "authoritative", "playful", "other"],
        ...,
    ] = ()


class CreativeVersion(_Strict):
    """A catalog record whose annotations intentionally do not alter serving identity."""

    schema_version: Literal[1] = 1
    creative_family_id: str = Field(min_length=1, max_length=200)
    manifest: ServingCreativeManifest
    message: CreativeMessage | None = None
    analyzer_refs: tuple[str, ...] = ()
    provenance: Literal[
        "CUSTOMER_LOCAL", "PROVIDER_READ", "GENERATED_LOCAL", "HUMAN_ANNOTATED", "IMPORTED_MANIFEST"
    ]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must have a timezone")
        return value.astimezone(UTC)

    @property
    def creative_version_id(self) -> str:
        return self.manifest.creative_version_id

    def to_ledger_payload(self) -> dict[str, Any]:
        """Full record; do not pass this to an older lossy creative ledger schema."""
        record = CreativeVersionRecord(
            schema_version=self.schema_version,
            creative_family_id=self.creative_family_id,
            creative_version_id=self.creative_version_id,
            provider_format=self.manifest.provider_format,
            locale=self.manifest.locale,
            destination_ref=self.manifest.destination_ref,
            surface_kind=self.manifest.surface_kind,
            rendition_refs=self.manifest.rendition_refs,
            components=self.manifest.components,
            serving_manifest_schema_version=self.manifest.schema_version,
            serving_manifest_digest=self.manifest.serving_manifest_digest,
            message=self.message,
            analyzer_refs=self.analyzer_refs,
            provenance=self.provenance,
            created_at=self.created_at,
        )
        return revalidate_creative_version_record(record).model_dump(mode="json", warnings="error")

    def append_to_ledger(self, ledger: Any, **envelope: Any) -> dict[str, Any]:
        try:
            result = ledger.append(
                "creative",
                "CREATIVE_VERSION",
                self.to_ledger_payload(),
                **envelope,
            )
            return cast(dict[str, Any], result)
        except AdhelmError as exc:
            if exc.code == "INVALID_LEDGER_PAYLOAD":
                raise AdhelmError(
                    "CREATIVE_LEDGER_SCHEMA_UNSUPPORTED",
                    "The installed creative ledger schema cannot retain the complete serving "
                    "manifest.",
                ) from exc
            raise


class CreativeVersionRecord(_Strict):
    """Strict complete persisted shape for a creative-version ledger record."""

    schema_version: Literal[1] = 1
    creative_family_id: str = Field(min_length=1, max_length=200)
    creative_version_id: str = Field(min_length=1, max_length=200)
    provider_format: str = Field(min_length=1, max_length=120)
    locale: str = Field(min_length=1, max_length=32)
    destination_ref: str = Field(min_length=1, max_length=200)
    surface_kind: Literal["MEDIA", "TEXT_ONLY"] = "MEDIA"
    rendition_refs: tuple[ServingRendition, ...] = Field(max_length=100)
    components: tuple[CreativeComponent, ...] = Field(min_length=1, max_length=200)
    serving_manifest_schema_version: Literal[1] = 1
    serving_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    message: CreativeMessage | None = None
    analyzer_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    provenance: Literal[
        "CUSTOMER_LOCAL", "PROVIDER_READ", "GENERATED_LOCAL", "HUMAN_ANNOTATED", "IMPORTED_MANIFEST"
    ]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="creative version creation time")

    @field_validator("serving_manifest_schema_version", mode="before")
    @classmethod
    def exact_serving_manifest_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("serving_manifest_schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def serving_identity_matches(self) -> CreativeVersionRecord:
        manifest = ServingCreativeManifest(
            schema_version=self.serving_manifest_schema_version,
            provider_format=self.provider_format,
            locale=self.locale,
            destination_ref=self.destination_ref,
            surface_kind=self.surface_kind,
            rendition_refs=self.rendition_refs,
            components=self.components,
        )
        if self.serving_manifest_digest != manifest.serving_manifest_digest:
            raise ValueError(
                "Serving manifest digest does not match the semantic serving projection"
            )
        if self.creative_version_id != manifest.creative_version_id:
            raise ValueError(
                "Creative version identifier does not match the serving manifest digest"
            )
        if len(set(self.analyzer_refs)) != len(self.analyzer_refs):
            raise ValueError("Creative version analyzer references must be unique")
        if self.surface_kind == "TEXT_ONLY" and self.analyzer_refs:
            raise ValueError("Text-only creative versions cannot name image analyzer evidence")
        return self


class CreativeFamilyRecord(_Strict):
    schema_version: Literal[1] = 1
    creative_family_id: str = Field(min_length=1, max_length=200)
    family_label: str = Field(min_length=1, max_length=200)
    created_at: datetime
    provenance: Literal["CUSTOMER_LOCAL", "HUMAN_ANNOTATED", "IMPORTED_MANIFEST"]

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="creative family creation time")


class CreativeAssetVersion(_Strict):
    schema_version: Literal[1] = 1
    asset_version_id: str = Field(pattern=r"^asset:sha256:[a-f0-9]{64}$")
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: Literal["IMAGE"]
    mime_type: str
    byte_size: int = Field(ge=1)
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    aspect_ratio: str
    availability_status: Literal["AVAILABLE"] = "AVAILABLE"

    @model_validator(mode="after")
    def intrinsic_identity_matches(self) -> CreativeAssetVersion:
        if self.aspect_ratio != _aspect_ratio(self.width_px, self.height_px):
            raise ValueError("Asset aspect ratio disagrees with intrinsic dimensions")
        if self.asset_version_id != _asset_version_id(
            self.content_digest, self.mime_type, self.width_px, self.height_px
        ):
            raise ValueError("Asset identifier does not match its intrinsic content identity")
        return self

    @classmethod
    def from_image(cls, data: bytes, analysis: ImageAnalysis) -> CreativeAssetVersion:
        analysis = revalidate_image_analysis(analysis)
        content_digest = hashlib.sha256(data).hexdigest()
        intrinsic = {
            "content_digest": content_digest,
            "mime_type": analysis.mime_type,
            "width_px": analysis.width_px,
            "height_px": analysis.height_px,
        }
        return cls(
            asset_version_id=f"asset:sha256:{digest(intrinsic)}",
            content_digest=content_digest,
            media_type="IMAGE",
            mime_type=analysis.mime_type,
            byte_size=len(data),
            width_px=analysis.width_px,
            height_px=analysis.height_px,
            aspect_ratio=analysis.aspect_ratio,
        )


class IdentityTransformation(_Strict):
    """The only transformation this module currently performs: no transformation."""

    kind: Literal["IDENTITY"] = "IDENTITY"


class CreativeRendition(_Strict):
    schema_version: Literal[1] = 1
    rendition_id: str = Field(pattern=r"^rendition:sha256:[a-f0-9]{64}$")
    source_asset_version_ref: str | None = Field(
        default=None, pattern=r"^asset:sha256:[a-f0-9]{64}$"
    )
    source_relation_status: Literal["PROVEN_SOURCE_ASSET", "UNKNOWN_SOURCE_ASSET"]
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_format: str = Field(min_length=1, max_length=120)
    locale: str = Field(min_length=1, max_length=32)
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    transformation_spec: IdentityTransformation
    transformation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def intrinsic_identity_matches(self) -> CreativeRendition:
        if (
            self.source_relation_status == "PROVEN_SOURCE_ASSET"
            and self.source_asset_version_ref is None
        ) or (
            self.source_relation_status == "UNKNOWN_SOURCE_ASSET"
            and self.source_asset_version_ref is not None
        ):
            raise ValueError("Rendition source relation and source asset reference disagree")
        if self.transformation_digest != digest(self.transformation_spec):
            raise ValueError("Rendition transformation digest does not match its specification")
        if self.rendition_id != _rendition_id(
            source_asset_version_ref=self.source_asset_version_ref,
            content_digest=self.content_digest,
            provider_format=self.provider_format,
            locale=self.locale,
            width_px=self.width_px,
            height_px=self.height_px,
            transformation_digest=self.transformation_digest,
        ):
            raise ValueError("Rendition identifier does not match its intrinsic identity")
        return self

    @classmethod
    def from_asset(
        cls,
        asset: CreativeAssetVersion,
        *,
        provider_format: str,
        locale: str,
        transformation_spec: IdentityTransformation | None = None,
    ) -> CreativeRendition:
        asset = revalidate_creative_asset_version(asset)
        if transformation_spec is None:
            transformation_spec = IdentityTransformation()
        if not isinstance(transformation_spec, IdentityTransformation):
            raise AdhelmError(
                "INVALID_TRANSFORMATION_SPEC",
                "This local analyzer can record only an identity rendition transformation.",
                exit_code=2,
            )
        transformation_digest = digest(transformation_spec)
        return cls(
            rendition_id=_rendition_id(
                source_asset_version_ref=asset.asset_version_id,
                content_digest=asset.content_digest,
                provider_format=provider_format,
                locale=locale,
                width_px=asset.width_px,
                height_px=asset.height_px,
                transformation_digest=transformation_digest,
            ),
            source_asset_version_ref=asset.asset_version_id,
            source_relation_status="PROVEN_SOURCE_ASSET",
            content_digest=asset.content_digest,
            provider_format=provider_format,
            locale=locale,
            width_px=asset.width_px,
            height_px=asset.height_px,
            transformation_spec=transformation_spec,
            transformation_digest=transformation_digest,
        )


class PaletteColor(_Strict):
    hex: str = Field(pattern=r"^#[0-9A-F]{6}$")
    proportion_basis_points: int = Field(ge=0, le=10_000)


class ImageAnalysis(_Strict):
    analyzer_name: Literal["adhelm-local-image"] = ANALYZER_NAME
    analyzer_version: Literal["1"] = ANALYZER_VERSION
    mime_type: Literal[
        "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"
    ]
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    aspect_ratio: str
    palette: tuple[PaletteColor, ...]
    mean_lightness_basis_points: int = Field(ge=0, le=10_000)
    mean_saturation_basis_points: int = Field(ge=0, le=10_000)
    contrast_basis_points: int = Field(ge=0, le=10_000)
    colorfulness_basis_points: int = Field(ge=0, le=10_000)
    extraction_statuses: tuple[Literal["NOT_SUPPORTED_BY_ANALYZER"], ...] = (
        "NOT_SUPPORTED_BY_ANALYZER",
    )

    @model_validator(mode="after")
    def deterministic_image_shape(self) -> ImageAnalysis:
        if self.aspect_ratio != _aspect_ratio(self.width_px, self.height_px):
            raise ValueError("Image analysis aspect ratio disagrees with its dimensions")
        if (
            not self.palette
            or len({item.hex for item in self.palette}) != len(self.palette)
            or tuple(item.hex for item in self.palette)
            != tuple(sorted(item.hex for item in self.palette))
            or sum(item.proportion_basis_points for item in self.palette) != 10_000
        ):
            raise ValueError("Image analysis palette must be nonempty, ordered and complete")
        return self


class AssetAnalysisRecord(_Strict):
    """A predeployment image analysis that names an asset, never a creative version."""

    schema_version: Literal[1] = 1
    asset_analysis_id: str = Field(pattern=r"^asset-analysis:sha256:[a-f0-9]{64}$")
    asset_version_id: str = Field(pattern=r"^asset:sha256:[a-f0-9]{64}$")
    asset_content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    analyzer_name: Literal["adhelm-local-image"]
    analyzer_version: Literal["1"]
    mime_type: Literal[
        "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"
    ]
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    aspect_ratio: str
    palette: tuple[PaletteColor, ...]
    mean_lightness_basis_points: int = Field(ge=0, le=10_000)
    mean_saturation_basis_points: int = Field(ge=0, le=10_000)
    contrast_basis_points: int = Field(ge=0, le=10_000)
    colorfulness_basis_points: int = Field(ge=0, le=10_000)
    extraction_statuses: tuple[Literal["NOT_SUPPORTED_BY_ANALYZER"], ...]
    provenance: Literal["LOCAL_EXTRACTED"] = "LOCAL_EXTRACTED"

    @model_validator(mode="after")
    def intrinsic_identity_matches(self) -> AssetAnalysisRecord:
        analysis = _analysis_from_asset_analysis_record(self)
        if self.asset_analysis_id != _asset_analysis_id(
            self.asset_version_id, self.asset_content_digest, analysis
        ):
            raise ValueError("Asset analysis identifier does not match its analyzed content")
        return self

    @classmethod
    def from_analysis(
        cls, asset: CreativeAssetVersion, analysis: ImageAnalysis
    ) -> AssetAnalysisRecord:
        asset = revalidate_creative_asset_version(asset)
        analysis = revalidate_image_analysis(analysis)
        if (
            asset.mime_type != analysis.mime_type
            or asset.width_px != analysis.width_px
            or asset.height_px != analysis.height_px
        ):
            raise AdhelmError(
                "ASSET_ANALYSIS_MISMATCH",
                "Asset identity does not match the decoded image analysis.",
                exit_code=2,
            )
        identity = {
            "asset_version_id": asset.asset_version_id,
            "asset_content_digest": asset.content_digest,
            **analysis.model_dump(mode="json"),
        }
        return cls(
            asset_analysis_id=f"asset-analysis:sha256:{digest(identity)}",
            asset_version_id=asset.asset_version_id,
            asset_content_digest=asset.content_digest,
            analyzer_name=analysis.analyzer_name,
            analyzer_version=analysis.analyzer_version,
            mime_type=analysis.mime_type,
            width_px=analysis.width_px,
            height_px=analysis.height_px,
            aspect_ratio=analysis.aspect_ratio,
            palette=analysis.palette,
            mean_lightness_basis_points=analysis.mean_lightness_basis_points,
            mean_saturation_basis_points=analysis.mean_saturation_basis_points,
            contrast_basis_points=analysis.contrast_basis_points,
            colorfulness_basis_points=analysis.colorfulness_basis_points,
            extraction_statuses=analysis.extraction_statuses,
        )


class ImageFingerprintRecord(_Strict):
    """Strict persisted fingerprint backed by an actual local ImageAnalysis result."""

    # V2 adds ``mime_type``.  It is part of the historical fingerprint digest,
    # so retaining it is necessary to recompute a newly written record's
    # identity without silently changing an M27 v1 payload.
    schema_version: Literal[2] = 2
    fingerprint_id: str = Field(pattern=r"^fingerprint:sha256:[a-f0-9]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[a-f0-9]{64}$")
    asset_version_id: str = Field(pattern=r"^asset:sha256:[a-f0-9]{64}$")
    rendition_id: str | None = Field(default=None, pattern=r"^rendition:sha256:[a-f0-9]{64}$")
    analysis_surface: Literal["SOURCE_ASSET", "FINAL_RENDITION"]
    analyzer_name: Literal["adhelm-local-image"]
    analyzer_version: Literal["1"]
    mime_type: Literal[
        "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"
    ]
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    aspect_ratio: str
    palette: tuple[PaletteColor, ...]
    mean_lightness_basis_points: int = Field(ge=0, le=10_000)
    mean_saturation_basis_points: int = Field(ge=0, le=10_000)
    contrast_basis_points: int = Field(ge=0, le=10_000)
    colorfulness_basis_points: int = Field(ge=0, le=10_000)
    extraction_statuses: tuple[Literal["NOT_SUPPORTED_BY_ANALYZER"], ...]
    provenance: Literal["LOCAL_EXTRACTED"] = "LOCAL_EXTRACTED"

    @model_validator(mode="after")
    def surface_identity_is_honest(self) -> ImageFingerprintRecord:
        if self.analysis_surface == "SOURCE_ASSET" and self.rendition_id is not None:
            raise ValueError("Source-asset analysis cannot name a rendition")
        if self.analysis_surface != "SOURCE_ASSET" and self.rendition_id is None:
            raise ValueError("Final-rendition analysis requires a rendition")
        analysis = _analysis_from_fingerprint_record(self)
        if self.fingerprint_id != _fingerprint_id(
            analysis,
            creative_version_id=self.creative_version_id,
            asset_version_id=self.asset_version_id,
            rendition_id=self.rendition_id,
            analysis_surface=self.analysis_surface,
        ):
            raise ValueError("Fingerprint identifier does not match its analyzed surface")
        return self

    @classmethod
    def from_analysis(
        cls,
        analysis: ImageAnalysis,
        *,
        creative_version_id: str,
        asset_version_id: str,
        rendition_id: str | None,
        analysis_surface: Literal["SOURCE_ASSET", "FINAL_RENDITION"],
    ) -> ImageFingerprintRecord:
        analysis = revalidate_image_analysis(analysis)
        fingerprint = {
            "creative_version_id": creative_version_id,
            "asset_version_id": asset_version_id,
            "rendition_id": rendition_id,
            "analysis_surface": analysis_surface,
            **analysis.model_dump(mode="json"),
        }
        return cls(
            fingerprint_id=f"fingerprint:sha256:{digest(fingerprint)}",
            creative_version_id=creative_version_id,
            asset_version_id=asset_version_id,
            rendition_id=rendition_id,
            analysis_surface=analysis_surface,
            analyzer_name=analysis.analyzer_name,
            analyzer_version=analysis.analyzer_version,
            mime_type=analysis.mime_type,
            width_px=analysis.width_px,
            height_px=analysis.height_px,
            aspect_ratio=analysis.aspect_ratio,
            palette=analysis.palette,
            mean_lightness_basis_points=analysis.mean_lightness_basis_points,
            mean_saturation_basis_points=analysis.mean_saturation_basis_points,
            contrast_basis_points=analysis.contrast_basis_points,
            colorfulness_basis_points=analysis.colorfulness_basis_points,
            extraction_statuses=analysis.extraction_statuses,
        )


class _LegacyImageFingerprintRecordV1(_Strict):
    """Read-only M27 fingerprint shape, retained without rewriting its bytes."""

    schema_version: Literal[1] = 1
    fingerprint_id: str = Field(min_length=1, max_length=200)
    creative_version_id: str = Field(min_length=1, max_length=200)
    asset_version_id: str = Field(min_length=1, max_length=200)
    rendition_id: str | None = Field(default=None, max_length=200)
    analysis_surface: Literal["SOURCE_ASSET", "FINAL_RENDITION", "PROVIDER_PREVIEW"]
    analyzer_name: Literal["adhelm-local-image"]
    analyzer_version: Literal["1"]
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    aspect_ratio: str
    palette: tuple[PaletteColor, ...]
    mean_lightness_basis_points: int = Field(ge=0, le=10_000)
    mean_saturation_basis_points: int = Field(ge=0, le=10_000)
    contrast_basis_points: int = Field(ge=0, le=10_000)
    colorfulness_basis_points: int = Field(ge=0, le=10_000)
    extraction_statuses: tuple[Literal["NOT_SUPPORTED_BY_ANALYZER"], ...]
    provenance: Literal["LOCAL_EXTRACTED"] = "LOCAL_EXTRACTED"

    @model_validator(mode="after")
    def surface_shape_is_honest(self) -> _LegacyImageFingerprintRecordV1:
        if self.analysis_surface == "SOURCE_ASSET" and self.rendition_id is not None:
            raise ValueError("Source-asset analysis cannot name a rendition")
        if self.analysis_surface != "SOURCE_ASSET" and self.rendition_id is None:
            raise ValueError("Final/preview analysis requires a rendition")
        return self


class DecodedImage(NamedTuple):
    """One bounded local decode, shared by every deterministic local extractor.

    ``exif_orientation`` is recorded, never applied.  Rotating pixels would make
    the analysis depend on metadata rather than on the stored bytes; callers that
    describe spatial structure disclose the orientation they measured instead.
    """

    mime_type: Literal[
        "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"
    ]
    width_px: int
    height_px: int
    pixels: list[tuple[int, int, int]]
    exif_orientation: int | None
    exif_orientation_status: Literal["EXTRACTED", "NOT_PRESENT", "INVALID"]


def decode_image(
    data: bytes, *, maximum_bytes: int = MAX_IMAGE_BYTES, maximum_pixels: int = MAX_IMAGE_PIXELS
) -> DecodedImage:
    """Decode bounded local raster bytes exactly once, with typed refusals only."""
    if len(data) == 0 or len(data) > maximum_bytes:
        raise AdhelmError(
            "CREATIVE_MEDIA_TOO_LARGE",
            "Image bytes exceed the configured analysis bound.",
            exit_code=2,
        )
    try:
        from PIL import Image
    except ImportError as exc:
        raise AdhelmError(
            "LOCAL_IMAGE_ANALYZER_UNAVAILABLE", "Pillow is required for local image analysis."
        ) from exc
    try:
        with Image.open(BytesIO(data)) as verified:
            verified.verify()
        with Image.open(BytesIO(data)) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise AdhelmError(
                    "UNSUPPORTED_ANIMATED_IMAGE",
                    "Animated or multi-frame images require a separate frame-analysis contract.",
                    exit_code=2,
                )
            mime_type = _decoded_mime_type(image.format)
            width, height = image.size
            if width < 1 or height < 1 or width * height > maximum_pixels:
                raise AdhelmError(
                    "CREATIVE_IMAGE_TOO_LARGE",
                    "Image pixel dimensions exceed the analysis bound.",
                    exit_code=2,
                )
            orientation, orientation_status = _exif_orientation(image)
            rgb = image.convert("RGB")
            pixels = cast(list[tuple[int, int, int]], list(rgb.get_flattened_data()))
    except AdhelmError:
        # A bound this function decided is already typed and stays exact.
        raise
    except MemoryError:
        # Resource exhaustion is the host's condition, not a claim about the file.
        raise
    except Image.DecompressionBombError as exc:
        raise AdhelmError(
            "CREATIVE_IMAGE_TOO_LARGE",
            "Image pixel dimensions exceed the analysis bound.",
            exit_code=2,
        ) from exc
    except Exception as exc:
        # Every remaining decode failure is one class of event: these bytes are
        # not a supported image.  Pillow's plugins signal that with whatever
        # exception the format's parser happens to use, including a bare
        # ``SyntaxError`` for a broken PNG chunk, so naming individual classes
        # leaves untyped escapes for the next corrupt file.
        raise AdhelmError(
            "INVALID_CREATIVE_IMAGE", "Local creative bytes are not a supported image.", exit_code=2
        ) from exc
    if not pixels:
        raise AdhelmError("INVALID_CREATIVE_IMAGE", "Image has no readable pixels.", exit_code=2)
    return DecodedImage(mime_type, width, height, pixels, orientation, orientation_status)


def _exif_orientation(
    image: Any,
) -> tuple[int | None, Literal["EXTRACTED", "NOT_PRESENT", "INVALID"]]:
    """Read the bounded EXIF orientation tag; untrusted metadata never raises.

    A tag outside the defined 1-8 range is disclosed as ``INVALID`` rather than
    reported as absent: the file did state an orientation, and it is not one.
    """
    try:
        raw = image.getexif().get(274)
    except Exception:  # noqa: BLE001 - third-party EXIF parsing is untrusted input
        return None, "NOT_PRESENT"
    if raw is None:
        return None, "NOT_PRESENT"
    if type(raw) is int and 1 <= raw <= 8:
        return raw, "EXTRACTED"
    return None, "INVALID"


def analysis_from_decoded_image(decoded: DecodedImage) -> ImageAnalysis:
    """Derive the exact deterministic analysis from an already-decoded image."""
    palette = _palette(decoded.pixels)
    lightness, saturation, contrast, colorfulness = _metrics(decoded.pixels)
    divisor = math.gcd(decoded.width_px, decoded.height_px)
    return ImageAnalysis(
        mime_type=decoded.mime_type,
        width_px=decoded.width_px,
        height_px=decoded.height_px,
        aspect_ratio=f"{decoded.width_px // divisor}:{decoded.height_px // divisor}",
        palette=tuple(palette),
        mean_lightness_basis_points=lightness,
        mean_saturation_basis_points=saturation,
        contrast_basis_points=contrast,
        colorfulness_basis_points=colorfulness,
    )


def analyze_image(
    data: bytes, *, maximum_bytes: int = MAX_IMAGE_BYTES, maximum_pixels: int = MAX_IMAGE_PIXELS
) -> ImageAnalysis:
    """Analyze a bounded local raster image with deterministic quantization."""
    return analysis_from_decoded_image(
        decode_image(data, maximum_bytes=maximum_bytes, maximum_pixels=maximum_pixels)
    )


def _decoded_mime_type(
    image_format: str | None,
) -> Literal["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"]:
    formats: dict[
        str,
        Literal["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"],
    ] = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
        "GIF": "image/gif",
        "BMP": "image/bmp",
        "TIFF": "image/tiff",
    }
    if image_format not in formats:
        raise AdhelmError(
            "UNSUPPORTED_IMAGE_FORMAT", "Decoded image format is not supported.", exit_code=2
        )
    return formats[image_format]


def _palette(pixels: list[tuple[int, int, int]]) -> list[PaletteColor]:
    counts: Counter[str] = Counter()
    for red, green, blue in pixels:
        # Centre each deterministic 32-value bucket to reduce palette noise.
        bucket = tuple(min(255, channel // 32 * 32 + 16) for channel in (red, green, blue))
        counts[f"#{bucket[0]:02X}{bucket[1]:02X}{bucket[2]:02X}"] += 1
    total = len(pixels)
    scaled = {colour: count * 10_000 for colour, count in counts.items()}
    result = {colour: value // total for colour, value in scaled.items()}
    remaining = 10_000 - sum(result.values())
    rank: list[str] = sorted(counts, key=lambda key: (-(scaled[key] % total), key))
    for key in rank[:remaining]:
        result[key] += 1
    return [
        PaletteColor(hex=colour, proportion_basis_points=result[colour])
        for colour in sorted(result)
    ]


def _metrics(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int, int]:
    luminance: list[float] = []
    saturation: list[float] = []
    red_green: list[float] = []
    yellow_blue: list[float] = []
    for red, green, blue in pixels:
        maximum = max(red, green, blue)
        minimum = min(red, green, blue)
        luminance.append((0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255)
        saturation.append(0 if maximum == 0 else (maximum - minimum) / maximum)
        red_green.append(float(red - green))
        yellow_blue.append(float((red + green) / 2 - blue))
    mean_lightness = round(sum(luminance) / len(luminance) * 10_000)
    mean_saturation = round(sum(saturation) / len(saturation) * 10_000)
    contrast = round((max(luminance) - min(luminance)) * 10_000)
    rg_std = _stddev(red_green)
    yb_std = _stddev(yellow_blue)
    rg_mean = sum(red_green) / len(red_green)
    yb_mean = sum(yellow_blue) / len(yellow_blue)
    colourfulness = min(
        10_000,
        round(
            (math.sqrt(rg_std**2 + yb_std**2) + 0.3 * math.sqrt(rg_mean**2 + yb_mean**2))
            / 255
            * 10_000
        ),
    )
    return mean_lightness, mean_saturation, contrast, colourfulness


def _stddev(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


class CreativeDeployment(_Strict):
    # M28 turns deployment IDs into a content-derived association identity.
    # M27 records had no payload schema version and therefore remain a
    # separately parsed read-only shape below.
    schema_version: Literal[2] = 2
    deployment_id: str = Field(pattern=r"^deployment:sha256:[a-f0-9]{64}$")
    provider: str = Field(min_length=1, max_length=80)
    account_ref: str = Field(min_length=1, max_length=200)
    ad_ref: str = Field(min_length=1, max_length=200)
    creative_family_id: str = Field(min_length=1, max_length=200)
    creative_version_id: str = Field(pattern=r"^creative:sha256:[a-f0-9]{64}$")
    rendition_refs: tuple[str, ...] = Field(min_length=1, max_length=100)
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    observed_from: datetime
    observed_to: datetime
    association_status: str = Field(min_length=1, max_length=80)

    @field_validator("effective_from", "effective_to", "observed_from", "observed_to")
    @classmethod
    def deployment_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Deployment timestamps must have a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def deployment_interval(self) -> CreativeDeployment:
        if self.observed_to < self.observed_from:
            raise ValueError("Observation interval is reversed")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("Effective interval is reversed")
        if (
            self.effective_from is None or self.effective_to is None
        ) and self.observed_from is None:
            raise ValueError("Unknown effective time requires an observation interval")
        if len(set(self.rendition_refs)) != len(self.rendition_refs) or any(
            not isinstance(reference, str)
            or re.fullmatch(r"rendition:sha256:[a-f0-9]{64}", reference) is None
            for reference in self.rendition_refs
        ):
            raise ValueError("Deployment rendition references must be unique canonical identities")
        if self.deployment_id != _deployment_id(self):
            raise ValueError("Deployment identifier does not match its association interval")
        return self

    @classmethod
    def from_association(
        cls,
        *,
        provider: str,
        account_ref: str,
        ad_ref: str,
        creative_family_id: str,
        creative_version_id: str,
        rendition_refs: tuple[str, ...],
        effective_from: datetime | None,
        effective_to: datetime | None,
        observed_from: datetime,
        observed_to: datetime,
        association_status: str,
    ) -> CreativeDeployment:
        """Build the content-derived deployment identity from its association facts."""
        provisional = cls.model_construct(
            deployment_id="",
            provider=provider,
            account_ref=account_ref,
            ad_ref=ad_ref,
            creative_family_id=creative_family_id,
            creative_version_id=creative_version_id,
            rendition_refs=rendition_refs,
            effective_from=effective_from,
            effective_to=effective_to,
            observed_from=observed_from,
            observed_to=observed_to,
            association_status=association_status,
        )
        return cls(
            deployment_id=_deployment_id(provisional),
            provider=provider,
            account_ref=account_ref,
            ad_ref=ad_ref,
            creative_family_id=creative_family_id,
            creative_version_id=creative_version_id,
            rendition_refs=rendition_refs,
            effective_from=effective_from,
            effective_to=effective_to,
            observed_from=observed_from,
            observed_to=observed_to,
            association_status=association_status,
        )


class _LegacyCreativeDeploymentV1(_Strict):
    """Read-only M27 deployment shape with no content-derived ID assurance."""

    deployment_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=80)
    account_ref: str = Field(min_length=1, max_length=200)
    ad_ref: str = Field(min_length=1, max_length=200)
    creative_family_id: str = Field(min_length=1, max_length=200)
    creative_version_id: str = Field(min_length=1, max_length=200)
    rendition_refs: tuple[str, ...] = Field(min_length=1, max_length=100)
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    observed_from: datetime
    observed_to: datetime
    association_status: str = Field(min_length=1, max_length=80)

    @field_validator("effective_from", "effective_to", "observed_from", "observed_to")
    @classmethod
    def deployment_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, label="legacy deployment timestamp")

    @model_validator(mode="after")
    def deployment_interval(self) -> _LegacyCreativeDeploymentV1:
        if self.observed_to < self.observed_from:
            raise ValueError("Observation interval is reversed")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("Effective interval is reversed")
        if (
            self.effective_from is None or self.effective_to is None
        ) and self.observed_from is None:
            raise ValueError("Unknown effective time requires an observation interval")
        if len(set(self.rendition_refs)) != len(self.rendition_refs):
            raise ValueError("Deployment rendition references must be unique")
        return self


class CreativePerformanceJoin(_Strict):
    metric_id: str
    creative_version_id: str | None = None
    creative_deployment_id: str | None = None
    join_status: Literal["EXACT", "MIXED_CREATIVE_VERSION", "UNKNOWN_EFFECTIVE_TIME", "UNMAPPED"]
    data_quality_statuses: tuple[str, ...] = ()


def join_performance_window(
    *,
    metric_id: str,
    provider: str,
    account_ref: str,
    ad_ref: str,
    window_start: datetime,
    window_end: datetime,
    deployments: tuple[CreativeDeployment, ...] | list[CreativeDeployment],
) -> CreativePerformanceJoin:
    """Return one honest association, never a copied metric row per rendition."""
    if window_start.tzinfo is None or window_end.tzinfo is None or window_end < window_start:
        raise AdhelmError(
            "INVALID_PERFORMANCE_WINDOW",
            "Performance window must be ordered timezone-aware instants.",
            exit_code=2,
        )
    checked_deployments = tuple(revalidate_creative_deployment(item) for item in deployments)
    candidates = [
        item
        for item in checked_deployments
        if (
            item.provider == provider
            and item.account_ref == account_ref
            and item.ad_ref == ad_ref
            and _overlaps(item, window_start, window_end)
        )
    ]
    if not candidates:
        return CreativePerformanceJoin(
            metric_id=metric_id,
            join_status="UNMAPPED",
            data_quality_statuses=("CREATIVE_METADATA_MISSING",),
        )
    if any(item.effective_from is None or item.effective_to is None for item in candidates):
        return CreativePerformanceJoin(
            metric_id=metric_id,
            join_status="UNKNOWN_EFFECTIVE_TIME",
            data_quality_statuses=("MIXED_CREATIVE_VERSION",),
        )
    if len(candidates) != 1 or not _contains(candidates[0], window_start, window_end):
        return CreativePerformanceJoin(
            metric_id=metric_id,
            join_status="MIXED_CREATIVE_VERSION",
            data_quality_statuses=("MIXED_CREATIVE_VERSION",),
        )
    item = candidates[0]
    return CreativePerformanceJoin(
        metric_id=metric_id,
        creative_version_id=item.creative_version_id,
        creative_deployment_id=item.deployment_id,
        join_status="EXACT",
    )


def _overlaps(item: CreativeDeployment, start: datetime, end: datetime) -> bool:
    lower = item.effective_from or item.observed_from
    upper = item.effective_to or item.observed_to
    return lower <= end and upper >= start


def _contains(item: CreativeDeployment, start: datetime, end: datetime) -> bool:
    lower = item.effective_from or item.observed_from
    upper = item.effective_to or item.observed_to
    return lower <= start and end <= upper


_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_ASSET_ID = re.compile(r"^asset:sha256:[a-f0-9]{64}$")
_ANALYSIS_ID = re.compile(r"^asset-analysis:sha256:[a-f0-9]{64}$")
_RENDITION_ID = re.compile(r"^rendition:sha256:[a-f0-9]{64}$")
_VERSION_ID = re.compile(r"^creative:sha256:[a-f0-9]{64}$")
_FINGERPRINT_ID = re.compile(r"^fingerprint:sha256:[a-f0-9]{64}$")
_DEPLOYMENT_ID = re.compile(r"^deployment:sha256:[a-f0-9]{64}$")


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must have a timezone")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return (
        _aware_utc(value, label="creative timestamp")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _aspect_ratio(width_px: int, height_px: int) -> str:
    divisor = math.gcd(width_px, height_px)
    return f"{width_px // divisor}:{height_px // divisor}"


#: Additive public alias so the sibling structure extractor in
#: ``adhelm.creative_extraction`` reuses this exact reduction instead of restating
#: it.  The implementation and every existing analysis result are unchanged.
reduced_aspect_ratio = _aspect_ratio


def _asset_version_id(content_digest: str, mime_type: str, width_px: int, height_px: int) -> str:
    return "asset:sha256:" + digest(
        {
            "content_digest": content_digest,
            "mime_type": mime_type,
            "width_px": width_px,
            "height_px": height_px,
        }
    )


def _rendition_id(
    *,
    source_asset_version_ref: str | None,
    content_digest: str,
    provider_format: str,
    locale: str,
    width_px: int,
    height_px: int,
    transformation_digest: str,
) -> str:
    if source_asset_version_ref is not None:
        # Preserve the installed proven-source identity domain. The source asset
        # already binds content and intrinsic dimensions; provider compilers pin
        # this exact formula in their existing native request digests.
        identity: dict[str, str | int] = {
            "asset_version_id": source_asset_version_ref,
            "provider_format": provider_format,
            "locale": locale,
            "transformation_digest": transformation_digest,
        }
    else:
        # An unknown parent cannot supply those intrinsic facts, so retain them
        # in the rendition identity itself.
        identity = {
            "content_digest": content_digest,
            "provider_format": provider_format,
            "locale": locale,
            "width_px": width_px,
            "height_px": height_px,
            "transformation_digest": transformation_digest,
        }
    return "rendition:sha256:" + digest(identity)


def _analysis_data(analysis: ImageAnalysis) -> dict[str, Any]:
    return analysis.model_dump(mode="json", warnings="error")


def _asset_analysis_id(
    asset_version_id: str, asset_content_digest: str, analysis: ImageAnalysis
) -> str:
    return "asset-analysis:sha256:" + digest(
        {
            "asset_version_id": asset_version_id,
            "asset_content_digest": asset_content_digest,
            **_analysis_data(analysis),
        }
    )


def _fingerprint_id(
    analysis: ImageAnalysis,
    *,
    creative_version_id: str,
    asset_version_id: str,
    rendition_id: str | None,
    analysis_surface: Literal["SOURCE_ASSET", "FINAL_RENDITION"],
) -> str:
    return "fingerprint:sha256:" + digest(
        {
            "creative_version_id": creative_version_id,
            "asset_version_id": asset_version_id,
            "rendition_id": rendition_id,
            "analysis_surface": analysis_surface,
            **_analysis_data(analysis),
        }
    )


def _deployment_id(value: CreativeDeployment) -> str:
    return "deployment:sha256:" + digest(
        {
            "schema_version": value.schema_version,
            "provider": value.provider,
            "account_ref": value.account_ref,
            "ad_ref": value.ad_ref,
            "creative_family_id": value.creative_family_id,
            "creative_version_id": value.creative_version_id,
            "rendition_refs": list(value.rendition_refs),
            "effective_from": (
                None if value.effective_from is None else _timestamp_text(value.effective_from)
            ),
            "effective_to": None
            if value.effective_to is None
            else _timestamp_text(value.effective_to),
            "observed_from": _timestamp_text(value.observed_from),
            "observed_to": _timestamp_text(value.observed_to),
            "association_status": value.association_status,
        }
    )


def _version_intrinsic_projection(value: CreativeVersionRecord) -> dict[str, Any]:
    """Facts whose alteration would reuse a content-derived version incorrectly.

    Message annotations, analyzer references, provenance, and record time are
    intentionally omitted: later local annotation evidence is append-only and
    must not manufacture a new serving identity.
    """
    return {
        "creative_family_id": value.creative_family_id,
        "creative_version_id": value.creative_version_id,
        "provider_format": value.provider_format,
        "locale": value.locale,
        "destination_ref": value.destination_ref,
        "surface_kind": value.surface_kind,
        "rendition_refs": [
            item.model_dump(mode="json", warnings="error") for item in value.rendition_refs
        ],
        "components": [item.model_dump(mode="json", warnings="error") for item in value.components],
        "serving_manifest_schema_version": value.serving_manifest_schema_version,
        "serving_manifest_digest": value.serving_manifest_digest,
    }


def _analysis_from_asset_analysis_record(record: AssetAnalysisRecord) -> ImageAnalysis:
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


def _analysis_from_fingerprint_record(record: ImageFingerprintRecord) -> ImageAnalysis:
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


def _fingerprint_matches_analysis(
    fingerprint: ImageFingerprintRecord | _LegacyImageFingerprintRecordV1,
    analysis: AssetAnalysisRecord,
) -> bool:
    """Rebuild a fingerprint's analyzer facts from its retained parent analysis.

    M27 fingerprints omitted ``mime_type`` even though it was in the original
    fingerprint digest.  A v1 payload is therefore checked only here, with its
    exact referenced analysis supplying that missing fact; it is never silently
    rewritten to the v2 payload shape.
    """
    if isinstance(fingerprint, _LegacyImageFingerprintRecordV1) and (
        fingerprint.analysis_surface == "PROVIDER_PREVIEW"
    ):
        return False
    try:
        fingerprint_analysis = ImageAnalysis(
            analyzer_name=fingerprint.analyzer_name,
            analyzer_version=fingerprint.analyzer_version,
            mime_type=(
                fingerprint.mime_type
                if isinstance(fingerprint, ImageFingerprintRecord)
                else analysis.mime_type
            ),
            width_px=fingerprint.width_px,
            height_px=fingerprint.height_px,
            aspect_ratio=fingerprint.aspect_ratio,
            palette=fingerprint.palette,
            mean_lightness_basis_points=fingerprint.mean_lightness_basis_points,
            mean_saturation_basis_points=fingerprint.mean_saturation_basis_points,
            contrast_basis_points=fingerprint.contrast_basis_points,
            colorfulness_basis_points=fingerprint.colorfulness_basis_points,
            extraction_statuses=fingerprint.extraction_statuses,
        )
    except (TypeError, ValueError, ValidationError):
        return False
    expected = _fingerprint_id(
        fingerprint_analysis,
        creative_version_id=fingerprint.creative_version_id,
        asset_version_id=fingerprint.asset_version_id,
        rendition_id=fingerprint.rendition_id,
        analysis_surface=cast(
            Literal["SOURCE_ASSET", "FINAL_RENDITION"], fingerprint.analysis_surface
        ),
    )
    return fingerprint.fingerprint_id == expected and _analysis_data(
        fingerprint_analysis
    ) == _analysis_data(_analysis_from_asset_analysis_record(analysis))


def _creative_mapping(value: object, expected: type[BaseModel]) -> dict[str, Any]:
    """Return JSON-shaped data without allowing a constructed model to bypass parsing."""
    try:
        if type(value) is expected:
            output = value.model_dump(mode="json", warnings="error")
        elif type(value) is dict:
            output = value
        else:
            raise ValueError("creative record type")
        if type(output) is not dict:
            raise ValueError("creative record shape")
        return output
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise AdhelmError(
            "INVALID_CREATIVE_HISTORY", "Creative history data is invalid.", exit_code=2
        ) from None


def _revalidate_creative_model[T: BaseModel](expected: type[T], value: object) -> T:
    try:
        return validate_json_value(expected, _creative_mapping(value, expected))
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise AdhelmError(
            "INVALID_CREATIVE_HISTORY", "Creative history data is invalid.", exit_code=2
        ) from None


def _creative_union_mapping(value: object, accepted: tuple[type[BaseModel], ...]) -> dict[str, Any]:
    """Return a closed raw mapping for one explicitly versioned payload family."""
    try:
        if type(value) in accepted:
            raw = cast(BaseModel, value).model_dump(mode="json", warnings="error")
        elif type(value) is dict:
            raw = value
        else:
            raise ValueError("creative record type")
        if type(raw) is not dict:
            raise ValueError("creative record shape")
        return raw
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise AdhelmError(
            "INVALID_CREATIVE_HISTORY", "Creative history data is invalid.", exit_code=2
        ) from None


def _revalidate_fingerprint_payload(
    value: object,
) -> ImageFingerprintRecord | _LegacyImageFingerprintRecordV1:
    try:
        raw = _creative_union_mapping(
            value, (ImageFingerprintRecord, _LegacyImageFingerprintRecordV1)
        )
        version = raw.get("schema_version")
        if type(version) is int and version == 2:
            return validate_json_value(ImageFingerprintRecord, raw)
        if type(version) is int and version == 1:
            return validate_json_value(_LegacyImageFingerprintRecordV1, raw)
        raise ValueError("fingerprint schema version")
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise AdhelmError(
            "INVALID_CREATIVE_HISTORY", "Creative history data is invalid.", exit_code=2
        ) from None


def _revalidate_deployment_payload(
    value: object,
) -> CreativeDeployment | _LegacyCreativeDeploymentV1:
    try:
        raw = _creative_union_mapping(value, (CreativeDeployment, _LegacyCreativeDeploymentV1))
        if "schema_version" in raw:
            return validate_json_value(CreativeDeployment, raw)
        return validate_json_value(_LegacyCreativeDeploymentV1, raw)
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise AdhelmError(
            "INVALID_CREATIVE_HISTORY", "Creative history data is invalid.", exit_code=2
        ) from None


def _legacy_payload_requires_migration() -> NoReturn:
    raise AdhelmError(
        "CREATIVE_HISTORY_MIGRATION_REQUIRED",
        "This legacy creative payload requires an explicit migration before new use.",
        exit_code=3,
        next_action="Inspect retained history; migration for new use is not installed.",
    )


def revalidate_creative_asset_version(value: object) -> CreativeAssetVersion:
    return _revalidate_creative_model(CreativeAssetVersion, value)


def revalidate_image_analysis(value: object) -> ImageAnalysis:
    return _revalidate_creative_model(ImageAnalysis, value)


def revalidate_asset_analysis_record(value: object) -> AssetAnalysisRecord:
    return _revalidate_creative_model(AssetAnalysisRecord, value)


def revalidate_creative_rendition(value: object) -> CreativeRendition:
    return _revalidate_creative_model(CreativeRendition, value)


def revalidate_creative_version_record(value: object) -> CreativeVersionRecord:
    return _revalidate_creative_model(CreativeVersionRecord, value)


def revalidate_image_fingerprint_record(value: object) -> ImageFingerprintRecord:
    checked = _revalidate_fingerprint_payload(value)
    if isinstance(checked, _LegacyImageFingerprintRecordV1):
        _legacy_payload_requires_migration()
    return checked


def revalidate_creative_deployment(value: object) -> CreativeDeployment:
    checked = _revalidate_deployment_payload(value)
    if isinstance(checked, _LegacyCreativeDeploymentV1):
        _legacy_payload_requires_migration()
    return checked


def is_legacy_creative_payload(value: BaseModel) -> bool:
    """Tell ledger append paths not to create another pre-M28 payload."""
    return isinstance(value, (_LegacyImageFingerprintRecordV1, _LegacyCreativeDeploymentV1))


def revalidate_creative_payload(kind: str, value: object) -> BaseModel:
    """Reparse a closed creative record before ledger or registration use."""
    models: dict[str, type[BaseModel]] = {
        "CREATIVE_FAMILY": CreativeFamilyRecord,
        "CREATIVE_ASSET_VERSION": CreativeAssetVersion,
        "CREATIVE_ASSET_ANALYSIS": AssetAnalysisRecord,
        "CREATIVE_RENDITION": CreativeRendition,
        "CREATIVE_VERSION": CreativeVersionRecord,
        "CREATIVE_FINGERPRINT": ImageFingerprintRecord,
        "CREATIVE_DEPLOYMENT": CreativeDeployment,
    }
    if kind == "CREATIVE_FINGERPRINT":
        return _revalidate_fingerprint_payload(value)
    if kind == "CREATIVE_DEPLOYMENT":
        return _revalidate_deployment_payload(value)
    model = models.get(kind)
    if model is None:
        raise AdhelmError(
            "INVALID_CREATIVE_HISTORY", "Creative record kind is invalid.", exit_code=2
        )
    return _revalidate_creative_model(model, value)


def validate_asset_analysis_binding(
    asset: CreativeAssetVersion, analysis: AssetAnalysisRecord
) -> None:
    """Require a retained analysis to describe this exact content-derived asset."""
    checked_asset = revalidate_creative_asset_version(asset)
    checked_analysis = revalidate_asset_analysis_record(analysis)
    if (
        checked_analysis.asset_version_id != checked_asset.asset_version_id
        or checked_analysis.asset_content_digest != checked_asset.content_digest
        or checked_analysis.mime_type != checked_asset.mime_type
        or checked_analysis.width_px != checked_asset.width_px
        or checked_analysis.height_px != checked_asset.height_px
        or checked_analysis.aspect_ratio != checked_asset.aspect_ratio
    ):
        raise AdhelmError(
            "INVALID_CREATIVE_HISTORY",
            "Creative analysis does not bind the selected asset.",
            exit_code=2,
        )


def validate_proven_rendition_binding(
    asset: CreativeAssetVersion, rendition: CreativeRendition
) -> None:
    """Verify the only installed local transformation is byte-for-byte identity."""
    checked_asset = revalidate_creative_asset_version(asset)
    checked_rendition = revalidate_creative_rendition(rendition)
    if (
        checked_rendition.source_relation_status != "PROVEN_SOURCE_ASSET"
        or checked_rendition.source_asset_version_ref != checked_asset.asset_version_id
        or checked_rendition.content_digest != checked_asset.content_digest
        or checked_rendition.width_px != checked_asset.width_px
        or checked_rendition.height_px != checked_asset.height_px
        or checked_rendition.transformation_spec != IdentityTransformation()
        or checked_rendition.rendition_id
        != _rendition_id(
            source_asset_version_ref=checked_asset.asset_version_id,
            content_digest=checked_asset.content_digest,
            provider_format=checked_rendition.provider_format,
            locale=checked_rendition.locale,
            width_px=checked_asset.width_px,
            height_px=checked_asset.height_px,
            transformation_digest=digest(IdentityTransformation()),
        )
    ):
        raise AdhelmError(
            "INVALID_CREATIVE_HISTORY",
            "Proven rendition does not match its source asset.",
            exit_code=2,
        )


def validate_creative_history_graph(records: list[dict[str, Any]]) -> None:
    """Validate the ordered, closed creative identity graph in one stream prefix.

    The ledger invokes this for both append candidates and physical rereads.  It
    deliberately accepts a source-asset analysis before any creative version;
    a final rendition fingerprint is accepted only after its exact version.
    """
    families: dict[str, CreativeFamilyRecord] = {}
    assets: dict[str, CreativeAssetVersion] = {}
    analyses: dict[str, AssetAnalysisRecord] = {}
    renditions: dict[str, CreativeRendition] = {}
    versions: dict[str, CreativeVersionRecord] = {}
    fingerprints: dict[str, ImageFingerprintRecord | _LegacyImageFingerprintRecordV1] = {}
    deployments: dict[str, CreativeDeployment | _LegacyCreativeDeploymentV1] = {}

    def require_exact[T: BaseModel](table: dict[str, T], identity: str, value: T) -> None:
        prior = table.get(identity)
        if prior is not None and canonical_bytes(
            prior.model_dump(mode="json", warnings="error")
        ) != canonical_bytes(value.model_dump(mode="json", warnings="error")):
            raise AdhelmError(
                "INVALID_CREATIVE_HISTORY",
                "A creative identifier cannot change its retained identity.",
                exit_code=2,
            )
        table[identity] = value

    for record in records:
        kind = record.get("record_kind")
        if kind == "SEGMENT_STARTED":
            continue
        payload = record.get("payload")
        if type(payload) is not dict:
            raise AdhelmError(
                "INVALID_CREATIVE_HISTORY", "Creative record payload is invalid.", exit_code=2
            )
        checked = revalidate_creative_payload(cast(str, kind), payload)
        if kind == "CREATIVE_FAMILY":
            family = cast(CreativeFamilyRecord, checked)
            require_exact(families, family.creative_family_id, family)
        elif kind == "CREATIVE_ASSET_VERSION":
            asset = cast(CreativeAssetVersion, checked)
            require_exact(assets, asset.asset_version_id, asset)
        elif kind == "CREATIVE_ASSET_ANALYSIS":
            analysis = cast(AssetAnalysisRecord, checked)
            analysis_asset = assets.get(analysis.asset_version_id)
            if analysis_asset is None:
                raise AdhelmError(
                    "CHECKPOINT_MISSING_REFERENCE",
                    "Creative analysis lacks its prior asset.",
                    exit_code=2,
                )
            validate_asset_analysis_binding(analysis_asset, analysis)
            require_exact(analyses, analysis.asset_analysis_id, analysis)
        elif kind == "CREATIVE_RENDITION":
            rendition = cast(CreativeRendition, checked)
            if rendition.source_relation_status == "PROVEN_SOURCE_ASSET":
                assert rendition.source_asset_version_ref is not None
                rendition_asset = assets.get(rendition.source_asset_version_ref)
                if rendition_asset is None:
                    raise AdhelmError(
                        "CHECKPOINT_MISSING_REFERENCE",
                        "Proven rendition lacks its prior asset.",
                        exit_code=2,
                    )
                validate_proven_rendition_binding(rendition_asset, rendition)
            require_exact(renditions, rendition.rendition_id, rendition)
        elif kind == "CREATIVE_VERSION":
            version = cast(CreativeVersionRecord, checked)
            if version.creative_family_id not in families:
                raise AdhelmError(
                    "CHECKPOINT_MISSING_REFERENCE",
                    "Creative version lacks its prior family.",
                    exit_code=2,
                )
            expected_assets: set[str] = set()
            for reference in version.rendition_refs:
                version_rendition = renditions.get(reference.rendition_id)
                if version_rendition is None:
                    raise AdhelmError(
                        "CHECKPOINT_MISSING_REFERENCE",
                        "Creative version lacks its prior rendition.",
                        exit_code=2,
                    )
                if (
                    version_rendition.provider_format != version.provider_format
                    or version_rendition.locale != version.locale
                ):
                    raise AdhelmError(
                        "INVALID_CREATIVE_HISTORY",
                        "Creative version and rendition format bindings differ.",
                        exit_code=2,
                    )
                if version_rendition.source_relation_status == "PROVEN_SOURCE_ASSET":
                    assert version_rendition.source_asset_version_ref is not None
                    expected_assets.add(version_rendition.source_asset_version_ref)
            referenced_analyses = [
                analyses[item] for item in version.analyzer_refs if item in analyses
            ]
            if len(referenced_analyses) != len(version.analyzer_refs):
                raise AdhelmError(
                    "CHECKPOINT_MISSING_REFERENCE",
                    "Creative version analyzer references are unavailable.",
                    exit_code=2,
                )
            if (
                expected_assets
                and {item.asset_version_id for item in referenced_analyses} != expected_assets
            ):
                raise AdhelmError(
                    "INVALID_CREATIVE_HISTORY",
                    "Creative version analyzer evidence does not cover its proven "
                    "rendition assets.",
                    exit_code=2,
                )
            prior_version = versions.get(version.creative_version_id)
            if prior_version is not None and canonical_bytes(
                _version_intrinsic_projection(prior_version)
            ) != canonical_bytes(_version_intrinsic_projection(version)):
                raise AdhelmError(
                    "INVALID_CREATIVE_HISTORY",
                    "A creative version identifier cannot change its serving identity.",
                    exit_code=2,
                )
            versions[version.creative_version_id] = version
        elif kind == "CREATIVE_FINGERPRINT":
            fingerprint = cast(ImageFingerprintRecord | _LegacyImageFingerprintRecordV1, checked)
            if isinstance(fingerprint, _LegacyImageFingerprintRecordV1) and (
                fingerprint.analysis_surface == "PROVIDER_PREVIEW"
            ):
                _legacy_payload_requires_migration()
            fingerprint_version = versions.get(fingerprint.creative_version_id)
            fingerprint_asset = assets.get(fingerprint.asset_version_id)
            if fingerprint_version is None or fingerprint_asset is None:
                raise AdhelmError(
                    "CHECKPOINT_MISSING_REFERENCE",
                    "Creative fingerprint lacks its version or asset.",
                    exit_code=2,
                )
            fingerprint_rendition: CreativeRendition | None = None
            if fingerprint.rendition_id is not None:
                fingerprint_rendition = renditions.get(fingerprint.rendition_id)
                if fingerprint_rendition is None or fingerprint_rendition.rendition_id not in {
                    item.rendition_id for item in fingerprint_version.rendition_refs
                }:
                    raise AdhelmError(
                        "INVALID_CREATIVE_HISTORY",
                        "Creative fingerprint rendition is not in its version.",
                        exit_code=2,
                    )
                if (
                    fingerprint_rendition.source_relation_status == "PROVEN_SOURCE_ASSET"
                    and fingerprint_rendition.source_asset_version_ref
                    != fingerprint_asset.asset_version_id
                ):
                    raise AdhelmError(
                        "INVALID_CREATIVE_HISTORY",
                        "Creative fingerprint asset disagrees with its rendition.",
                        exit_code=2,
                    )
                if fingerprint_rendition.source_relation_status == "UNKNOWN_SOURCE_ASSET" and (
                    fingerprint_rendition.content_digest != fingerprint_asset.content_digest
                    or fingerprint_rendition.width_px != fingerprint_asset.width_px
                    or fingerprint_rendition.height_px != fingerprint_asset.height_px
                ):
                    raise AdhelmError(
                        "INVALID_CREATIVE_HISTORY",
                        "An unknown-source rendition fingerprint lacks matching local "
                        "content evidence.",
                        exit_code=2,
                    )
            elif not any(
                candidate.source_asset_version_ref == fingerprint_asset.asset_version_id
                for candidate in (
                    renditions[item.rendition_id] for item in fingerprint_version.rendition_refs
                )
                if candidate.source_relation_status == "PROVEN_SOURCE_ASSET"
            ):
                raise AdhelmError(
                    "INVALID_CREATIVE_HISTORY",
                    "Source fingerprint asset is not used by its version.",
                    exit_code=2,
                )
            matching = [
                analysis
                for analysis_id, analysis in analyses.items()
                if analysis_id in fingerprint_version.analyzer_refs
                and analysis.asset_version_id == fingerprint_asset.asset_version_id
                and _fingerprint_matches_analysis(fingerprint, analysis)
            ]
            if len(matching) != 1:
                raise AdhelmError(
                    "INVALID_CREATIVE_HISTORY",
                    "Creative fingerprint does not match its version analyzer evidence.",
                    exit_code=2,
                )
            require_exact(fingerprints, fingerprint.fingerprint_id, fingerprint)
        elif kind == "CREATIVE_DEPLOYMENT":
            deployment = cast(CreativeDeployment | _LegacyCreativeDeploymentV1, checked)
            deployment_version = versions.get(deployment.creative_version_id)
            if (
                deployment_version is None
                or deployment.creative_family_id != deployment_version.creative_family_id
            ):
                raise AdhelmError(
                    "INVALID_CREATIVE_HISTORY",
                    "Deployment family does not match its creative version.",
                    exit_code=2,
                )
            expected = tuple(item.rendition_id for item in deployment_version.rendition_refs)
            if deployment.rendition_refs != expected:
                raise AdhelmError(
                    "INVALID_CREATIVE_HISTORY",
                    "Deployment rendition order does not match its creative version.",
                    exit_code=2,
                )
            require_exact(deployments, deployment.deployment_id, deployment)
        else:
            raise AdhelmError(
                "INVALID_CREATIVE_HISTORY", "Creative record kind is invalid.", exit_code=2
            )
