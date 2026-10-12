"""Independent review findings for H01.1 local creative extraction.

Every test in this module is a reproduction of a defect found while reviewing
``src/adhelm/creative_extraction.py`` and the ``video_creative.py`` additions as
hostile-input parser code.  The byte structures below are built here rather
than read from ``tests/fixtures/creative`` so the reproductions do not depend
on the implementation under review or on its own generator.

Each test states the behaviour the contract requires; a test that fails is an
open finding, not a description of current behaviour.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from typing import Any

import pytest

from adhelm.creative_extraction import (
    _image_parameters_digest,
    _video_parameters_digest,
    extract_image_features,
    extract_video_metadata,
)
from adhelm.errors import AdhelmError
from adhelm.video_creative import (
    CustomerDeclaredVideoAsset,
    DeclaredVideoContainerObservation,
    observe_declared_video_container,
)

EVIDENCE = "evidence:sha256:" + "a" * 64

# --- ISO base media builders ------------------------------------------------


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def full_box(kind: bytes, version: int, payload: bytes) -> bytes:
    return box(kind, bytes([version]) + b"\x00\x00\x00" + payload)


def ftyp(brand: bytes = b"isom") -> bytes:
    return box(b"ftyp", brand + struct.pack(">I", 512) + brand)


def mvhd(timescale: int = 1000, duration: int = 2000) -> bytes:
    return full_box(b"mvhd", 0, struct.pack(">IIII", 0, 0, timescale, duration) + bytes(80))


def tkhd(width: int, height: int) -> bytes:
    payload = struct.pack(">IIIII", 0, 0, 1, 0, 0) + bytes(16) + bytes(36)
    payload += struct.pack(">II", width << 16, height << 16)
    return full_box(b"tkhd", 0, payload)


def hdlr(handler: bytes) -> bytes:
    return full_box(b"hdlr", 0, bytes(4) + handler + bytes(12) + b"x\x00")


def mdhd(timescale: int) -> bytes:
    return full_box(b"mdhd", 0, struct.pack(">IIII", 0, 0, timescale, 0) + bytes(4))


def stsd(width: int, height: int) -> bytes:
    body = b"avc1" + bytes(6) + struct.pack(">H", 1) + bytes(16)
    body += struct.pack(">HH", width, height) + bytes(50)
    entry = struct.pack(">I", 4 + len(body)) + body
    return full_box(b"stsd", 0, struct.pack(">I", 1) + entry)


def stts(entries: tuple[tuple[int, int], ...]) -> bytes:
    payload = struct.pack(">I", len(entries))
    for count, delta in entries:
        payload += struct.pack(">II", count, delta)
    return full_box(b"stts", 0, payload)


def trak(
    *,
    handler: bytes = b"vide",
    width: int = 64,
    height: int = 64,
    media_timescale: int = 90_000,
    entries: tuple[tuple[int, int], ...] = ((60, 3000),),
) -> bytes:
    table = box(b"stbl", stsd(width, height) + stts(entries))
    media = box(b"mdia", mdhd(media_timescale) + hdlr(handler) + box(b"minf", table))
    return box(b"trak", tkhd(width, height) + media)


def mp4(*children: bytes, brand: bytes = b"isom") -> bytes:
    return ftyp(brand) + box(b"moov", b"".join(children))


# --- EBML / Matroska builders ----------------------------------------------

EBML_HEADER = b"\x1a\x45\xdf\xa3"
DOC_TYPE = b"\x42\x82"
SEGMENT = b"\x18\x53\x80\x67"
INFO = b"\x15\x49\xa9\x66"
TIMESTAMP_SCALE = b"\x2a\xd7\xb1"
DURATION = b"\x44\x89"
TRACKS = b"\x16\x54\xae\x6b"
TRACK_ENTRY = b"\xae"
TRACK_TYPE = b"\x83"
DEFAULT_DURATION = b"\x23\xe3\x83"
VIDEO = b"\xe0"
PIXEL_WIDTH = b"\xb0"
PIXEL_HEIGHT = b"\xba"


def ebml_size(value: int) -> bytes:
    return ((1 << 56) | value).to_bytes(8, "big")


def element(identifier: bytes, payload: bytes) -> bytes:
    return identifier + ebml_size(len(payload)) + payload


def uint_element(identifier: bytes, value: int) -> bytes:
    width = max(1, (value.bit_length() + 7) // 8)
    return element(identifier, value.to_bytes(width, "big"))


def track_entry(
    *, kind: int = 1, width: int = 32, height: int = 32, default_duration: int | None = 33_333_333
) -> bytes:
    payload = uint_element(TRACK_TYPE, kind)
    if default_duration is not None:
        payload += uint_element(DEFAULT_DURATION, default_duration)
    if kind == 1:
        payload += element(
            VIDEO, uint_element(PIXEL_WIDTH, width) + uint_element(PIXEL_HEIGHT, height)
        )
    return element(TRACK_ENTRY, payload)


def webm(
    *entries: bytes,
    timestamp_scale: int | None = 1_000_000,
    duration_seconds: float | None = 2.0,
    doc_type: bytes = b"webm",
) -> bytes:
    header = element(EBML_HEADER, element(DOC_TYPE, doc_type))
    info_payload = b""
    if timestamp_scale is not None:
        info_payload += uint_element(TIMESTAMP_SCALE, timestamp_scale)
    if duration_seconds is not None:
        ticks = duration_seconds * 1_000_000_000 / (timestamp_scale or 1_000_000)
        info_payload += element(DURATION, struct.pack(">d", ticks))
    body = element(INFO, info_payload)
    if entries:
        body += element(TRACKS, b"".join(entries))
    return header + element(SEGMENT, body)


# --- image builders ---------------------------------------------------------


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png_header_claiming(width: int, height: int) -> bytes:
    """A PNG whose IHDR declares dimensions far larger than its payload."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(bytes(10)))
        + png_chunk(b"IEND", b"")
    )


