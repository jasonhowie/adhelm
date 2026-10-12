"""Deterministic local creative extraction: image structure and video containers.

The module extracts only what the bytes themselves prove.  It runs no model, no
OCR engine, no speech recognition, no frame decoder, and no identity, biometric,
or protected-trait inference.  Every unsupported field is an explicit status
rather than a guess, and no record retains raw pixels, media bytes, or text.

Image work reuses the existing bounded Pillow decode in :mod:`adhelm.creative` so
the shared palette and colour metrics stay byte-identical with
``analyze_image``.  The added layout grid and text-presence heuristic use exact
integer arithmetic, so their results do not depend on platform float behaviour.

Video work parses container metadata only.  ISO base media (MP4/MOV) boxes and
Matroska/WebM EBML elements are read with bounded depth, bounded element counts,
and buffer-bound size checks.  A malformed, oversized, or unsupported container
fails closed with a typed error instead of a partial record.
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from adhelm.canonical import digest
from adhelm.creative import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    AssetAnalysisRecord,
    ImageAnalysis,
    PaletteColor,
    analysis_from_decoded_image,
    decode_image,
    reduced_aspect_ratio,
)
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel

IMAGE_STRUCTURE_EXTRACTOR_NAME: Literal["adhelm-local-image-structure"] = (
    "adhelm-local-image-structure"
)
IMAGE_STRUCTURE_EXTRACTOR_VERSION: Literal["1"] = "1"
VIDEO_CONTAINER_EXTRACTOR_NAME: Literal["adhelm-local-video-container"] = (
    "adhelm-local-video-container"
)
VIDEO_CONTAINER_EXTRACTOR_VERSION: Literal["1"] = "1"

LAYOUT_GRID_ROWS = 4
LAYOUT_GRID_COLUMNS = 4
#: Every grid cell needs at least two rows and columns to hold one edge
#: comparison, so a smaller image reports an explicit unevaluated layout.
LAYOUT_MINIMUM_DIMENSION = 8
EDGE_LUMINANCE_THRESHOLD = 32
TEXT_HEURISTIC_VERSION: Literal["1"] = "1"
TEXT_LIKELY_THRESHOLD_BASIS_POINTS = 750
#: Integer Rec. 709 luminance weights scaled by ``LUMINANCE_SCALE``.
LUMINANCE_WEIGHTS = (2126, 7152, 722)
LUMINANCE_SCALE = 10_000

#: The layout pass is a second linear walk over the decoded pixels.  Above this
#: working budget the shared analysis fields are still reported and the layout is
#: explicitly not evaluated, rather than spending unbounded time on one image.
MAX_STRUCTURE_PIXELS = 4_000_000

MAX_VIDEO_CONTAINER_BYTES = 64 * 1024 * 1024
MAX_CONTAINER_ELEMENTS = 4096
#: Defensive only: every container walk in this module is structural, so no
#: well-formed or hostile input can reach it through open recursion.  It is
#: therefore a refusal guard and not part of ``parameters_digest``.
MAX_CONTAINER_DEPTH = 8
MAX_TIME_TO_SAMPLE_ENTRIES = 4096
MAX_DERIVED_FRAME_COUNT = 10_000_000
MAX_TRACKS = 64

#: Extracted values share the declared video domain in ``adhelm.video_creative``,
#: which imports these exact bounds.  A container claiming a fact outside the
#: product domain is refused; a clamped or truncated value would be a fabricated
#: measurement.
MAX_EXTRACTED_DIMENSION = 8_192
MAX_EXTRACTED_FRAME_RATE_MILLI = 240_000
MAX_EXTRACTED_DURATION_MS = 86_400_000
MAX_ISO_TIMESCALE = 4_294_967_295
MAX_MATROSKA_TIMESTAMP_SCALE = 1_000_000_000

#: Beyond these, a header is not a video description the product merely declines
#: to carry: no real container states them, so the file is malformed rather than
#: out of the product's domain.  Between a product bound and its structural
#: ceiling the file is structurally believable and simply outside the domain.
STRUCTURAL_MAX_DIMENSION = 16_384
STRUCTURAL_MAX_FRAME_RATE_MILLI = 1_000_000
STRUCTURAL_MAX_DURATION_MS = 7 * 86_400_000
STRUCTURAL_MAX_MATROSKA_TIMESTAMP_SCALE = 1_000_000_000_000

_ISO_BMFF_HEADER = 8
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"


def _image_parameters_digest() -> str:
    """Every parameter that can change a reported image value.

    The aspect-class cut points are repeated literally here because moving one
    would relabel retained records; ``_aspect_ratio_class`` holds the same
    ordered thresholds and ``tests/test_creative_extraction.py`` locks their
    exact boundary behaviour.
    """
    return digest(
        {
            "aspect_ratio_class_thresholds": [1600, 1050, 950, 625],
            "edge_luminance_threshold": EDGE_LUMINANCE_THRESHOLD,
            "grid_columns": LAYOUT_GRID_COLUMNS,
            "grid_rows": LAYOUT_GRID_ROWS,
            "layout_measured_in": "STORED_PIXEL_ORIENTATION",
            "layout_minimum_dimension": LAYOUT_MINIMUM_DIMENSION,
            "luminance_scale": LUMINANCE_SCALE,
            "luminance_weights": list(LUMINANCE_WEIGHTS),
            "max_structure_pixels": MAX_STRUCTURE_PIXELS,
            "text_heuristic_version": TEXT_HEURISTIC_VERSION,
            "text_likely_threshold_basis_points": TEXT_LIKELY_THRESHOLD_BASIS_POINTS,
        }
    )


def _video_parameters_digest() -> str:
    """Every parameter that can change a reported container value or its status."""
    return digest(
        {
            "matroska_default_timestamp_scale": _MATROSKA_DEFAULT_TIMESTAMP_SCALE,
            "max_container_bytes": MAX_VIDEO_CONTAINER_BYTES,
            "max_container_elements": MAX_CONTAINER_ELEMENTS,
            "max_derived_frame_count": MAX_DERIVED_FRAME_COUNT,
            "max_extracted_dimension": MAX_EXTRACTED_DIMENSION,
            "max_extracted_duration_ms": MAX_EXTRACTED_DURATION_MS,
            "max_extracted_frame_rate_milli": MAX_EXTRACTED_FRAME_RATE_MILLI,
            "max_iso_timescale": MAX_ISO_TIMESCALE,
            "max_matroska_timestamp_scale": MAX_MATROSKA_TIMESTAMP_SCALE,
            "max_time_to_sample_entries": MAX_TIME_TO_SAMPLE_ENTRIES,
            "max_tracks": MAX_TRACKS,
            "structural_max_dimension": STRUCTURAL_MAX_DIMENSION,
            "structural_max_duration_ms": STRUCTURAL_MAX_DURATION_MS,
            "structural_max_frame_rate_milli": STRUCTURAL_MAX_FRAME_RATE_MILLI,
            "structural_max_matroska_timestamp_scale": (STRUCTURAL_MAX_MATROSKA_TIMESTAMP_SCALE),
        }
    )


class LayoutCell(StrictModel):
    """One fixed coarse-grid region described only by aggregate integer measures."""

    row: int = Field(ge=0, le=LAYOUT_GRID_ROWS - 1)
    column: int = Field(ge=0, le=LAYOUT_GRID_COLUMNS - 1)
    mean_luminance_basis_points: int = Field(ge=0, le=10_000)
    luminance_stddev_basis_points: int = Field(ge=0, le=10_000)
    edge_density_basis_points: int = Field(ge=0, le=10_000)


class ImageStructureFeatures(StrictModel):
    """Deterministic image structure with explicit non-extraction statuses."""

    schema_version: Literal[1] = 1
    image_features_id: str = Field(pattern=r"^image-features:sha256:[a-f0-9]{64}$")
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=1)
    extractor_name: Literal["adhelm-local-image-structure"] = IMAGE_STRUCTURE_EXTRACTOR_NAME
    extractor_version: Literal["1"] = IMAGE_STRUCTURE_EXTRACTOR_VERSION
    parameters_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    analyzer_name: Literal["adhelm-local-image"]
    analyzer_version: Literal["1"]
    mime_type: Literal[
        "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"
    ]
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    aspect_ratio: str = Field(min_length=3, max_length=32)
    aspect_ratio_class: Literal[
        "PORTRAIT_TALL", "PORTRAIT", "SQUARE", "LANDSCAPE", "LANDSCAPE_WIDE"
    ]
    palette: tuple[PaletteColor, ...] = Field(min_length=1)
    mean_lightness_basis_points: int = Field(ge=0, le=10_000)
    mean_saturation_basis_points: int = Field(ge=0, le=10_000)
    contrast_basis_points: int = Field(ge=0, le=10_000)
    colorfulness_basis_points: int = Field(ge=0, le=10_000)
    exif_orientation: int | None = Field(default=None, ge=1, le=8)
    #: ``INVALID`` means the file stated an orientation outside the defined
    #: 1-8 range.  That is not the same fact as declaring none.
    exif_orientation_status: Literal["EXTRACTED", "NOT_PRESENT", "INVALID"]
    layout_status: Literal[
        "EXTRACTED", "NOT_EVALUATED_IMAGE_TOO_SMALL", "NOT_EVALUATED_ABOVE_STRUCTURE_BUDGET"
    ]
    #: The grid describes stored pixels.  An EXIF orientation tag is disclosed
    #: and never applied, so the measurement stays a fact about the bytes.
    layout_measured_in: Literal["STORED_PIXEL_ORIENTATION"] = "STORED_PIXEL_ORIENTATION"
    layout_grid_rows: Literal[4] = 4
    layout_grid_columns: Literal[4] = 4
    layout_cells: tuple[LayoutCell, ...] = Field(max_length=16)
    text_presence: Literal["TEXT_LIKELY", "TEXT_UNLIKELY", "NOT_EVALUATED"]
    text_presence_basis_points: int | None = Field(default=None, ge=0, le=10_000)
    text_heuristic_version: Literal["1"] = TEXT_HEURISTIC_VERSION
    extracted_text_status: Literal["NOT_EXTRACTED_NO_LOCAL_OCR"] = "NOT_EXTRACTED_NO_LOCAL_OCR"
    logo_presence_status: Literal["NOT_EVALUATED_NO_LOCAL_DETECTOR"] = (
        "NOT_EVALUATED_NO_LOCAL_DETECTOR"
    )
    face_presence_status: Literal["NOT_EVALUATED_NO_BIOMETRIC_INFERENCE"] = (
        "NOT_EVALUATED_NO_BIOMETRIC_INFERENCE"
    )
    identity_inference_status: Literal["NOT_EVALUATED_PROHIBITED"] = "NOT_EVALUATED_PROHIBITED"
    protected_trait_inference_status: Literal["NOT_EVALUATED_PROHIBITED"] = (
        "NOT_EVALUATED_PROHIBITED"
    )
    pixel_data_status: Literal["NOT_RETAINED"] = "NOT_RETAINED"
    provenance: Literal["LOCAL_EXTRACTED"] = "LOCAL_EXTRACTED"

    @model_validator(mode="after")
    def structure_is_consistent(self) -> ImageStructureFeatures:
        if self.aspect_ratio != reduced_aspect_ratio(self.width_px, self.height_px):
            raise ValueError("Image structure aspect ratio disagrees with its dimensions")
        if self.aspect_ratio_class != _aspect_ratio_class(self.width_px, self.height_px):
            raise ValueError("Image structure aspect class disagrees with its dimensions")
        expected_cells = LAYOUT_GRID_ROWS * LAYOUT_GRID_COLUMNS
        if self.layout_status == "EXTRACTED":
            if len(self.layout_cells) != expected_cells:
                raise ValueError("An extracted layout requires one cell per grid region")
            positions = [(cell.row, cell.column) for cell in self.layout_cells]
            if positions != [
                (row, column)
                for row in range(LAYOUT_GRID_ROWS)
                for column in range(LAYOUT_GRID_COLUMNS)
            ]:
                raise ValueError("Layout cells must be complete and row-major ordered")
            if self.text_presence == "NOT_EVALUATED" or self.text_presence_basis_points is None:
                raise ValueError("An extracted layout carries an evaluated text-presence result")
            expected_presence = (
                "TEXT_LIKELY"
                if self.text_presence_basis_points >= TEXT_LIKELY_THRESHOLD_BASIS_POINTS
                else "TEXT_UNLIKELY"
            )
            if self.text_presence != expected_presence:
                raise ValueError("Text presence disagrees with its heuristic score and threshold")
        else:
            if self.layout_cells:
                raise ValueError("An unevaluated layout cannot carry cells")
            if self.text_presence != "NOT_EVALUATED" or self.text_presence_basis_points is not None:
                raise ValueError("An unevaluated layout cannot carry a text-presence result")
        if (self.exif_orientation is not None) != (self.exif_orientation_status == "EXTRACTED"):
            raise ValueError("An orientation tag is present exactly when it was extracted")
        if self.parameters_digest != _image_parameters_digest():
            raise ValueError("Image structure parameters digest disagrees with this extractor")
        if self.image_features_id != _identity("image-features", self):
            raise ValueError("Image structure identifier does not match its extracted content")
        return self

    def to_image_analysis(self) -> ImageAnalysis:
        """Rebuild the exact shared analysis fields this record was derived from."""
        return ImageAnalysis(
            mime_type=self.mime_type,
            width_px=self.width_px,
            height_px=self.height_px,
            aspect_ratio=self.aspect_ratio,
            palette=self.palette,
            mean_lightness_basis_points=self.mean_lightness_basis_points,
            mean_saturation_basis_points=self.mean_saturation_basis_points,
            contrast_basis_points=self.contrast_basis_points,
            colorfulness_basis_points=self.colorfulness_basis_points,
        )


class AssetStructureRecord(StrictModel):
    """Sibling record binding image structure to an existing asset analysis.

    The record is deliberately a sibling instead of a new ``AssetAnalysisRecord``
    field: the retained analysis payload, its identity domain, and the creative
    history graph therefore stay byte-identical for existing callers.
    """

    schema_version: Literal[1] = 1
    asset_structure_id: str = Field(pattern=r"^asset-structure:sha256:[a-f0-9]{64}$")
    asset_analysis_ref: str = Field(pattern=r"^asset-analysis:sha256:[a-f0-9]{64}$")
    asset_version_ref: str = Field(pattern=r"^asset:sha256:[a-f0-9]{64}$")
    asset_content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    features: ImageStructureFeatures
    provenance: Literal["LOCAL_EXTRACTED"] = "LOCAL_EXTRACTED"

    @model_validator(mode="after")
    def structure_binds_its_analysis(self) -> AssetStructureRecord:
        if self.features.content_digest != self.asset_content_digest:
            raise ValueError("Asset structure must describe the exact analyzed asset content")
        if self.asset_structure_id != _identity("asset-structure", self):
            raise ValueError("Asset structure identifier does not match its bound content")
        return self

    @classmethod
    def from_asset_analysis(
        cls, analysis: AssetAnalysisRecord, features: ImageStructureFeatures
    ) -> AssetStructureRecord:
        if analysis.asset_content_digest != features.content_digest:
            raise AdhelmError(
                "ASSET_STRUCTURE_MISMATCH",
                "Structure features describe different asset content than the analysis.",
                exit_code=2,
            )
        if (
            analysis.mime_type != features.mime_type
            or analysis.width_px != features.width_px
            or analysis.height_px != features.height_px
            or analysis.palette != features.palette
            or analysis.mean_lightness_basis_points != features.mean_lightness_basis_points
            or analysis.mean_saturation_basis_points != features.mean_saturation_basis_points
            or analysis.contrast_basis_points != features.contrast_basis_points
            or analysis.colorfulness_basis_points != features.colorfulness_basis_points
        ):
            raise AdhelmError(
                "ASSET_STRUCTURE_MISMATCH",
                "Structure features disagree with the retained asset analysis.",
                exit_code=2,
            )
        return _with_identity(
            cls,
            "asset-structure",
            {
                "asset_analysis_ref": analysis.asset_analysis_id,
                "asset_version_ref": analysis.asset_version_id,
                "asset_content_digest": analysis.asset_content_digest,
                "features": features,
            },
        )


def extract_image_features(
    data: bytes, *, maximum_bytes: int = MAX_IMAGE_BYTES, maximum_pixels: int = MAX_IMAGE_PIXELS
) -> ImageStructureFeatures:
    """Extract deterministic structure from bounded local raster image bytes.

    The bytes are decoded exactly once: the shared analysis fields and the layout
    grid are both derived from that single materialized pixel list, so the
    extractor never costs more than one decode plus one linear pass.

    ``maximum_bytes`` and ``maximum_pixels`` only gate refusal.  They never
    change an extracted value, so they are excluded from ``parameters_digest``.
    """
    decoded = decode_image(data, maximum_bytes=maximum_bytes, maximum_pixels=maximum_pixels)
    analysis = analysis_from_decoded_image(decoded)
    width = decoded.width_px
    height = decoded.height_px
    layout_status: Literal[
        "EXTRACTED", "NOT_EVALUATED_IMAGE_TOO_SMALL", "NOT_EVALUATED_ABOVE_STRUCTURE_BUDGET"
    ]
    presence: Literal["TEXT_LIKELY", "TEXT_UNLIKELY", "NOT_EVALUATED"]
    cells: tuple[LayoutCell, ...] = ()
    presence_score: int | None = None
    presence = "NOT_EVALUATED"
    if width * height > MAX_STRUCTURE_PIXELS:
        layout_status = "NOT_EVALUATED_ABOVE_STRUCTURE_BUDGET"
    elif width < LAYOUT_MINIMUM_DIMENSION or height < LAYOUT_MINIMUM_DIMENSION:
        layout_status = "NOT_EVALUATED_IMAGE_TOO_SMALL"
    else:
        cells = _layout_cells(decoded.pixels, width=width, height=height)
        presence_score = max(
            min(cell.edge_density_basis_points, cell.luminance_stddev_basis_points)
            for cell in cells
        )
        layout_status = "EXTRACTED"
        presence = (
            "TEXT_LIKELY"
            if presence_score >= TEXT_LIKELY_THRESHOLD_BASIS_POINTS
            else "TEXT_UNLIKELY"
        )
    return _with_identity(
        ImageStructureFeatures,
        "image-features",
        {
            "content_digest": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
            "parameters_digest": _image_parameters_digest(),
            "analyzer_name": analysis.analyzer_name,
            "analyzer_version": analysis.analyzer_version,
            "mime_type": analysis.mime_type,
            "width_px": width,
            "height_px": height,
            "aspect_ratio": analysis.aspect_ratio,
            "aspect_ratio_class": _aspect_ratio_class(width, height),
            "palette": analysis.palette,
            "mean_lightness_basis_points": analysis.mean_lightness_basis_points,
            "mean_saturation_basis_points": analysis.mean_saturation_basis_points,
            "contrast_basis_points": analysis.contrast_basis_points,
            "colorfulness_basis_points": analysis.colorfulness_basis_points,
            "exif_orientation": decoded.exif_orientation,
            "exif_orientation_status": decoded.exif_orientation_status,
            "layout_status": layout_status,
            "layout_cells": cells,
            "text_presence": presence,
            "text_presence_basis_points": presence_score,
        },
    )


def extract_vault_image_features(root: Path, expected_digest: str) -> ImageStructureFeatures:
    """Extract structure from an image already retained in the private local vault."""
    from adhelm.assets import read_image

    return extract_image_features(read_image(root, expected_digest))


def _luminance_row(pixels: list[tuple[int, int, int]], *, base: int, width: int) -> list[int]:
    """Integer Rec. 709 luminance for one pixel row, with no platform float use."""
    red_weight, green_weight, blue_weight = LUMINANCE_WEIGHTS
    row: list[int] = []
    for index in range(base, base + width):
        red, green, blue = pixels[index]
        row.append(
            (red_weight * red + green_weight * green + blue_weight * blue) // LUMINANCE_SCALE
        )
    return row


def _layout_cells(
    pixels: list[tuple[int, int, int]], *, width: int, height: int
) -> tuple[LayoutCell, ...]:
    """Accumulate the fixed grid from one forward pass over two rows at a time."""
    counts = [0] * (LAYOUT_GRID_ROWS * LAYOUT_GRID_COLUMNS)
    totals = [0] * len(counts)
    squares = [0] * len(counts)
    comparisons = [0] * len(counts)
    edges = [0] * len(counts)
    columns = [x * LAYOUT_GRID_COLUMNS // width for x in range(width)]
    previous: list[int] | None = None
    for y in range(height):
        current = _luminance_row(pixels, base=y * width, width=width)
        row = y * LAYOUT_GRID_ROWS // height
        for x in range(width):
            index = row * LAYOUT_GRID_COLUMNS + columns[x]
            value = current[x]
            counts[index] += 1
            totals[index] += value
            squares[index] += value * value
            if x + 1 < width:
                comparisons[index] += 1
                if abs(value - current[x + 1]) >= EDGE_LUMINANCE_THRESHOLD:
                    edges[index] += 1
        if previous is not None:
            # The downward comparison belongs to the cell of the upper pixel,
            # exactly as a whole-image luminance map would attribute it.
            above = (y - 1) * LAYOUT_GRID_ROWS // height
            for x in range(width):
                index = above * LAYOUT_GRID_COLUMNS + columns[x]
                comparisons[index] += 1
                if abs(previous[x] - current[x]) >= EDGE_LUMINANCE_THRESHOLD:
                    edges[index] += 1
        previous = current
    cells: list[LayoutCell] = []
    for row in range(LAYOUT_GRID_ROWS):
        for column in range(LAYOUT_GRID_COLUMNS):
            index = row * LAYOUT_GRID_COLUMNS + column
            count = counts[index]
            if count == 0 or comparisons[index] == 0:
                raise AdhelmError(
                    "CREATIVE_LAYOUT_UNAVAILABLE",
                    "Image dimensions do not support the fixed layout grid.",
                    exit_code=2,
                )
            # ``variance * count**2`` stays an exact integer, so the floor-rounded
            # basis points below never depend on floating-point rounding.
            scaled_variance = squares[index] * count - totals[index] * totals[index]
            cells.append(
                LayoutCell(
                    row=row,
                    column=column,
                    mean_luminance_basis_points=min(
                        10_000, totals[index] * 10_000 // (count * 255)
                    ),
                    luminance_stddev_basis_points=min(
                        10_000, math.isqrt(scaled_variance * 10_000**2 // 255**2) // count
                    ),
                    edge_density_basis_points=min(
                        10_000, edges[index] * 10_000 // comparisons[index]
                    ),
                )
            )
    return tuple(cells)


def _aspect_ratio_class(
    width_px: int, height_px: int
) -> Literal["PORTRAIT_TALL", "PORTRAIT", "SQUARE", "LANDSCAPE", "LANDSCAPE_WIDE"]:
    ratio = width_px * 1000 // height_px
    if ratio >= 1600:
        return "LANDSCAPE_WIDE"
    if ratio >= 1050:
        return "LANDSCAPE"
    if ratio >= 950:
        return "SQUARE"
    if ratio >= 625:
        return "PORTRAIT"
    return "PORTRAIT_TALL"


class VideoContainerMetadata(StrictModel):
    """Container-level video facts with explicit statuses for everything absent."""

    schema_version: Literal[1] = 1
    video_metadata_id: str = Field(pattern=r"^video-container:sha256:[a-f0-9]{64}$")
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=1, le=MAX_VIDEO_CONTAINER_BYTES)
    extractor_name: Literal["adhelm-local-video-container"] = VIDEO_CONTAINER_EXTRACTOR_NAME
    extractor_version: Literal["1"] = VIDEO_CONTAINER_EXTRACTOR_VERSION
    parameters_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    container_kind: Literal["ISO_BMFF", "MATROSKA"]
    container_brand: str = Field(min_length=1, max_length=32)
    timescale: int | None = Field(default=None, ge=1, le=MAX_ISO_TIMESCALE)
    timescale_unit: Literal["TICKS_PER_SECOND", "NANOSECONDS_PER_TICK"] | None = None
    #: ``DEFAULTED_BY_SPECIFICATION`` means the container omitted the value and
    #: the format defines it; the number is real but it was never extracted.
    timescale_status: Literal[
        "EXTRACTED", "DEFAULTED_BY_SPECIFICATION", "NOT_DOCUMENTED_IN_CONTAINER"
    ]
    duration_ms: int | None = Field(default=None, ge=0, le=MAX_EXTRACTED_DURATION_MS)
    duration_status: Literal["EXTRACTED", "NOT_DOCUMENTED_IN_CONTAINER"]
    video_track_status: Literal["EXTRACTED", "NOT_PRESENT"]
    video_track_count: int = Field(ge=0, le=MAX_TRACKS)
    #: A container may declare several video tracks.  The extractor reads the
    #: first declared one and always discloses that choice; it never silently
    #: drops the rest.
    video_track_selection_status: Literal[
        "SINGLE_VIDEO_TRACK", "FIRST_DECLARED_OF_MULTIPLE", "NOT_APPLICABLE_NO_VIDEO_TRACK"
    ]
    audio_track_count: int = Field(ge=0, le=MAX_TRACKS)
    video_width_px: int | None = Field(default=None, ge=1, le=MAX_EXTRACTED_DIMENSION)
    video_height_px: int | None = Field(default=None, ge=1, le=MAX_EXTRACTED_DIMENSION)
    video_dimensions_status: Literal[
        "EXTRACTED", "NOT_DOCUMENTED_IN_CONTAINER", "NOT_APPLICABLE_NO_VIDEO_TRACK"
    ]
    video_dimensions_source: (
        Literal[
            "ISO_BMFF_SAMPLE_DESCRIPTION",
            "ISO_BMFF_TRACK_HEADER",
            "MATROSKA_TRACK_VIDEO",
        ]
        | None
    ) = None
    frame_rate_milli: int | None = Field(default=None, ge=1, le=MAX_EXTRACTED_FRAME_RATE_MILLI)
    frame_rate_status: Literal[
        "EXTRACTED",
        "NOT_DOCUMENTED_IN_CONTAINER",
        "VARIABLE_NOT_CONSTANT",
        "NOT_APPLICABLE_NO_VIDEO_TRACK",
    ]
    frame_count: int | None = Field(default=None, ge=0, le=MAX_DERIVED_FRAME_COUNT)
    frame_count_status: Literal[
        "EXTRACTED", "NOT_DERIVABLE_FROM_CONTAINER", "NOT_APPLICABLE_NO_VIDEO_TRACK"
    ]
    audio_track_status: Literal["DECLARED_PRESENT", "DECLARED_ABSENT"]
    decoded_frame_status: Literal["NOT_DECODED_NO_LOCAL_DECODER"] = "NOT_DECODED_NO_LOCAL_DECODER"
    palette_status: Literal["NOT_ANALYZED_NO_VIDEO_DECODE"] = "NOT_ANALYZED_NO_VIDEO_DECODE"
    scene_status: Literal["NOT_ANALYZED_NO_VIDEO_DECODE"] = "NOT_ANALYZED_NO_VIDEO_DECODE"
    motion_status: Literal["NOT_ANALYZED_NO_VIDEO_DECODE"] = "NOT_ANALYZED_NO_VIDEO_DECODE"
    on_visual_text_status: Literal["NOT_EXTRACTED_NO_LOCAL_OCR"] = "NOT_EXTRACTED_NO_LOCAL_OCR"
    transcript_status: Literal["NOT_EXTRACTED_NO_LOCAL_TRANSCRIPTION"] = (
        "NOT_EXTRACTED_NO_LOCAL_TRANSCRIPTION"
    )
    audio_analysis_status: Literal["NOT_ANALYZED_NO_AUDIO_EXTRACTION"] = (
        "NOT_ANALYZED_NO_AUDIO_EXTRACTION"
    )
    face_presence_status: Literal["NOT_EVALUATED_NO_BIOMETRIC_INFERENCE"] = (
        "NOT_EVALUATED_NO_BIOMETRIC_INFERENCE"
    )
    identity_inference_status: Literal["NOT_EVALUATED_PROHIBITED"] = "NOT_EVALUATED_PROHIBITED"
    protected_trait_inference_status: Literal["NOT_EVALUATED_PROHIBITED"] = (
        "NOT_EVALUATED_PROHIBITED"
    )
    component_performance_status: Literal["NOT_INFERRED"] = "NOT_INFERRED"
    media_bytes_status: Literal["NOT_RETAINED"] = "NOT_RETAINED"
    provenance: Literal["LOCAL_EXTRACTED"] = "LOCAL_EXTRACTED"

    @model_validator(mode="after")
    def container_statuses_are_consistent(self) -> VideoContainerMetadata:
        documented = ("EXTRACTED", "DEFAULTED_BY_SPECIFICATION")
        pairs: tuple[tuple[object, str, tuple[str, ...]], ...] = (
            (self.timescale, self.timescale_status, documented),
            (self.timescale_unit, self.timescale_status, documented),
            (self.duration_ms, self.duration_status, ("EXTRACTED",)),
            (self.video_width_px, self.video_dimensions_status, ("EXTRACTED",)),
            (self.video_height_px, self.video_dimensions_status, ("EXTRACTED",)),
            (self.video_dimensions_source, self.video_dimensions_status, ("EXTRACTED",)),
            (self.frame_rate_milli, self.frame_rate_status, ("EXTRACTED",)),
            (self.frame_count, self.frame_count_status, ("EXTRACTED",)),
        )
        for value, status, extracted in pairs:
            if (value is not None) != (status in extracted):
                raise ValueError("A container value must be present exactly when it is extracted")
        if self.video_track_status == "NOT_PRESENT":
            if (
                self.video_dimensions_status != "NOT_APPLICABLE_NO_VIDEO_TRACK"
                or self.frame_rate_status != "NOT_APPLICABLE_NO_VIDEO_TRACK"
                or self.frame_count_status != "NOT_APPLICABLE_NO_VIDEO_TRACK"
                or self.video_track_selection_status != "NOT_APPLICABLE_NO_VIDEO_TRACK"
                or self.video_track_count != 0
            ):
                raise ValueError("A container without a video track cannot report video facts")
        elif "NOT_APPLICABLE_NO_VIDEO_TRACK" in (
            self.video_dimensions_status,
            self.frame_rate_status,
            self.frame_count_status,
            self.video_track_selection_status,
        ):
            raise ValueError("A container with a video track cannot report it as inapplicable")
        elif self.video_track_selection_status != (
            "SINGLE_VIDEO_TRACK" if self.video_track_count == 1 else "FIRST_DECLARED_OF_MULTIPLE"
        ):
            raise ValueError("Video track selection must follow the declared video track count")
        if (self.audio_track_count > 0) != (self.audio_track_status == "DECLARED_PRESENT"):
            raise ValueError("Audio presence must follow the declared audio track count")
        if self.container_kind == "ISO_BMFF" and self.timescale_unit not in (
            None,
            "TICKS_PER_SECOND",
        ):
            raise ValueError("ISO base media timescales are ticks per second")
        if self.container_kind == "MATROSKA" and self.timescale_unit not in (
            None,
            "NANOSECONDS_PER_TICK",
        ):
            raise ValueError("Matroska timestamp scales are nanoseconds per tick")
        if self.parameters_digest != _video_parameters_digest():
            raise ValueError("Container parameters digest disagrees with this extractor")
        if self.video_metadata_id != _identity("video-container", self):
            raise ValueError("Container identifier does not match its extracted content")
        return self


def extract_video_metadata(data: bytes) -> VideoContainerMetadata:
    """Parse bounded container metadata from local video bytes without decoding."""
    if not 1 <= len(data) <= MAX_VIDEO_CONTAINER_BYTES:
        raise AdhelmError(
            "CREATIVE_VIDEO_TOO_LARGE",
            "Video bytes are empty or exceed the configured container bound.",
            exit_code=2,
        )
    if data[:4] == _EBML_MAGIC:
        observed = _matroska_metadata(data)
    elif len(data) >= _ISO_BMFF_HEADER and data[4:8] == b"ftyp":
        observed = _iso_bmff_metadata(data)
    else:
        raise AdhelmError(
            "UNSUPPORTED_VIDEO_CONTAINER",
            "Local video bytes are not a supported ISO base media or Matroska container.",
            exit_code=2,
        )
    return _with_identity(
        VideoContainerMetadata,
        "video-container",
        {
            "content_digest": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
            "parameters_digest": _video_parameters_digest(),
            **observed,
        },
    )


class _Budget:
    """Bounded parsing budget shared by every nested container walk."""

    def __init__(self) -> None:
        self.remaining = MAX_CONTAINER_ELEMENTS

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise _too_complex()


def _malformed() -> AdhelmError:
    return AdhelmError(
        "VIDEO_CONTAINER_MALFORMED",
        "Local video container structure is invalid.",
        exit_code=2,
    )


def _truncated() -> AdhelmError:
    return AdhelmError(
        "VIDEO_CONTAINER_TRUNCATED",
        "Local video container declares content beyond the supplied bytes.",
        exit_code=2,
    )


def _out_of_domain() -> AdhelmError:
    return AdhelmError(
        "VIDEO_CONTAINER_OUT_OF_DOMAIN",
        "Local video container declares a value outside the supported video domain.",
        exit_code=2,
    )


def _too_complex() -> AdhelmError:
    return AdhelmError(
        "VIDEO_CONTAINER_TOO_COMPLEX",
        "Local video container exceeds the bounded parsing limits.",
        exit_code=2,
    )


def _boxes(
    data: bytes, start: int, end: int, *, budget: _Budget, depth: int
) -> list[tuple[bytes, int, int]]:
    if depth > MAX_CONTAINER_DEPTH:
        raise _too_complex()
    result: list[tuple[bytes, int, int]] = []
    offset = start
    while offset < end:
        if end - offset < _ISO_BMFF_HEADER:
            raise _malformed()
        size = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        header = _ISO_BMFF_HEADER
        if size == 1:
            if end - offset < 16:
                raise _malformed()
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header = 16
            if size < 16:
                raise _malformed()
        elif size == 0:
            # ISO/IEC 14496-12 permits size 0 only for the last box in the file.
            # Honouring it inside a parent would let one box swallow its siblings
            # and turn a malformed structure into a confident absence claim.
            if depth != 0:
                raise _malformed()
            size = end - offset
        elif size < _ISO_BMFF_HEADER:
            raise _malformed()
        if size > end - offset:
            raise _truncated()
        budget.spend()
        result.append((kind, offset + header, offset + size))
        offset += size
    return result


def _only(boxes: list[tuple[bytes, int, int]], kind: bytes) -> tuple[int, int] | None:
    """Select a structure that must not be declared twice."""
    found = [item for item in boxes if item[0] == kind]
    if len(found) > 1:
        raise _malformed()
    return None if not found else (found[0][1], found[0][2])


def _in_domain(value: int, maximum: int, structural: int) -> int:
    """Refuse a container value outside the declared advertising video domain.

    A value a real container could state, but that the product does not carry,
    is ``VIDEO_CONTAINER_OUT_OF_DOMAIN``.  A value past the structural ceiling is
    ``VIDEO_CONTAINER_MALFORMED``, because no container describing video states
    it.  Neither is ever clamped into range.
    """
    if value < 1 or value > structural:
        raise _malformed()
    if value > maximum:
        raise _out_of_domain()
    return value


def _iso_bmff_metadata(data: bytes) -> dict[str, Any]:
    budget = _Budget()
    top = _boxes(data, 0, len(data), budget=budget, depth=0)
    if not top or top[0][0] != b"ftyp":
        raise _malformed()
    brand_start, brand_end = top[0][1], top[0][2]
    if brand_end - brand_start < 4:
        raise _malformed()
    brand = data[brand_start : brand_start + 4]
    if not brand.isascii() or not brand.decode("ascii").isprintable():
        raise _malformed()
    movie = _only(top, b"moov")
    if movie is None:
        raise _malformed()
    children = _boxes(data, movie[0], movie[1], budget=budget, depth=1)
    timescale, duration_ms = _movie_header(data, children)
    tracks = [item for item in children if item[0] == b"trak"]
    if len(tracks) > MAX_TRACKS:
        raise _too_complex()
    video: dict[str, Any] | None = None
    video_tracks = 0
    audio_tracks = 0
    for _, start, end in tracks:
        track = _iso_track(data, start, end, budget=budget)
        if track["handler"] == b"soun":
            audio_tracks += 1
        elif track["handler"] == b"vide":
            video_tracks += 1
            if video is None:
                video = track
    observed: dict[str, Any] = {
        "container_kind": "ISO_BMFF",
        "container_brand": brand.decode("ascii"),
        "timescale": timescale,
        "timescale_unit": None if timescale is None else "TICKS_PER_SECOND",
        "timescale_status": "NOT_DOCUMENTED_IN_CONTAINER" if timescale is None else "EXTRACTED",
        "duration_ms": duration_ms,
        "duration_status": "NOT_DOCUMENTED_IN_CONTAINER" if duration_ms is None else "EXTRACTED",
        "audio_track_count": audio_tracks,
        "audio_track_status": "DECLARED_PRESENT" if audio_tracks else "DECLARED_ABSENT",
        "video_track_count": video_tracks,
    }
    observed.update(_video_selection(video, video_tracks))
    return observed


def _video_selection(track: dict[str, Any] | None, video_tracks: int) -> dict[str, Any]:
    """Disclose the selected video track, or the absence of one, without guessing."""
    if track is None:
        return {
            "video_track_status": "NOT_PRESENT",
            "video_track_selection_status": "NOT_APPLICABLE_NO_VIDEO_TRACK",
            "video_dimensions_status": "NOT_APPLICABLE_NO_VIDEO_TRACK",
            "frame_rate_status": "NOT_APPLICABLE_NO_VIDEO_TRACK",
            "frame_count_status": "NOT_APPLICABLE_NO_VIDEO_TRACK",
        }
    fields: dict[str, Any] = {
        "video_track_status": "EXTRACTED",
        "video_track_selection_status": (
            "SINGLE_VIDEO_TRACK" if video_tracks == 1 else "FIRST_DECLARED_OF_MULTIPLE"
        ),
    }
    fields.update(_video_track_fields(track))
    return fields


def _video_track_fields(track: dict[str, Any]) -> dict[str, Any]:
    width = track["sample_width"] or track["header_width"]
    height = track["sample_height"] or track["header_height"]
    source = (
        "ISO_BMFF_SAMPLE_DESCRIPTION"
        if track["sample_width"] and track["sample_height"]
        else "ISO_BMFF_TRACK_HEADER"
    )
    fields: dict[str, Any] = {}
    if width and height:
        fields.update(
            video_width_px=_in_domain(width, MAX_EXTRACTED_DIMENSION, STRUCTURAL_MAX_DIMENSION),
            video_height_px=_in_domain(height, MAX_EXTRACTED_DIMENSION, STRUCTURAL_MAX_DIMENSION),
            video_dimensions_status="EXTRACTED",
            video_dimensions_source=source,
        )
    else:
        fields.update(video_dimensions_status="NOT_DOCUMENTED_IN_CONTAINER")
    rate = track["frame_rate_milli"]
    if track["variable_rate"]:
        fields.update(frame_rate_status="VARIABLE_NOT_CONSTANT")
    elif rate is None:
        fields.update(frame_rate_status="NOT_DOCUMENTED_IN_CONTAINER")
    else:
        fields.update(
            frame_rate_milli=_in_domain(
                rate, MAX_EXTRACTED_FRAME_RATE_MILLI, STRUCTURAL_MAX_FRAME_RATE_MILLI
            ),
            frame_rate_status="EXTRACTED",
        )
    count = track["frame_count"]
    if count is None:
        fields.update(frame_count_status="NOT_DERIVABLE_FROM_CONTAINER")
    else:
        fields.update(frame_count=count, frame_count_status="EXTRACTED")
    return fields


def _movie_header(
    data: bytes, children: list[tuple[bytes, int, int]]
) -> tuple[int | None, int | None]:
    header = _only(children, b"mvhd")
    if header is None:
        raise _malformed()
    start, end = header
    version, payload = _full_box(data, start, end)
    if version == 0:
        required, timescale_at, duration_at, width = 16, 8, 12, 4
    elif version == 1:
        required, timescale_at, duration_at, width = 28, 16, 20, 8
    else:
        raise _malformed()
    if len(payload) < required:
        raise _malformed()
    timescale = int.from_bytes(payload[timescale_at : timescale_at + 4], "big")
    duration = int.from_bytes(payload[duration_at : duration_at + width], "big")
    # A zero movie timescale makes every declared movie time meaningless, so the
    # container is malformed rather than a movie with unknown timing.
    _in_domain(timescale, MAX_ISO_TIMESCALE, MAX_ISO_TIMESCALE)
    if duration in (0, (1 << (8 * width)) - 1):
        return timescale, None
    return timescale, _bounded_duration(duration * 1000 // timescale)


def _bounded_duration(duration_ms: int) -> int:
    if duration_ms < 0 or duration_ms > STRUCTURAL_MAX_DURATION_MS:
        raise _malformed()
    if duration_ms > MAX_EXTRACTED_DURATION_MS:
        raise _out_of_domain()
    return duration_ms


def _full_box(data: bytes, start: int, end: int) -> tuple[int, bytes]:
    if end - start < 4:
        raise _malformed()
    return data[start], data[start + 4 : end]


def _iso_track(data: bytes, start: int, end: int, *, budget: _Budget) -> dict[str, Any]:
    children = _boxes(data, start, end, budget=budget, depth=2)
    header_width, header_height = _track_header(data, children)
    media = _only(children, b"mdia")
    handler = b""
    timescale = 0
    sample_width = 0
    sample_height = 0
    frame_rate: int | None = None
    frame_count: int | None = None
    variable_rate = False
    if media is not None:
        media_children = _boxes(data, media[0], media[1], budget=budget, depth=3)
        handler = _track_handler(data, media_children)
        timescale = _media_timescale(data, media_children)
        information = _only(media_children, b"minf")
        if information is not None:
            table = _only(
                _boxes(data, information[0], information[1], budget=budget, depth=4), b"stbl"
            )
            if table is not None:
                samples = _boxes(data, table[0], table[1], budget=budget, depth=5)
                sample_width, sample_height = _sample_dimensions(data, samples)
                frame_count, frame_rate, variable_rate = _time_to_sample(data, samples, timescale)
    return {
        "handler": handler,
        "header_width": header_width,
        "header_height": header_height,
        "sample_width": sample_width,
        "sample_height": sample_height,
        "frame_count": frame_count,
        "frame_rate_milli": frame_rate,
        "variable_rate": variable_rate,
    }


def _track_header(data: bytes, children: list[tuple[bytes, int, int]]) -> tuple[int, int]:
    header = _only(children, b"tkhd")
    if header is None:
        return 0, 0
    version, payload = _full_box(data, header[0], header[1])
    if version == 0:
        width_at = 72
    elif version == 1:
        width_at = 84
    else:
        raise _malformed()
    if len(payload) < width_at + 8:
        raise _malformed()
    width = int.from_bytes(payload[width_at : width_at + 4], "big") >> 16
    height = int.from_bytes(payload[width_at + 4 : width_at + 8], "big") >> 16
    return width, height


def _track_handler(data: bytes, children: list[tuple[bytes, int, int]]) -> bytes:
    handler = _only(children, b"hdlr")
    if handler is None:
        return b""
    _, payload = _full_box(data, handler[0], handler[1])
    if len(payload) < 8:
        raise _malformed()
    return payload[4:8]


def _media_timescale(data: bytes, children: list[tuple[bytes, int, int]]) -> int:
    header = _only(children, b"mdhd")
    if header is None:
        return 0
    version, payload = _full_box(data, header[0], header[1])
    if version == 0:
        required, timescale_at = 16, 8
    elif version == 1:
        required, timescale_at = 28, 16
    else:
        raise _malformed()
    if len(payload) < required:
        raise _malformed()
    return int.from_bytes(payload[timescale_at : timescale_at + 4], "big")


def _sample_dimensions(data: bytes, children: list[tuple[bytes, int, int]]) -> tuple[int, int]:
    description = _only(children, b"stsd")
    if description is None:
        return 0, 0
    _, payload = _full_box(data, description[0], description[1])
    if len(payload) < 4:
        raise _malformed()
    if int.from_bytes(payload[:4], "big") == 0:
        return 0, 0
    entry = payload[4:]
    if len(entry) < 8:
        raise _malformed()
    entry_size = int.from_bytes(entry[:4], "big")
    if entry_size < 8 or entry_size > len(entry):
        raise _truncated()
    if entry_size < 36:
        return 0, 0
    return (
        int.from_bytes(entry[32:34], "big"),
        int.from_bytes(entry[34:36], "big"),
    )


def _time_to_sample(
    data: bytes, children: list[tuple[bytes, int, int]], timescale: int
) -> tuple[int | None, int | None, bool]:
    table = _only(children, b"stts")
    if table is None:
        return None, None, False
    _, payload = _full_box(data, table[0], table[1])
    if len(payload) < 4:
        raise _malformed()
    entries = int.from_bytes(payload[:4], "big")
    if entries > MAX_TIME_TO_SAMPLE_ENTRIES:
        raise _too_complex()
    if len(payload) < 4 + entries * 8:
        raise _truncated()
    total = 0
    deltas: set[int] = set()
    for index in range(entries):
        at = 4 + index * 8
        samples = int.from_bytes(payload[at : at + 4], "big")
        delta = int.from_bytes(payload[at + 4 : at + 8], "big")
        total += samples
        if samples:
            deltas.add(delta)
    # An uncountable sample total drops only the count.  A variable-rate signal
    # is an observed fact about the table and survives that bound.
    count: int | None = None if total > MAX_DERIVED_FRAME_COUNT else total
    if len(deltas) > 1:
        return count, None, True
    if not deltas:
        return count, None, False
    delta = deltas.pop()
    if delta == 0 or timescale == 0:
        return count, None, False
    rate = timescale * 1000 // delta
    return count, (rate if rate >= 1 else None), False


_EBML_HEADER_ID = b"\x1a\x45\xdf\xa3"
_DOC_TYPE_ID = b"\x42\x82"
_SEGMENT_ID = b"\x18\x53\x80\x67"
_INFO_ID = b"\x15\x49\xa9\x66"
_TIMESTAMP_SCALE_ID = b"\x2a\xd7\xb1"
_DURATION_ID = b"\x44\x89"
_TRACKS_ID = b"\x16\x54\xae\x6b"
_TRACK_ENTRY_ID = b"\xae"
_TRACK_TYPE_ID = b"\x83"
_DEFAULT_DURATION_ID = b"\x23\xe3\x83"
_VIDEO_ID = b"\xe0"
_PIXEL_WIDTH_ID = b"\xb0"
_PIXEL_HEIGHT_ID = b"\xba"
_MATROSKA_DEFAULT_TIMESTAMP_SCALE = 1_000_000


def _ebml_vint(data: bytes, offset: int, end: int, *, keep_marker: bool) -> tuple[bytes, int, int]:
    if offset >= end:
        raise _malformed()
    first = data[offset]
    if first == 0:
        raise _malformed()
    length = 9 - first.bit_length()
    if length > (4 if keep_marker else 8) or offset + length > end:
        raise _malformed()
    raw = data[offset : offset + length]
    if keep_marker:
        return raw, 0, offset + length
    value = first & (0xFF >> length)
    unknown = value == (0xFF >> length)
    for byte in raw[1:]:
        value = (value << 8) | byte
        unknown = unknown and byte == 0xFF
    return raw, (-1 if unknown else value), offset + length


def _elements(
    data: bytes, start: int, end: int, *, budget: _Budget, depth: int
) -> list[tuple[bytes, int, int]]:
    if depth > MAX_CONTAINER_DEPTH:
        raise _too_complex()
    result: list[tuple[bytes, int, int]] = []
    offset = start
    while offset < end:
        identifier, _, after_id = _ebml_vint(data, offset, end, keep_marker=True)
        _, size, after_size = _ebml_vint(data, after_id, end, keep_marker=False)
        if size < 0:
            if identifier != _SEGMENT_ID:
                raise _malformed()
            size = end - after_size
        if size > end - after_size:
            raise _truncated()
        budget.spend()
        result.append((identifier, after_size, after_size + size))
        offset = after_size + size
    return result


def _ebml_uint(data: bytes, start: int, end: int) -> int:
    # Matroska encodes an unsigned integer in zero to eight bytes; an empty
    # element is the value zero, not a malformed one.
    if not 0 <= end - start <= 8:
        raise _malformed()
    return int.from_bytes(data[start:end], "big")


def _ebml_float(data: bytes, start: int, end: int) -> float:
    width = end - start
    if width == 0:
        return 0.0
    if width == 4:
        value = float(struct.unpack(">f", data[start:end])[0])
    elif width == 8:
        value = float(struct.unpack(">d", data[start:end])[0])
    else:
        raise _malformed()
    if not math.isfinite(value) or value < 0:
        raise _malformed()
    return value


def _matroska_metadata(data: bytes) -> dict[str, Any]:
    budget = _Budget()
    top = _elements(data, 0, len(data), budget=budget, depth=0)
    if not top or top[0][0] != _EBML_HEADER_ID:
        raise _malformed()
    header = _elements(data, top[0][1], top[0][2], budget=budget, depth=1)
    doc_type_element = _only(header, _DOC_TYPE_ID)
    doc_type = "matroska"
    if doc_type_element is not None:
        raw = data[doc_type_element[0] : doc_type_element[1]].rstrip(b"\x00")
        if not raw or len(raw) > 32 or not raw.isascii() or not raw.decode().isprintable():
            raise _malformed()
        doc_type = raw.decode("ascii")
    segment = _only(top, _SEGMENT_ID)
    if segment is None:
        raise _malformed()
    children = _elements(data, segment[0], segment[1], budget=budget, depth=1)
    timescale, timescale_declared, duration_ms = _matroska_info(data, children, budget=budget)
    video, video_tracks, audio_tracks = _matroska_tracks(data, children, budget=budget)
    observed: dict[str, Any] = {
        "container_kind": "MATROSKA",
        "container_brand": doc_type,
        "timescale": timescale,
        "timescale_unit": "NANOSECONDS_PER_TICK",
        "timescale_status": ("EXTRACTED" if timescale_declared else "DEFAULTED_BY_SPECIFICATION"),
        "duration_ms": duration_ms,
        "duration_status": "NOT_DOCUMENTED_IN_CONTAINER" if duration_ms is None else "EXTRACTED",
        "audio_track_count": audio_tracks,
        "audio_track_status": "DECLARED_PRESENT" if audio_tracks else "DECLARED_ABSENT",
        "video_track_count": video_tracks,
        "frame_count_status": "NOT_DERIVABLE_FROM_CONTAINER",
    }
    if video is None:
        observed.update(
            video_track_status="NOT_PRESENT",
            video_track_selection_status="NOT_APPLICABLE_NO_VIDEO_TRACK",
            video_dimensions_status="NOT_APPLICABLE_NO_VIDEO_TRACK",
            frame_rate_status="NOT_APPLICABLE_NO_VIDEO_TRACK",
            frame_count_status="NOT_APPLICABLE_NO_VIDEO_TRACK",
        )
        return observed
    observed["video_track_status"] = "EXTRACTED"
    observed["video_track_selection_status"] = (
        "SINGLE_VIDEO_TRACK" if video_tracks == 1 else "FIRST_DECLARED_OF_MULTIPLE"
    )
    if video["width"] and video["height"]:
        observed.update(
            video_width_px=_in_domain(
                video["width"], MAX_EXTRACTED_DIMENSION, STRUCTURAL_MAX_DIMENSION
            ),
            video_height_px=_in_domain(
                video["height"], MAX_EXTRACTED_DIMENSION, STRUCTURAL_MAX_DIMENSION
            ),
            video_dimensions_status="EXTRACTED",
            video_dimensions_source="MATROSKA_TRACK_VIDEO",
        )
    else:
        observed["video_dimensions_status"] = "NOT_DOCUMENTED_IN_CONTAINER"
    frame_duration = video["default_duration"]
    if frame_duration:
        observed.update(
            frame_rate_milli=_in_domain(
                1_000_000_000_000 // frame_duration,
                MAX_EXTRACTED_FRAME_RATE_MILLI,
                STRUCTURAL_MAX_FRAME_RATE_MILLI,
            ),
            frame_rate_status="EXTRACTED",
        )
    else:
        observed["frame_rate_status"] = "NOT_DOCUMENTED_IN_CONTAINER"
    return observed


def _matroska_info(
    data: bytes, children: list[tuple[bytes, int, int]], *, budget: _Budget
) -> tuple[int, bool, int | None]:
    """Return the timestamp scale, whether the container declared it, and duration."""
    information = _only(children, _INFO_ID)
    if information is None:
        raise _malformed()
    timescale = _MATROSKA_DEFAULT_TIMESTAMP_SCALE
    declared = False
    ticks: float | None = None
    for identifier, start, end in _elements(
        data, information[0], information[1], budget=budget, depth=2
    ):
        if identifier == _TIMESTAMP_SCALE_ID:
            if declared:
                raise _malformed()
            timescale = _ebml_uint(data, start, end)
            declared = True
        elif identifier == _DURATION_ID:
            if ticks is not None:
                raise _malformed()
            ticks = _ebml_float(data, start, end)
    _in_domain(timescale, MAX_MATROSKA_TIMESTAMP_SCALE, STRUCTURAL_MAX_MATROSKA_TIMESTAMP_SCALE)
    # Matroska requires a positive Duration.  A zero, including the zero a
    # zero-width float encodes, is an absent duration exactly as a zero ``mvhd``
    # duration is, never an extracted zero-length movie.
    if ticks is None or ticks <= 0:
        return timescale, declared, None
    return timescale, declared, _bounded_duration(int(ticks * timescale) // 1_000_000)


def _optional_uint(data: bytes, children: list[tuple[bytes, int, int]], identifier: bytes) -> int:
    """Read an exactly-once unsigned element, or zero when it is absent."""
    found = _only(children, identifier)
    if found is None:
        return 0
    return _ebml_uint(data, found[0], found[1])


def _matroska_tracks(
    data: bytes, children: list[tuple[bytes, int, int]], *, budget: _Budget
) -> tuple[dict[str, int] | None, int, int]:
    tracks = _only(children, _TRACKS_ID)
    if tracks is None:
        return None, 0, 0
    entries = [
        item
        for item in _elements(data, tracks[0], tracks[1], budget=budget, depth=2)
        if item[0] == _TRACK_ENTRY_ID
    ]
    if len(entries) > MAX_TRACKS:
        raise _too_complex()
    video: dict[str, int] | None = None
    video_tracks = 0
    audio_tracks = 0
    for _, start, end in entries:
        entry = _elements(data, start, end, budget=budget, depth=3)
        kind = _optional_uint(data, entry, _TRACK_TYPE_ID)
        default_duration = _optional_uint(data, entry, _DEFAULT_DURATION_ID)
        width = 0
        height = 0
        picture = _only(entry, _VIDEO_ID)
        if picture is not None:
            sizes = _elements(data, picture[0], picture[1], budget=budget, depth=4)
            width = _optional_uint(data, sizes, _PIXEL_WIDTH_ID)
            height = _optional_uint(data, sizes, _PIXEL_HEIGHT_ID)
        if kind == 2:
            audio_tracks += 1
        elif kind == 1:
            video_tracks += 1
            if video is None:
                video = {"width": width, "height": height, "default_duration": default_duration}
    return video, video_tracks, audio_tracks


_IDENTITY_FIELDS = {
    "image-features": "image_features_id",
    "asset-structure": "asset_structure_id",
    "video-container": "video_metadata_id",
}


def _identity(prefix: str, model: StrictModel) -> str:
    """Derive a content identity from every stored field except the identity."""
    payload = model.model_dump(mode="json")
    payload.pop(_IDENTITY_FIELDS[prefix])
    return f"{prefix}:sha256:{digest(payload)}"


def _with_identity[T: StrictModel](model: type[T], prefix: str, fields: dict[str, Any]) -> T:
    name = _IDENTITY_FIELDS[prefix]
    provisional: dict[str, Any] = {name: f"{prefix}:sha256:{'0' * 64}", **fields}
    payload = model.model_construct(None, **provisional).model_dump(mode="json")
    payload.pop(name)
    final: dict[str, Any] = {name: f"{prefix}:sha256:{digest(payload)}", **fields}
    return model(**final)
