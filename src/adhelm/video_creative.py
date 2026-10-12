"""Closed customer-declared video identities without local decoding or registration.

This module deliberately records a declaration rather than a verified media asset.
It never accepts a filesystem path or bytes, opens a file, invokes a decoder, or
claims that the declared digest, metadata, or rendition has been locally proved.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.creative_extraction import (
    MAX_EXTRACTED_DURATION_MS,
    VideoContainerMetadata,
    extract_video_metadata,
)
from adhelm.models import StrictModel

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VIDEO_ASSET_ID = re.compile(r"^video_asset:sha256:[0-9a-f]{64}$")
_VIDEO_RENDITION_ID = re.compile(r"^video_rendition:sha256:[0-9a-f]{64}$")
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}$")
_EVIDENCE_REF = re.compile(r"^evidence:sha256:[0-9a-f]{64}$")
_MAX_DECLARED_VIDEO_BYTES = 1_073_741_824
_MAX_DIMENSION = 8_192
#: The declared and extracted duration domains are one bound.  A declaration the
#: extractor could never confirm, or an extracted value no declaration could
#: carry, would make ``duration_status`` meaningless for that media.
_MAX_DURATION_MS = MAX_EXTRACTED_DURATION_MS
_MAX_FRAME_RATE_MILLI = 240_000
_COMPARISON = Literal[
    "DECLARATION_MATCH",
    "DECLARATION_WITHIN_TRUNCATION",
    "DECLARATION_MISMATCH",
    "NOT_COMPARABLE_NOT_IN_CONTAINER",
]
#: The extractor floors ``duration_ms`` and ``frame_rate_milli``, so one unit of
#: its own truncation is not evidence that a declaration disagrees.
_TRUNCATION_TOLERANCE = 1
_EXACT_COMPARISON = Literal["DECLARATION_MATCH", "DECLARATION_MISMATCH"]


def _video_asset_identity(
    *,
    declared_content_digest: str,
    mime_type: str,
    declared_byte_size: int,
    declared_width_px: int,
    declared_height_px: int,
    declared_duration_ms: int,
    declared_frame_rate_milli: int,
) -> str:
    return "video_asset:sha256:" + digest(
        {
            "schema_version": 1,
            "declared_content_digest": declared_content_digest,
            "mime_type": mime_type,
            "declared_byte_size": declared_byte_size,
            "declared_width_px": declared_width_px,
            "declared_height_px": declared_height_px,
            "declared_duration_ms": declared_duration_ms,
            "declared_frame_rate_milli": declared_frame_rate_milli,
        }
    )


def _declared_identity_transformation_digest() -> str:
    return digest(
        {
            "kind": "DECLARED_IDENTITY_NO_LOCAL_DECODE",
            "local_media_status": "DECLARED_UNVERIFIED_NO_LOCAL_DECODE",
        }
    )


def _video_rendition_identity(
    *,
    video_asset_version_ref: str,
    provider_format: str,
    locale: str,
    transformation_digest: str,
) -> str:
    return "video_rendition:sha256:" + digest(
        {
            "schema_version": 1,
            "video_asset_version_ref": video_asset_version_ref,
            "provider_format": provider_format,
            "locale": locale,
            "transformation_digest": transformation_digest,
        }
    )


class CustomerDeclaredVideoAsset(StrictModel):
    """A bounded customer declaration, not proof of locally available video bytes."""

    schema_version: Literal[1] = 1
    video_asset_version_id: str = Field(pattern=_VIDEO_ASSET_ID.pattern)
    declared_content_digest: str = Field(pattern=_DIGEST.pattern)
    media_type: Literal["VIDEO"] = "VIDEO"
    mime_type: Literal["video/mp4"] = "video/mp4"
    declared_byte_size: int = Field(ge=1, le=_MAX_DECLARED_VIDEO_BYTES)
    declared_width_px: int = Field(ge=1, le=_MAX_DIMENSION)
    declared_height_px: int = Field(ge=1, le=_MAX_DIMENSION)
    declared_duration_ms: int = Field(ge=1, le=_MAX_DURATION_MS)
    declared_frame_rate_milli: int = Field(ge=1, le=_MAX_FRAME_RATE_MILLI)
    evidence_ref: str = Field(pattern=_EVIDENCE_REF.pattern)
    provenance: Literal["CUSTOMER_DECLARED"] = "CUSTOMER_DECLARED"
    local_media_status: Literal["DECLARED_UNVERIFIED_NO_LOCAL_DECODE"] = (
        "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"
    )
    execution_eligibility: Literal["REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"] = (
        "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
    )
    decoded_frame_status: Literal["NOT_DECODED"] = "NOT_DECODED"
    palette_analysis_status: Literal["NOT_ANALYZED_NO_VIDEO_DECODE"] = (
        "NOT_ANALYZED_NO_VIDEO_DECODE"
    )
    ocr_status: Literal["NOT_ANALYZED_NO_VIDEO_DECODE"] = "NOT_ANALYZED_NO_VIDEO_DECODE"
    transcript_status: Literal["NOT_ANALYZED_NO_AUDIO_EXTRACTION"] = (
        "NOT_ANALYZED_NO_AUDIO_EXTRACTION"
    )
    audio_analysis_status: Literal["NOT_ANALYZED_NO_AUDIO_EXTRACTION"] = (
        "NOT_ANALYZED_NO_AUDIO_EXTRACTION"
    )
    component_performance_status: Literal["NOT_INFERRED"] = "NOT_INFERRED"

    @model_validator(mode="after")
    def identity_binds_every_declared_intrinsic_field(self) -> CustomerDeclaredVideoAsset:
        expected = _video_asset_identity(
            declared_content_digest=self.declared_content_digest,
            mime_type=self.mime_type,
            declared_byte_size=self.declared_byte_size,
            declared_width_px=self.declared_width_px,
            declared_height_px=self.declared_height_px,
            declared_duration_ms=self.declared_duration_ms,
            declared_frame_rate_milli=self.declared_frame_rate_milli,
        )
        if self.video_asset_version_id != expected:
            raise ValueError(
                "video_asset_version_id must bind the complete declared video intrinsic metadata"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        declared_content_digest: str,
        declared_byte_size: int,
        declared_width_px: int,
        declared_height_px: int,
        declared_duration_ms: int,
        declared_frame_rate_milli: int,
        evidence_ref: str,
    ) -> CustomerDeclaredVideoAsset:
        return cls(
            video_asset_version_id=_video_asset_identity(
                declared_content_digest=declared_content_digest,
                mime_type="video/mp4",
                declared_byte_size=declared_byte_size,
                declared_width_px=declared_width_px,
                declared_height_px=declared_height_px,
                declared_duration_ms=declared_duration_ms,
                declared_frame_rate_milli=declared_frame_rate_milli,
            ),
            declared_content_digest=declared_content_digest,
            declared_byte_size=declared_byte_size,
            declared_width_px=declared_width_px,
            declared_height_px=declared_height_px,
            declared_duration_ms=declared_duration_ms,
            declared_frame_rate_milli=declared_frame_rate_milli,
            evidence_ref=evidence_ref,
        )


class CustomerDeclaredVideoRendition(StrictModel):
    """A declared serving rendition derived from a declared video asset identity."""

    schema_version: Literal[1] = 1
    rendition_id: str = Field(pattern=_VIDEO_RENDITION_ID.pattern)
    video_asset_version_ref: str = Field(pattern=_VIDEO_ASSET_ID.pattern)
    declared_content_digest: str = Field(pattern=_DIGEST.pattern)
    media_type: Literal["VIDEO"] = "VIDEO"
    provider_format: str = Field(min_length=1, max_length=120)
    locale: str = Field(min_length=2, max_length=32)
    declared_width_px: int = Field(ge=1, le=_MAX_DIMENSION)
    declared_height_px: int = Field(ge=1, le=_MAX_DIMENSION)
    source_relation_status: Literal["DECLARED_UNVERIFIED"] = "DECLARED_UNVERIFIED"
    transformation_status: Literal["DECLARED_IDENTITY_NO_LOCAL_DECODE"] = (
        "DECLARED_IDENTITY_NO_LOCAL_DECODE"
    )
    transformation_digest: str = Field(pattern=_DIGEST.pattern)
    local_media_status: Literal["DECLARED_UNVERIFIED_NO_LOCAL_DECODE"] = (
        "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"
    )
    decoded_frame_status: Literal["NOT_DECODED"] = "NOT_DECODED"

    @field_validator("locale")
    @classmethod
    def valid_locale(cls, value: str) -> str:
        if _LOCALE.fullmatch(value) is None:
            raise ValueError("locale must be a bounded language tag")
        return value

    @field_validator("provider_format")
    @classmethod
    def valid_provider_format(cls, value: str) -> str:
        if not value.isascii() or not value.isprintable() or value != value.strip():
            raise ValueError("provider_format must be a printable ASCII identifier")
        return value

    @model_validator(mode="after")
    def identity_binds_declared_video_surface(self) -> CustomerDeclaredVideoRendition:
        expected_transform = _declared_identity_transformation_digest()
        expected_rendition = _video_rendition_identity(
            video_asset_version_ref=self.video_asset_version_ref,
            provider_format=self.provider_format,
            locale=self.locale,
            transformation_digest=expected_transform,
        )
        if (
            self.transformation_digest != expected_transform
            or self.rendition_id != expected_rendition
        ):
            raise ValueError(
                "declared video rendition identity must bind its source, format, locale, and status"
            )
        return self

    @classmethod
    def from_video_asset(
        cls,
        asset: CustomerDeclaredVideoAsset,
        *,
        provider_format: str,
        locale: str,
    ) -> CustomerDeclaredVideoRendition:
        transformation_digest = _declared_identity_transformation_digest()
        return cls(
            rendition_id=_video_rendition_identity(
                video_asset_version_ref=asset.video_asset_version_id,
                provider_format=provider_format,
                locale=locale,
                transformation_digest=transformation_digest,
            ),
            video_asset_version_ref=asset.video_asset_version_id,
            declared_content_digest=asset.declared_content_digest,
            provider_format=provider_format,
            locale=locale,
            declared_width_px=asset.declared_width_px,
            declared_height_px=asset.declared_height_px,
            transformation_digest=transformation_digest,
        )


class DeclaredVideoContainerObservation(StrictModel):
    """A declared video asset beside its locally extracted container metadata.

    Declared and extracted facts stay in separate fields.  An extracted value
    never replaces a declaration, never changes the declared asset identity, and
    never upgrades ``DECLARED_UNVERIFIED_NO_LOCAL_DECODE`` into a decoded claim.
    Container metadata proves the container's own headers only; a disagreement
    is recorded as ``DECLARATION_MISMATCH`` instead of a silent correction.
    """

    schema_version: Literal[1] = 1
    video_asset_version_ref: str = Field(pattern=_VIDEO_ASSET_ID.pattern)
    declared_content_digest: str = Field(pattern=_DIGEST.pattern)
    declared_mime_type: Literal["video/mp4"] = "video/mp4"
    declared_byte_size: int = Field(ge=1, le=_MAX_DECLARED_VIDEO_BYTES)
    declared_width_px: int = Field(ge=1, le=_MAX_DIMENSION)
    declared_height_px: int = Field(ge=1, le=_MAX_DIMENSION)
    declared_duration_ms: int = Field(ge=1, le=_MAX_DURATION_MS)
    declared_frame_rate_milli: int = Field(ge=1, le=_MAX_FRAME_RATE_MILLI)
    extracted: VideoContainerMetadata
    content_digest_status: _EXACT_COMPARISON
    byte_size_status: _EXACT_COMPARISON
    container_status: _EXACT_COMPARISON
    width_status: _COMPARISON
    height_status: _COMPARISON
    duration_status: _COMPARISON
    frame_rate_status: _COMPARISON
    declaration_status: Literal[
        "DECLARATION_MATCH", "DECLARATION_MISMATCH", "DECLARATION_PARTIALLY_UNVERIFIED"
    ]
    local_media_status: Literal["CONTAINER_METADATA_ONLY_NO_DECODE"] = (
        "CONTAINER_METADATA_ONLY_NO_DECODE"
    )
    decoded_frame_status: Literal["NOT_DECODED"] = "NOT_DECODED"
    execution_eligibility: Literal["REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"] = (
        "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
    )
    component_performance_status: Literal["NOT_INFERRED"] = "NOT_INFERRED"
    provenance: Literal["CUSTOMER_DECLARED_PLUS_LOCAL_CONTAINER"] = (
        "CUSTOMER_DECLARED_PLUS_LOCAL_CONTAINER"
    )

    @model_validator(mode="after")
    def observation_rederives_everything_it_asserts(self) -> DeclaredVideoContainerObservation:
        """Re-derive the identity and every comparison from the values held here.

        A persisted observation must not be able to state a verification result
        that its own declared fields and extracted container do not produce.
        """
        if self.video_asset_version_ref != _video_asset_identity(
            declared_content_digest=self.declared_content_digest,
            mime_type=self.declared_mime_type,
            declared_byte_size=self.declared_byte_size,
            declared_width_px=self.declared_width_px,
            declared_height_px=self.declared_height_px,
            declared_duration_ms=self.declared_duration_ms,
            declared_frame_rate_milli=self.declared_frame_rate_milli,
        ):
            raise ValueError(
                "video_asset_version_ref must bind the complete declared video intrinsic metadata"
            )
        expected = _declaration_comparisons(self, self.extracted)
        stated = {
            "content_digest_status": self.content_digest_status,
            "byte_size_status": self.byte_size_status,
            "container_status": self.container_status,
            "width_status": self.width_status,
            "height_status": self.height_status,
            "duration_status": self.duration_status,
            "frame_rate_status": self.frame_rate_status,
        }
        if stated != expected:
            raise ValueError("Every declaration comparison must follow from the stored values")
        if self.declaration_status != _declaration_summary(expected):
            raise ValueError("declaration_status must summarize its exact field comparisons")
        return self

    @classmethod
    def from_declaration(
        cls, asset: CustomerDeclaredVideoAsset, metadata: VideoContainerMetadata
    ) -> DeclaredVideoContainerObservation:
        comparisons = _declaration_comparisons(asset, metadata)
        return cls(
            video_asset_version_ref=asset.video_asset_version_id,
            declared_content_digest=asset.declared_content_digest,
            declared_byte_size=asset.declared_byte_size,
            declared_width_px=asset.declared_width_px,
            declared_height_px=asset.declared_height_px,
            declared_duration_ms=asset.declared_duration_ms,
            declared_frame_rate_milli=asset.declared_frame_rate_milli,
            extracted=metadata,
            declaration_status=_declaration_summary(comparisons),
            **comparisons,
        )


def observe_declared_video_container(
    asset: CustomerDeclaredVideoAsset, data: bytes
) -> DeclaredVideoContainerObservation:
    """Attach bounded local container metadata to an existing video declaration."""
    return DeclaredVideoContainerObservation.from_declaration(asset, extract_video_metadata(data))


class _DeclaredVideoFacts(Protocol):
    """The declared intrinsics a comparison needs, from the asset or the record."""

    @property
    def declared_content_digest(self) -> str: ...

    @property
    def declared_byte_size(self) -> int: ...

    @property
    def declared_width_px(self) -> int: ...

    @property
    def declared_height_px(self) -> int: ...

    @property
    def declared_duration_ms(self) -> int: ...

    @property
    def declared_frame_rate_milli(self) -> int: ...


def _declaration_comparisons(
    declared: _DeclaredVideoFacts, metadata: VideoContainerMetadata
) -> dict[str, Any]:
    return {
        "content_digest_status": _exact(
            declared.declared_content_digest == metadata.content_digest
        ),
        "byte_size_status": _exact(declared.declared_byte_size == metadata.byte_size),
        "container_status": _exact(metadata.container_kind == "ISO_BMFF"),
        "width_status": _compare(declared.declared_width_px, metadata.video_width_px),
        "height_status": _compare(declared.declared_height_px, metadata.video_height_px),
        "duration_status": _compare(
            declared.declared_duration_ms, metadata.duration_ms, tolerance=_TRUNCATION_TOLERANCE
        ),
        "frame_rate_status": _compare(
            declared.declared_frame_rate_milli,
            metadata.frame_rate_milli,
            tolerance=_TRUNCATION_TOLERANCE,
        ),
    }


def _declaration_summary(
    comparisons: dict[str, Any],
) -> Literal["DECLARATION_MATCH", "DECLARATION_MISMATCH", "DECLARATION_PARTIALLY_UNVERIFIED"]:
    values = tuple(comparisons.values())
    if "DECLARATION_MISMATCH" in values:
        return "DECLARATION_MISMATCH"
    if "NOT_COMPARABLE_NOT_IN_CONTAINER" in values:
        return "DECLARATION_PARTIALLY_UNVERIFIED"
    return "DECLARATION_MATCH"


def _exact(matched: bool) -> _EXACT_COMPARISON:
    return "DECLARATION_MATCH" if matched else "DECLARATION_MISMATCH"


def _compare(declared: int, extracted: int | None, *, tolerance: int = 0) -> _COMPARISON:
    """Compare a declared value with an extracted one inside the stated tolerance.

    The extractor floors both ``duration_ms`` and ``frame_rate_milli`` out of an
    exact integer ratio, so a truthful declaration can legitimately sit one unit
    above the extracted value.  That is disclosed as its own status rather than
    reported as a flat disagreement or hidden as an exact match.
    """
    if extracted is None:
        return "NOT_COMPARABLE_NOT_IN_CONTAINER"
    if declared == extracted:
        return "DECLARATION_MATCH"
    if 0 < declared - extracted <= tolerance:
        return "DECLARATION_WITHIN_TRUNCATION"
    return "DECLARATION_MISMATCH"