def declaration(data: bytes, **overrides: int) -> CustomerDeclaredVideoAsset:
    fields: dict[str, Any] = {
        "declared_width_px": 64,
        "declared_height_px": 64,
        "declared_duration_ms": 2000,
        "declared_frame_rate_milli": 30_000,
    }
    fields.update(overrides)
    return CustomerDeclaredVideoAsset.create(
        declared_content_digest=hashlib.sha256(data).hexdigest(),
        declared_byte_size=len(data),
        evidence_ref=EVIDENCE,
        **fields,
    )


# --- Finding 1: an untyped parser failure escapes the extractor -------------


def test_a_matroska_default_duration_below_one_milli_frame_fails_with_a_typed_error() -> None:
    """A hostile ``DefaultDuration`` must raise ``AdhelmError``, not ``ValidationError``.

    ``_matroska_metadata`` computes ``1_000_000_000_000 // default_duration``
    with no floor guard, so any ``DefaultDuration`` above 1e12 ns yields
    ``frame_rate_milli == 0`` and fails the ``ge=1`` field bound as an
    unhandled ``pydantic`` error.  The ISO path guards the same division
    (``rate if rate >= 1 else None``); the Matroska path does not.
    """
    data = webm(track_entry(default_duration=2_000_000_000_000))
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code in {
        "VIDEO_CONTAINER_MALFORMED",
        "VIDEO_CONTAINER_TRUNCATED",
        "VIDEO_CONTAINER_TOO_COMPLEX",
    }


# --- Finding 2: a defaulted timescale is reported as an extraction ----------


