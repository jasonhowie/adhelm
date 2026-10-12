"""Bounded container parsing and declared-versus-extracted video semantics.

Every fixture is checked-in synthetic bytes.  No decoder, frame, transcript,
provider, or network path is exercised.
"""

from __future__ import annotations

import hashlib
from importlib import util
from pathlib import Path
from types import ModuleType

import pytest

from adhelm.canonical import canonical_bytes
from adhelm.creative_extraction import (
    MAX_CONTAINER_DEPTH,
    _boxes,
    _Budget,
    extract_video_metadata,
)
from adhelm.errors import AdhelmError
from adhelm.video_creative import (
    CustomerDeclaredVideoAsset,
    DeclaredVideoContainerObservation,
    observe_declared_video_container,
)

FIXTURES = Path(__file__).parent / "fixtures" / "creative"
EVIDENCE = "evidence:sha256:" + "a" * 64


def generator() -> ModuleType:
    spec = util.spec_from_file_location(
        "adhelm_creative_fixture_generator", FIXTURES / "generate.py"
    )
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declaration(
    data: bytes,
    *,
    width: int = 1080,
    height: int = 1920,
    duration_ms: int = 2000,
    frame_rate_milli: int = 30_000,
) -> CustomerDeclaredVideoAsset:
    return CustomerDeclaredVideoAsset.create(
        declared_content_digest=hashlib.sha256(data).hexdigest(),
        declared_byte_size=len(data),
        declared_width_px=width,
        declared_height_px=height,
        declared_duration_ms=duration_ms,
        declared_frame_rate_milli=frame_rate_milli,
        evidence_ref=EVIDENCE,
    )


def test_iso_base_media_metadata_is_extracted_without_decoding_a_frame() -> None:
    record = extract_video_metadata((FIXTURES / "video-mp4-constant-rate.mp4").read_bytes())
    assert record.container_kind == "ISO_BMFF"
    assert record.container_brand == "isom"
    assert (record.timescale, record.timescale_unit) == (1000, "TICKS_PER_SECOND")
    assert record.duration_ms == 2000
    assert (record.video_width_px, record.video_height_px) == (1080, 1920)
    assert record.video_dimensions_source == "ISO_BMFF_SAMPLE_DESCRIPTION"
    assert (record.frame_rate_status, record.frame_rate_milli) == ("EXTRACTED", 30_000)
    assert (record.frame_count_status, record.frame_count) == ("EXTRACTED", 60)
    assert record.audio_track_status == "DECLARED_PRESENT"
    assert record.decoded_frame_status == "NOT_DECODED_NO_LOCAL_DECODER"
    assert record.on_visual_text_status == "NOT_EXTRACTED_NO_LOCAL_OCR"
    assert record.transcript_status == "NOT_EXTRACTED_NO_LOCAL_TRANSCRIPTION"
    assert record.audio_analysis_status == "NOT_ANALYZED_NO_AUDIO_EXTRACTION"
    assert record.media_bytes_status == "NOT_RETAINED"


def test_a_sixty_four_bit_box_size_reads_the_same_movie_facts() -> None:
    small = extract_video_metadata((FIXTURES / "video-mp4-constant-rate.mp4").read_bytes())
    large = extract_video_metadata((FIXTURES / "video-mp4-large-size-moov.mp4").read_bytes())
    assert (large.duration_ms, large.frame_count) == (small.duration_ms, small.frame_count)
    assert large.video_metadata_id != small.video_metadata_id


def test_variable_sample_deltas_never_become_a_single_declared_frame_rate() -> None:
    record = extract_video_metadata((FIXTURES / "video-mp4-variable-rate.mp4").read_bytes())
    assert record.frame_rate_status == "VARIABLE_NOT_CONSTANT"
    assert record.frame_rate_milli is None
    assert (record.frame_count_status, record.frame_count) == ("EXTRACTED", 30)
    assert record.audio_track_status == "DECLARED_ABSENT"