def test_a_matroska_container_without_a_timestamp_scale_does_not_claim_an_extraction() -> None:
    """An absent ``TimestampScale`` is a documented default, not an extracted fact.

    ``docs/implementation/creative-extraction.md`` states that everything the
    runtime cannot observe "is an explicit status, never an inference, a
    default, or a silent null", and AGENTS rule 20 forbids silent fallback.
    ``_matroska_metadata`` hard-codes ``timescale_status="EXTRACTED"`` and
    substitutes 1,000,000 ns, so a record cannot be told apart from one whose
    container really declared that value.
    """
    absent = extract_video_metadata(webm(track_entry(), timestamp_scale=None))
    present = extract_video_metadata(webm(track_entry(), timestamp_scale=1_000_000))
    assert present.timescale_status == "EXTRACTED"
    assert absent.timescale == 1_000_000
    assert absent.timescale_status != "EXTRACTED"


def test_the_video_parameters_digest_covers_the_matroska_default_timestamp_scale() -> None:
    """The default timescale changes a reported value, so it is a tunable.

    ``_video_parameters_digest`` records only refusal bounds.  Changing
    ``_MATROSKA_DEFAULT_TIMESTAMP_SCALE`` would silently change every
    ``timescale`` and ``duration_ms`` extracted from a container that omits
    ``TimestampScale`` while leaving ``parameters_digest`` identical, so
    incomparable records would compare as comparable.
    """
    import adhelm.creative_extraction as extraction

    baseline = _video_parameters_digest()
    original = extraction._MATROSKA_DEFAULT_TIMESTAMP_SCALE
    try:
        extraction._MATROSKA_DEFAULT_TIMESTAMP_SCALE = 500_000
        assert _video_parameters_digest() != baseline
    finally:
        extraction._MATROSKA_DEFAULT_TIMESTAMP_SCALE = original


# --- Finding 3: aspect-ratio-class thresholds are outside the digest --------


def test_the_image_parameters_digest_covers_the_aspect_ratio_class_thresholds() -> None:
    """``aspect_ratio_class`` is a stored field, so its cut points are tunables.

    ``_aspect_ratio_class`` hard-codes 1600 / 1050 / 950 / 625.  None of them
    reach ``_image_parameters_digest``, so moving a boundary would relabel
    every retained record's shape bucket without changing
    ``parameters_digest`` — the exact silent re-measurement the documented
    comparability rule forbids.
    """
    import inspect

    import adhelm.creative_extraction as extraction

    body = inspect.getsource(extraction._image_parameters_digest)
    class_body = inspect.getsource(extraction._aspect_ratio_class)
    thresholds = [word for word in ("1600", "1050", "950", "625") if word in class_body]
    assert thresholds == ["1600", "1050", "950", "625"]
    assert _image_parameters_digest() == extraction._image_parameters_digest()
    assert [word for word in thresholds if word in body] == thresholds


# --- Finding 4: a decompression-bomb header escapes as a third-party error --


def test_a_decompression_bomb_header_is_refused_with_a_typed_error() -> None:
    """Pillow's bomb guard raises outside the handled exception tuple.

    ``PIL.Image.DecompressionBombError`` derives from ``Exception``, not from
    ``OSError``/``ValueError``, so it passes straight through both
    ``analyze_image`` and ``_luminance_map``.  A 138-byte PNG that declares
    30000x30000 therefore aborts ``extract_image_features`` with an untyped
    third-party exception instead of a bounded typed refusal.
    """
    with pytest.raises(AdhelmError) as error:
        extract_image_features(png_header_claiming(30_000, 30_000))
    assert error.value.code in {"CREATIVE_IMAGE_TOO_LARGE", "INVALID_CREATIVE_IMAGE"}


# --- Finding 5: a size-0 box silently swallows its siblings -----------------


def test_a_size_zero_box_that_is_not_the_last_box_is_refused() -> None:
    """ISO/IEC 14496-12 permits ``size == 0`` only for the last box in the file.

    ``_boxes`` treats it as "to the end of the enclosing parent" at any depth
    and any position.  A ``moov`` whose ``mvhd`` declares size 0 therefore
    swallows the following ``trak``, and the extractor emits a record asserting
    ``video_track_status: NOT_PRESENT`` for a file that declares a video track.
    A malformed structure must fail closed, not become a positive claim.
    """
    mvhd_size_zero = (
        b"\x00\x00\x00\x00"
        + b"mvhd"
        + b"\x00\x00\x00\x00"
        + struct.pack(">IIII", 0, 0, 1000, 2000)
        + bytes(80)
    )
    data = ftyp() + box(b"moov", mvhd_size_zero + trak())
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code == "VIDEO_CONTAINER_MALFORMED"