def test_an_undocumented_movie_duration_is_a_status_not_a_guess() -> None:
    record = extract_video_metadata((FIXTURES / "video-mp4-unknown-duration.mp4").read_bytes())
    assert record.duration_status == "NOT_DOCUMENTED_IN_CONTAINER"
    assert record.duration_ms is None
    assert record.timescale_status == "EXTRACTED"


def test_track_header_dimensions_are_labelled_when_the_sample_entry_omits_them() -> None:
    record = extract_video_metadata(
        (FIXTURES / "video-mp4-track-header-dimensions.mp4").read_bytes()
    )
    assert (record.video_width_px, record.video_height_px) == (640, 360)
    assert record.video_dimensions_source == "ISO_BMFF_TRACK_HEADER"


def test_matroska_metadata_reports_its_own_timescale_unit_and_frame_limits() -> None:
    record = extract_video_metadata((FIXTURES / "video-webm-constant-rate.webm").read_bytes())
    assert (record.container_kind, record.container_brand) == ("MATROSKA", "webm")
    assert (record.timescale, record.timescale_unit) == (1_000_000, "NANOSECONDS_PER_TICK")
    assert record.duration_ms == 3000
    assert (record.video_width_px, record.video_height_px) == (1280, 720)
    assert record.frame_rate_milli == 30_000
    assert record.frame_count_status == "NOT_DERIVABLE_FROM_CONTAINER"
    assert record.audio_track_status == "DECLARED_PRESENT"


def test_a_matroska_file_without_duration_or_rate_reports_both_as_absent() -> None:
    record = extract_video_metadata((FIXTURES / "video-webm-video-only.webm").read_bytes())
    assert record.duration_status == "NOT_DOCUMENTED_IN_CONTAINER"
    assert record.frame_rate_status == "NOT_DOCUMENTED_IN_CONTAINER"
    assert record.audio_track_status == "DECLARED_ABSENT"


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("video-mp4-truncated.mp4", "VIDEO_CONTAINER_TRUNCATED"),
        ("video-mp4-oversize-claim.mp4", "VIDEO_CONTAINER_TRUNCATED"),
        ("video-mp4-bad-box-size.mp4", "VIDEO_CONTAINER_MALFORMED"),
        ("video-mp4-no-movie.mp4", "VIDEO_CONTAINER_MALFORMED"),
        ("video-mp4-many-boxes.mp4", "VIDEO_CONTAINER_TOO_COMPLEX"),
        ("video-webm-oversize-element.webm", "VIDEO_CONTAINER_TRUNCATED"),
        ("video-unsupported.bin", "UNSUPPORTED_VIDEO_CONTAINER"),
    ],
)
def test_broken_containers_fail_closed_with_a_typed_error(name: str, code: str) -> None:
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata((FIXTURES / name).read_bytes())
    assert error.value.code == code
    assert error.value.exit_code == 2


def test_empty_and_oversized_container_bytes_are_refused() -> None:
    for data in (b"", b"\x00" * (64 * 1024 * 1024 + 1)):
        with pytest.raises(AdhelmError) as error:
            extract_video_metadata(data)
        assert error.value.code == "CREATIVE_VIDEO_TOO_LARGE"


def test_box_walking_is_depth_bounded_independently_of_its_callers() -> None:
    build = generator()
    data = build.box(b"free", b"")
    with pytest.raises(AdhelmError) as error:
        _boxes(data, 0, len(data), budget=_Budget(), depth=MAX_CONTAINER_DEPTH + 1)
    assert error.value.code == "VIDEO_CONTAINER_TOO_COMPLEX"


def test_a_sample_table_entry_count_beyond_the_buffer_is_truncated_not_partial() -> None:
    build = generator()
    table = build.box(
        b"stbl",
        build.stsd(build.visual_sample_entry(b"avc1", 320, 240))
        + build.full_box(b"stts", 0, (64).to_bytes(4, "big") + bytes(8)),
    )
    track = build.box(
        b"trak", build.tkhd(1, 1000, 320, 240) + build.mdia(b"vide", 1000, 1000, table)
    )
    data = build.ftyp() + build.box(b"moov", build.mvhd(1000, 1000) + track)
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code == "VIDEO_CONTAINER_TRUNCATED"