# --- Finding 6: extra video tracks are dropped without disclosure -----------


def test_an_iso_container_with_two_video_tracks_does_not_silently_report_the_first() -> None:
    """Reporting one of several video tracks without saying so is a silent choice.

    ``_iso_bmff_metadata`` keeps the first ``vide`` handler and discards the
    rest.  Nothing in ``VideoContainerMetadata`` records how many video tracks
    the container declared, so a 64x64 decoy track in front of the real
    1920x1080 track makes the record describe the wrong stream with no status
    a reader could act on.
    """
    data = mp4(mvhd(), trak(width=64, height=64), trak(width=1920, height=1080))
    metadata = extract_video_metadata(data)
    assert metadata.video_track_status == "EXTRACTED"
    fields = set(type(metadata).model_fields)
    assert {"video_track_count", "video_track_selection_status"} & fields


def test_a_matroska_container_with_two_video_tracks_does_not_silently_report_the_first() -> None:
    """The Matroska path drops extra ``TrackType == 1`` entries the same way."""
    data = webm(track_entry(width=32, height=32), track_entry(width=1920, height=1080))
    metadata = extract_video_metadata(data)
    assert metadata.video_track_status == "EXTRACTED"
    fields = set(type(metadata).model_fields)
    assert {"video_track_count", "video_track_selection_status"} & fields


# --- Finding 7: extracted video facts have no domain bound ------------------


def test_an_extracted_iso_frame_rate_stays_inside_a_plausible_video_domain() -> None:
    """``timescale * 1000 // delta`` is unbounded above.

    ``video_creative.py`` bounds a *declared* rate at ``_MAX_FRAME_RATE_MILLI``
    (240,000 milli-fps).  The extractor accepted a container claiming a media
    timescale of 2^32-1 with a one-tick sample delta and reported
    ``frame_rate_milli = 4_294_967_295_000`` as ``EXTRACTED``, so a declared
    and an extracted rate were compared across different domains.

    Resolution: the extractor now refuses the container.  The finding's original
    assertion asked for an in-domain value from bytes that declare an
    out-of-domain one, which only a clamp could satisfy, and a clamped number
    would be a fabricated measurement.  Failing closed is the stricter answer
    and matches the documented bounded-parsing refusals.
    """
    data = mp4(mvhd(), trak(media_timescale=0xFFFFFFFF, entries=((10, 1),)))
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code == "VIDEO_CONTAINER_MALFORMED"


def test_an_extracted_matroska_frame_rate_stays_inside_a_plausible_video_domain() -> None:
    """A one-nanosecond ``DefaultDuration`` claimed one billion frames a second.

    Resolved the same way as the ISO case: an out-of-domain extracted rate is a
    typed container refusal, never a clamped value.
    """
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(webm(track_entry(default_duration=1)))
    assert error.value.code == "VIDEO_CONTAINER_MALFORMED"


def test_extracted_container_dimensions_stay_inside_a_plausible_video_domain() -> None:
    """A declared dimension is capped at 8,192; an extracted one was not capped.

    Resolved the same way as the two rate findings: a container declaring a
    65535-pixel track is refused rather than reported with a clamped dimension.
    """
    data = mp4(mvhd(), trak(width=65535, height=65535))
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code == "VIDEO_CONTAINER_MALFORMED"


def test_an_absurd_container_timescale_fails_with_a_container_error() -> None:
    """Hostile bytes must not surface an internal canonicalization error code.

    A Matroska ``TimestampScale`` of 2^63 has no domain check, so the failure
    only happens later inside ``adhelm.canonical.digest`` and reaches the caller
    as ``INVALID_CANONICAL_VALUE`` — a code that says nothing about the video
    container and is not one of the documented refusal codes.
    """
    data = webm(track_entry(), timestamp_scale=2**63, duration_seconds=None)
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code in {
        "VIDEO_CONTAINER_MALFORMED",
        "VIDEO_CONTAINER_TRUNCATED",
        "VIDEO_CONTAINER_TOO_COMPLEX",
        "UNSUPPORTED_VIDEO_CONTAINER",
    }


# --- Finding 8: the declared-video observation is not self-binding ----------


def test_a_declared_video_observation_binds_its_asset_identity() -> None:
    """The observation copies declared facts without re-deriving the asset id.

    ``ImageStructureFeatures`` and ``AssetStructureRecord`` both recompute and
    enforce their identity in a ``model_validator``.
    ``DeclaredVideoContainerObservation`` does not, so a record read back from
    JSON can carry ``video_asset_version_ref`` for one declaration while
    stating the declared width, height, duration, or rate of another.
    """
    data = mp4(mvhd(), trak())
    observation = observe_declared_video_container(declaration(data), data)
    forged = observation.model_dump()
    forged["declared_width_px"] = 4096
    forged["width_status"] = "DECLARATION_MISMATCH"
    forged["declaration_status"] = "DECLARATION_MISMATCH"
    with pytest.raises(ValueError):
        DeclaredVideoContainerObservation(**forged)


def test_a_declared_video_observation_rederives_its_field_comparisons() -> None:
    """Comparison statuses must follow from the values the record itself holds.

    ``declaration_status_summarizes_its_fields`` only checks the summary
    against the stated per-field statuses; it never checks a per-field status
    against ``declared_*`` and ``extracted``.  Swapping ``extracted`` for an
    unrelated container leaves ``content_digest_status: DECLARATION_MATCH`` and
    ``declaration_status: DECLARATION_MATCH`` in place — a fabricated
    verification claim inside a persistable record (AGENTS rule 20).
    """
    data = mp4(mvhd(), trak())
    other = mp4(mvhd(timescale=1000, duration=9000), trak(width=320, height=240))
    observation = observe_declared_video_container(declaration(data), data)
    assert observation.content_digest_status == "DECLARATION_MATCH"
    forged = observation.model_dump()
    forged["extracted"] = extract_video_metadata(other).model_dump(mode="json")
    with pytest.raises(ValueError):
        DeclaredVideoContainerObservation(**forged)


def test_a_floor_rounded_container_duration_is_not_reported_as_a_flat_mismatch() -> None:
    """``duration_ms`` is a floor of an exact tick count, compared with ``==``.

    A container declaring 179,999 ticks at 90,000 ticks per second lasts
    1999.99 ms.  ``_movie_header`` floors it to 1999, so a truthful declaration
    of 2000 ms is reported as ``DECLARATION_MISMATCH`` with no rounding
    tolerance and no status distinguishing a real disagreement from the
    extractor's own sub-millisecond truncation.
    """
    data = mp4(mvhd(timescale=90_000, duration=179_999), trak(media_timescale=90_000))
    observation = observe_declared_video_container(
        declaration(data, declared_duration_ms=2000), data
    )
    assert observation.duration_status != "DECLARATION_MISMATCH"


# --- Finding 9: EXIF orientation is undisclosed under a spatial grid --------