def test_a_declaration_matching_its_container_is_reported_as_a_match() -> None:
    data = (FIXTURES / "video-mp4-constant-rate.mp4").read_bytes()
    asset = declaration(data)
    observation = observe_declared_video_container(asset, data)
    assert observation.declaration_status == "DECLARATION_MATCH"
    assert observation.local_media_status == "CONTAINER_METADATA_ONLY_NO_DECODE"
    assert observation.decoded_frame_status == "NOT_DECODED"
    assert observation.execution_eligibility == "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
    assert observation.component_performance_status == "NOT_INFERRED"


def test_extracted_container_facts_never_overwrite_the_declaration() -> None:
    data = (FIXTURES / "video-mp4-constant-rate.mp4").read_bytes()
    asset = declaration(data, width=1920, height=1080, duration_ms=9000, frame_rate_milli=24_000)
    observation = observe_declared_video_container(asset, data)
    assert observation.declaration_status == "DECLARATION_MISMATCH"
    assert observation.width_status == "DECLARATION_MISMATCH"
    assert observation.height_status == "DECLARATION_MISMATCH"
    assert observation.duration_status == "DECLARATION_MISMATCH"
    assert observation.frame_rate_status == "DECLARATION_MISMATCH"
    assert observation.declared_width_px == 1920
    assert observation.declared_duration_ms == 9000
    assert observation.extracted.video_width_px == 1080
    assert observation.extracted.duration_ms == 2000
    # The declared asset record and its identity are unchanged by observation.
    assert canonical_bytes(asset) == canonical_bytes(
        declaration(data, width=1920, height=1080, duration_ms=9000, frame_rate_milli=24_000)
    )
    assert observation.video_asset_version_ref == asset.video_asset_version_id
    assert asset.local_media_status == "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"


def test_a_container_without_the_declared_fact_is_partially_unverified() -> None:
    data = (FIXTURES / "video-mp4-unknown-duration.mp4").read_bytes()
    observation = observe_declared_video_container(declaration(data), data)
    assert observation.duration_status == "NOT_COMPARABLE_NOT_IN_CONTAINER"
    assert observation.declaration_status == "DECLARATION_PARTIALLY_UNVERIFIED"


def test_a_declared_mp4_backed_by_a_matroska_container_is_a_mismatch() -> None:
    data = (FIXTURES / "video-webm-constant-rate.webm").read_bytes()
    observation = observe_declared_video_container(
        declaration(data, width=1280, height=720, duration_ms=3000), data
    )
    assert observation.container_status == "DECLARATION_MISMATCH"
    assert observation.declaration_status == "DECLARATION_MISMATCH"


def test_a_declaration_bound_to_other_bytes_is_a_digest_mismatch() -> None:
    data = (FIXTURES / "video-mp4-constant-rate.mp4").read_bytes()
    other = (FIXTURES / "video-mp4-variable-rate.mp4").read_bytes()
    observation = observe_declared_video_container(declaration(other), data)
    assert observation.content_digest_status == "DECLARATION_MISMATCH"
    assert observation.byte_size_status == "DECLARATION_MISMATCH"
    assert observation.declaration_status == "DECLARATION_MISMATCH"


def test_a_summary_status_cannot_contradict_its_field_comparisons() -> None:
    data = (FIXTURES / "video-mp4-constant-rate.mp4").read_bytes()
    observation = observe_declared_video_container(declaration(data), data)
    assert DeclaredVideoContainerObservation(**observation.model_dump()) == observation
    forged = observation.model_copy(update={"width_status": "DECLARATION_MISMATCH"}).model_dump()
    with pytest.raises(ValueError):
        DeclaredVideoContainerObservation(**forged)