def test_a_layout_grid_discloses_the_orientation_it_was_measured_in() -> None:
    """The 4x4 grid is spatial, so stored-versus-displayed orientation matters.

    Neither ``analyze_image`` nor ``_luminance_map`` applies the EXIF
    orientation tag.  Two JPEGs with identical stored pixels but orientation 1
    and orientation 6 display 90 degrees apart, yet they produce the same
    ``aspect_ratio_class`` and the same ``layout_cells``, and no field records
    which orientation the grid describes.  A caller comparing "where the text
    block sits" across creatives cannot detect the difference.
    """
    from PIL import Image

    image = Image.new("RGB", (32, 16))
    for x in range(32):
        for y in range(16):
            image.putpixel((x, y), (255, 255, 255) if x < 16 else (0, 0, 0))
    upright = io.BytesIO()
    image.save(upright, format="JPEG", quality=95)
    exif = image.getexif()
    exif[274] = 6
    rotated = io.BytesIO()
    image.save(rotated, format="JPEG", quality=95, exif=exif)

    plain = extract_image_features(upright.getvalue())
    tagged = extract_image_features(rotated.getvalue())
    assert plain.layout_status == "EXTRACTED" and tagged.layout_status == "EXTRACTED"
    fields = set(type(plain).model_fields)
    assert {"orientation_status", "exif_orientation_status"} & fields


# --- Finding 10: the image bytes are decoded and materialized twice ---------


def test_image_extraction_materializes_its_pixels_once() -> None:
    """``extract_image_features`` decodes the same bytes twice.

    ``analyze_image`` builds a ``list`` of width*height RGB tuples, then
    ``_luminance_map`` reopens the identical bytes and builds a second one.
    Measured at the permitted bound (``MAX_IMAGE_PIXELS`` = 10,000,000) a
    138 KB PNG costs 25.6 s and 2.5 GB of resident memory, against 20.0 s and
    2.4 GB for ``analyze_image`` alone.  There is no time or memory bound at
    all, so a small input buys a large amount of work.
    """
    from PIL import Image

    calls = 0
    original = Image.Image.get_flattened_data

    def counted(self: Image.Image, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    Image.Image.get_flattened_data = counted  # type: ignore[method-assign]
    try:
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), (12, 34, 56)).save(buffer, format="PNG")
        extract_image_features(buffer.getvalue())
    finally:
        Image.Image.get_flattened_data = original  # type: ignore[method-assign]
    assert calls == 1


# ===========================================================================
# Re-review of the repair commit.  Findings 1-12 above are confirmed repaired;
# what follows is new.
# ===========================================================================


def duplicated_trak(kind: bytes) -> bytes:
    """A ``trak`` that declares one exactly-once structure twice.

    The two copies always disagree, so which one the parser keeps is visible in
    the emitted record.
    """
    first_table = box(b"stbl", stsd(320, 240) + stts(((60, 3000),)))
    second_table = box(b"stbl", stsd(1920, 1080) + stts(((60, 1500),)))
    if kind == b"stbl":
        media = box(
            b"mdia", mdhd(90_000) + hdlr(b"vide") + box(b"minf", first_table + second_table)
        )
        return box(b"trak", tkhd(64, 64) + media)
    if kind == b"minf":
        media = box(
            b"mdia",
            mdhd(90_000) + hdlr(b"vide") + box(b"minf", first_table) + box(b"minf", second_table),
        )
        return box(b"trak", tkhd(64, 64) + media)
    if kind == b"mdia":
        return box(
            b"trak",
            tkhd(64, 64)
            + box(b"mdia", mdhd(90_000) + hdlr(b"vide") + box(b"minf", first_table))
            + box(b"mdia", mdhd(30_000) + hdlr(b"vide") + box(b"minf", second_table)),
        )
    media_children = {
        b"hdlr": mdhd(90_000) + hdlr(b"soun") + hdlr(b"vide"),
        b"mdhd": mdhd(90_000) + mdhd(30_000) + hdlr(b"vide"),
    }.get(kind, mdhd(90_000) + hdlr(b"vide"))
    media = box(b"mdia", media_children + box(b"minf", first_table))
    header = tkhd(64, 64) + tkhd(1920, 1080) if kind == b"tkhd" else tkhd(64, 64)
    return box(b"trak", header + media)