def test_a_defaulted_matroska_timestamp_scale_is_not_reported_as_extracted() -> None:
    defaulted = extract_video_metadata(
        (FIXTURES / "video-webm-default-timescale.webm").read_bytes()
    )
    declared = extract_video_metadata((FIXTURES / "video-webm-constant-rate.webm").read_bytes())
    assert defaulted.timescale == declared.timescale == 1_000_000
    assert defaulted.timescale_status == "DEFAULTED_BY_SPECIFICATION"
    assert declared.timescale_status == "EXTRACTED"
    assert defaulted.duration_ms == 3000


def test_several_video_tracks_are_disclosed_rather_than_silently_dropped() -> None:
    several = extract_video_metadata((FIXTURES / "video-mp4-two-video-tracks.mp4").read_bytes())
    one = extract_video_metadata((FIXTURES / "video-mp4-constant-rate.mp4").read_bytes())
    assert several.video_track_count == 2
    assert several.video_track_selection_status == "FIRST_DECLARED_OF_MULTIPLE"
    assert (several.video_width_px, several.video_height_px) == (640, 360)
    assert one.video_track_count == 1
    assert one.video_track_selection_status == "SINGLE_VIDEO_TRACK"
    assert (one.audio_track_count, one.audio_track_status) == (1, "DECLARED_PRESENT")
    assert (several.audio_track_count, several.audio_track_status) == (0, "DECLARED_ABSENT")


def test_a_size_zero_box_inside_a_parent_cannot_swallow_a_declared_track() -> None:
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata((FIXTURES / "video-mp4-size-zero-inner-box.mp4").read_bytes())
    assert error.value.code == "VIDEO_CONTAINER_MALFORMED"


def test_the_extractors_own_truncation_is_its_own_declaration_status() -> None:
    data = (FIXTURES / "video-mp4-truncated-duration.mp4").read_bytes()
    metadata = extract_video_metadata(data)
    assert metadata.duration_ms == 1999
    observation = observe_declared_video_container(declaration(data, duration_ms=2000), data)
    assert observation.duration_status == "DECLARATION_WITHIN_TRUNCATION"
    assert observation.declaration_status == "DECLARATION_MATCH"
    # Two milliseconds is outside the extractor's own one-unit floor, so it is a
    # real disagreement rather than a rounding artefact.
    beyond = observe_declared_video_container(declaration(data, duration_ms=2001), data)
    assert beyond.duration_status == "DECLARATION_MISMATCH"
    assert beyond.declaration_status == "DECLARATION_MISMATCH"


def test_a_plausible_value_beyond_the_product_bound_is_out_of_domain() -> None:
    """8,193 pixels is a real header; 65,535 is not a video description at all."""
    build = generator()
    beyond = build.ftyp() + build.box(
        b"moov", build.mvhd(1000, 1000) + build.video_track(width=8193, height=8193)
    )
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(beyond)
    assert error.value.code == "VIDEO_CONTAINER_OUT_OF_DOMAIN"
    assert error.value.exit_code == 2
    implausible = build.ftyp() + build.box(
        b"moov", build.mvhd(1000, 1000) + build.video_track(width=65535, height=65535)
    )
    with pytest.raises(AdhelmError) as beyond_structure:
        extract_video_metadata(implausible)
    assert beyond_structure.value.code == "VIDEO_CONTAINER_MALFORMED"


def test_a_duration_beyond_the_shared_domain_is_out_of_domain() -> None:
    build = generator()
    data = build.ftyp() + build.box(b"moov", build.mvhd(1000, 86_400_001) + build.video_track())
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code == "VIDEO_CONTAINER_OUT_OF_DOMAIN"


def test_the_extracted_and_declared_duration_domains_are_one_bound() -> None:
    from adhelm.creative_extraction import MAX_EXTRACTED_DURATION_MS
    from adhelm.video_creative import _MAX_DURATION_MS

    assert MAX_EXTRACTED_DURATION_MS == _MAX_DURATION_MS == 86_400_000