@pytest.mark.parametrize("kind", [b"tkhd", b"mdia", b"hdlr", b"mdhd", b"minf", b"stbl"])
def test_a_duplicate_iso_singleton_box_is_malformed(kind: bytes) -> None:
    """The duplicate-structure contract stops six boxes short of covering itself.

    The repair made a second ``moov``, ``mvhd``, ``stsd``, or ``stts`` malformed
    (``_only``), and the documentation states that a duplicate of an
    exactly-once structure is malformed.  ``mdia``, ``minf``, ``stbl``, ``tkhd``,
    ``hdlr``, and ``mdhd`` still go through ``_first`` and silently keep the
    first copy.  ISO/IEC 14496-12 8.3.1/8.4.1/8.4.3/8.4.4/8.5.1 make every one
    of them exactly one per parent, so a second copy is the same malformed
    input, not a choice to make quietly.

    The ``hdlr`` case is the sharpest: a track declaring ``soun`` then ``vide``
    is reported as ``video_track_count: 0`` with ``audio_track_status:
    DECLARED_PRESENT``, an absence claim about a track that declares a video
    handler.
    """
    data = mp4(mvhd(), duplicated_trak(kind))
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code == "VIDEO_CONTAINER_MALFORMED"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            uint_element(TRACK_TYPE, 1)
            + uint_element(TRACK_TYPE, 2)
            + element(VIDEO, uint_element(PIXEL_WIDTH, 32) + uint_element(PIXEL_HEIGHT, 32)),
            id="TrackType",
        ),
        pytest.param(
            uint_element(TRACK_TYPE, 1)
            + uint_element(DEFAULT_DURATION, 33_333_333)
            + uint_element(DEFAULT_DURATION, 16_666_666)
            + element(VIDEO, uint_element(PIXEL_WIDTH, 32) + uint_element(PIXEL_HEIGHT, 32)),
            id="DefaultDuration",
        ),
        pytest.param(
            uint_element(TRACK_TYPE, 1)
            + element(VIDEO, uint_element(PIXEL_WIDTH, 320) + uint_element(PIXEL_HEIGHT, 240))
            + element(VIDEO, uint_element(PIXEL_WIDTH, 1920) + uint_element(PIXEL_HEIGHT, 1080)),
            id="Video",
        ),
        pytest.param(
            uint_element(TRACK_TYPE, 1)
            + element(
                VIDEO,
                uint_element(PIXEL_WIDTH, 320)
                + uint_element(PIXEL_WIDTH, 1920)
                + uint_element(PIXEL_HEIGHT, 240),
            ),
            id="PixelWidth",
        ),
    ],
)
def test_a_duplicate_matroska_track_element_is_malformed(payload: bytes) -> None:
    """Inside a ``TrackEntry`` the same duplicates are silently *last*-wins.

    ``_only`` guards ``Segment``, ``Info``, ``Tracks``, ``DocType``,
    ``TimestampScale``, and ``Duration``.  ``TrackType``, ``DefaultDuration``,
    ``Video``, ``PixelWidth``, and ``PixelHeight`` are read in a plain loop that
    overwrites, so the extractor resolves a contradiction quietly and in the
    opposite direction from the ISO path.  Observed: ``TrackType`` 1 then 2 is
    reported as audio-only with ``video_track_count: 0``; two ``DefaultDuration``
    values report 60,000 milli-fps rather than the first 30,000; and a duplicate
    ``PixelWidth`` reports 1920x240, a dimension pair no single element declares.
    """
    data = webm(element(TRACK_ENTRY, payload))
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(data)
    assert error.value.code == "VIDEO_CONTAINER_MALFORMED"


def test_a_zero_matroska_duration_is_not_an_extracted_zero_length_movie() -> None:
    """A ``Duration`` of zero becomes ``duration_ms: 0`` with status ``EXTRACTED``.

    Matroska requires ``Duration`` to be greater than zero, and the ISO path
    already maps a zero ``mvhd`` duration to ``NOT_DOCUMENTED_IN_CONTAINER``
    rather than to a zero-length movie.  The Matroska path reports the zero as a
    positive extracted fact, so the same absent-duration container yields two
    different statuses depending on its family.  Compounding it, the new
    truncation tolerance then reads that zero as consistent with a declared
    1 ms asset (``DECLARATION_WITHIN_TRUNCATION``), which turns "no duration at
    all" into agreement.
    """
    header = element(EBML_HEADER, element(DOC_TYPE, b"webm"))
    info = element(
        INFO, uint_element(TIMESTAMP_SCALE, 1_000_000) + element(DURATION, struct.pack(">d", 0.0))
    )
    tracks = element(TRACKS, track_entry())
    metadata = extract_video_metadata(header + element(SEGMENT, info + tracks))
    assert metadata.duration_status != "EXTRACTED"
    assert metadata.duration_ms is None


def test_the_extracted_duration_domain_equals_the_declared_one() -> None:
    """The documented shared domain does not hold for duration.

    ``docs/implementation/creative-extraction.md`` states that extracted values
    "share the declared video domain in ``video_creative.py``", listing 24 hours.
    The declared bound is ``_MAX_DURATION_MS`` = 3,600,000 (one hour), so the
    extracted domain is 24x wider.  A two-hour MP4 extracts
    ``duration_ms: 7_200_000``, a value no declaration can ever carry, so
    ``duration_status`` for such media is permanently ``DECLARATION_MISMATCH``
    with nothing distinguishing it from a real disagreement.
    """
    import adhelm.creative_extraction as extraction
    import adhelm.video_creative as declarations

    two_hours = mp4(mvhd(timescale=1000, duration=7_200_000), trak())
    assert extract_video_metadata(two_hours).duration_ms == 7_200_000
    assert extraction.MAX_EXTRACTED_DURATION_MS == declarations._MAX_DURATION_MS


def png_with_corrupt_idat_crc() -> bytes:
    """A valid PNG whose IDAT chunk carries one wrong CRC byte."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (51, 102, 204)).save(buffer, format="PNG")
    data = bytearray(buffer.getvalue())
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = bytes(data[offset + 4 : offset + 8])
        crc_at = offset + 8 + length
        if kind == b"IDAT":
            data[crc_at + 3] ^= 0xFF
            return bytes(data)
        offset = crc_at + 4
    raise AssertionError("the generated PNG has no IDAT chunk")


def test_a_corrupt_png_checksum_is_refused_with_a_typed_error() -> None:
    """Pillow signals a broken PNG with ``SyntaxError``, which still escapes.

    The repair closed the untyped-escape hole for ``DecompressionBombError`` by
    naming that one class.  Pillow's plugins also raise a bare ``SyntaxError``
    for a corrupt file — ``PngImagePlugin.crc`` raises
    ``SyntaxError("broken PNG file (bad header checksum in b'IDAT')")`` from
    inside ``verify()`` — and ``SyntaxError`` derives from ``Exception``, not
    from ``OSError`` or ``ValueError``, so it passes straight through
    ``decode_image``, ``analyze_image``, and ``extract_image_features``.

    This is not an exotic input.  Flipping one byte of a checked-in PNG fixture
    reaches it, and single-byte mutation of the four PNG fixtures escapes
    untyped in 40-65% of cases (20,000 mutated video containers produced no
    untyped escape at all, and JPEG and WebP are clean).  A corrupted download
    or a bit-flip aborts the CLI with a Python traceback instead of
    ``INVALID_CREATIVE_IMAGE`` at exit code 2.
    """
    with pytest.raises(AdhelmError) as error:
        extract_image_features(png_with_corrupt_idat_crc())
    assert error.value.code == "INVALID_CREATIVE_IMAGE"


def test_the_shared_analyzer_also_refuses_a_corrupt_png_with_a_typed_error() -> None:
    """The same escape reaches the pre-existing ``analyze_image`` entry point."""
    from adhelm.creative import analyze_image

    with pytest.raises(AdhelmError) as error:
        analyze_image(png_with_corrupt_idat_crc())
    assert error.value.code == "INVALID_CREATIVE_IMAGE"
