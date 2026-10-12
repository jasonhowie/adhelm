"""Reproducible generator for the local creative extraction golden fixtures.

Run ``uv run python tests/fixtures/creative/generate.py`` to rewrite every
fixture in this directory.  Images are drawn pixel by pixel with Pillow; video
fixtures are hand-built ISO base media and EBML byte structures, so no encoder,
decoder, or network access is involved.  Regenerating with a different Pillow
release can change compressed image bytes, which is why the checked-in files
and their digests are the golden inputs and the tests never regenerate them.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent

# --- images ----------------------------------------------------------------


def solid_image() -> bytes:
    from PIL import Image

    return _png(Image.new("RGB", (16, 16), (51, 102, 204)))


def quadrant_image() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (32, 32))
    colours = ((255, 255, 255), (0, 0, 0), (204, 0, 0), (0, 0, 204))
    pixels = image.load()
    assert pixels is not None
    for y in range(32):
        for x in range(32):
            pixels[x, y] = colours[(y // 16) * 2 + (x // 16)]
    return _png(image)


def text_band_image() -> bytes:
    """A white card with a striped band that mimics a bottom-left text block."""
    from PIL import Image

    image = Image.new("RGB", (64, 64), (255, 255, 255))
    pixels = image.load()
    assert pixels is not None
    for y in range(48, 60):
        for x in range(4, 44):
            if x % 2 == 0:
                pixels[x, y] = (16, 16, 16)
    return _png(image)


def gradient_image() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (36, 24))
    pixels = image.load()
    assert pixels is not None
    for y in range(24):
        for x in range(36):
            level = x * 7
            pixels[x, y] = (level, level, 255 - level)
    buffer = _encode(image, "JPEG", quality=90, optimize=False, subsampling=0)
    return buffer


def banner_image() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (40, 10), (250, 250, 240))
    pixels = image.load()
    assert pixels is not None
    for y in range(10):
        for x in range(20, 40):
            pixels[x, y] = (10, 60, 30)
    return _encode(image, "WEBP", lossless=True, quality=100, method=6)


def tiny_image() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (2, 2))
    pixels = image.load()
    assert pixels is not None
    pixels[0, 0] = (255, 0, 0)
    pixels[1, 0] = (0, 255, 0)
    pixels[0, 1] = (0, 0, 255)
    pixels[1, 1] = (255, 255, 255)
    return _png(image)


def truncated_image() -> bytes:
    return solid_image()[:48]


def _png(image: object) -> bytes:
    return _encode(image, "PNG", optimize=True)


def _encode(image: object, image_format: str, **options: object) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format=image_format, **options)  # type: ignore[attr-defined]
    return buffer.getvalue()


# --- ISO base media (MP4/MOV) ----------------------------------------------


def box(kind: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def large_box(kind: bytes, payload: bytes) -> bytes:
    return (1).to_bytes(4, "big") + kind + (len(payload) + 16).to_bytes(8, "big") + payload


def full_box(kind: bytes, version: int, payload: bytes) -> bytes:
    return box(kind, bytes([version]) + b"\x00\x00\x00" + payload)


def ftyp(brand: bytes = b"isom") -> bytes:
    return box(b"ftyp", brand + (512).to_bytes(4, "big") + brand + b"mp42")


def mvhd(timescale: int, duration: int) -> bytes:
    payload = (
        (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + timescale.to_bytes(4, "big")
        + duration.to_bytes(4, "big")
        + (0x00010000).to_bytes(4, "big")
        + (0x0100).to_bytes(2, "big")
        + bytes(10)
        + _unity_matrix()
        + bytes(24)
        + (2).to_bytes(4, "big")
    )
    return full_box(b"mvhd", 0, payload)


def tkhd(track_id: int, duration: int, width: int, height: int) -> bytes:
    payload = (
        (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + track_id.to_bytes(4, "big")
        + bytes(4)
        + duration.to_bytes(4, "big")
        + bytes(8)
        + (0).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + bytes(2)
        + _unity_matrix()
        + (width << 16).to_bytes(4, "big")
        + (height << 16).to_bytes(4, "big")
    )
    return full_box(b"tkhd", 0, payload)


def mdhd(timescale: int, duration: int) -> bytes:
    payload = (
        (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + timescale.to_bytes(4, "big")
        + duration.to_bytes(4, "big")
        + (0x55C4).to_bytes(2, "big")
        + bytes(2)
    )
    return full_box(b"mdhd", 0, payload)


def hdlr(handler: bytes) -> bytes:
    return full_box(b"hdlr", 0, bytes(4) + handler + bytes(12) + b"\x00")


def visual_sample_entry(fmt: bytes, width: int, height: int) -> bytes:
    payload = (
        bytes(6)
        + (1).to_bytes(2, "big")
        + bytes(2)
        + bytes(2)
        + bytes(12)
        + width.to_bytes(2, "big")
        + height.to_bytes(2, "big")
        + (0x00480000).to_bytes(4, "big")
        + (0x00480000).to_bytes(4, "big")
        + bytes(4)
        + (1).to_bytes(2, "big")
        + bytes(32)
        + (0x0018).to_bytes(2, "big")
        + b"\xff\xff"
    )
    return box(fmt, payload)


def audio_sample_entry() -> bytes:
    payload = (
        bytes(6)
        + (1).to_bytes(2, "big")
        + bytes(8)
        + (2).to_bytes(2, "big")
        + (16).to_bytes(2, "big")
        + bytes(4)
        + (48_000 << 16).to_bytes(4, "big")
    )
    return box(b"mp4a", payload)


def stsd(entry: bytes) -> bytes:
    return full_box(b"stsd", 0, (1).to_bytes(4, "big") + entry)


def stts(entries: list[tuple[int, int]]) -> bytes:
    payload = len(entries).to_bytes(4, "big")
    for count, delta in entries:
        payload += count.to_bytes(4, "big") + delta.to_bytes(4, "big")
    return full_box(b"stts", 0, payload)


def stbl(sample_entry: bytes, time_to_sample: list[tuple[int, int]]) -> bytes:
    return box(b"stbl", stsd(sample_entry) + stts(time_to_sample))


def minf(table: bytes) -> bytes:
    return box(b"minf", box(b"dinf", b"") + table)


def mdia(handler: bytes, timescale: int, duration: int, table: bytes) -> bytes:
    return box(b"mdia", mdhd(timescale, duration) + hdlr(handler) + minf(table))


def video_track(
    *,
    track_id: int = 1,
    movie_duration: int = 2000,
    width: int = 1080,
    height: int = 1920,
    sample_width: int | None = None,
    sample_height: int | None = None,
    timescale: int = 30_000,
    samples: list[tuple[int, int]] | None = None,
) -> bytes:
    entries = samples if samples is not None else [(60, 1000)]
    table = stbl(
        visual_sample_entry(
            b"avc1",
            width if sample_width is None else sample_width,
            height if sample_height is None else sample_height,
        ),
        entries,
    )
    duration = sum(count * delta for count, delta in entries)
    return box(
        b"trak",
        tkhd(track_id, movie_duration, width, height) + mdia(b"vide", timescale, duration, table),
    )


def audio_track(track_id: int = 2, movie_duration: int = 2000) -> bytes:
    table = stbl(audio_sample_entry(), [(94, 1024)])
    return box(
        b"trak",
        tkhd(track_id, movie_duration, 0, 0) + mdia(b"soun", 48_000, 96_256, table),
    )


def _unity_matrix() -> bytes:
    values = [0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000]
    return b"".join(value.to_bytes(4, "big") for value in values)


def mp4_constant_rate() -> bytes:
    movie = box(b"moov", mvhd(1000, 2000) + video_track() + audio_track())
    return ftyp() + movie + box(b"mdat", bytes(8))


def mp4_variable_rate() -> bytes:
    track = video_track(samples=[(10, 1500), (20, 1000)], movie_duration=3500)
    return ftyp() + box(b"moov", mvhd(1000, 3500) + track)


def mp4_track_header_dimensions() -> bytes:
    """A video track whose sample description omits usable coded dimensions."""
    table = stbl(box(b"avc1", bytes(20)), [(24, 1000)])
    track = box(b"trak", tkhd(1, 1000, 640, 360) + mdia(b"vide", 24_000, 24_000, table))
    return ftyp() + box(b"moov", mvhd(1000, 1000) + track)


def mp4_large_size_moov() -> bytes:
    payload = mvhd(1000, 2000) + video_track() + audio_track()
    return ftyp() + large_box(b"moov", payload)


def mp4_unknown_duration() -> bytes:
    return ftyp() + box(b"moov", mvhd(1000, 0xFFFFFFFF) + video_track())


def mp4_truncated() -> bytes:
    complete = mp4_constant_rate()
    return complete[: len(ftyp()) + 64]


def mp4_oversize_claim() -> bytes:
    payload = mvhd(1000, 2000)
    claimed = (0x7FFFFFFF).to_bytes(4, "big") + b"moov" + payload
    return ftyp() + claimed


def mp4_bad_box_size() -> bytes:
    return ftyp() + (3).to_bytes(4, "big") + b"moov"


def mp4_no_movie() -> bytes:
    return ftyp() + box(b"mdat", bytes(16))


def mp4_two_video_tracks() -> bytes:
    """Two declared video tracks, so the selected one must be disclosed."""
    first = video_track(track_id=1, width=640, height=360)
    second = video_track(track_id=2, width=1080, height=1920)
    return ftyp() + box(b"moov", mvhd(1000, 2000) + first + second)


def mp4_truncated_duration() -> bytes:
    """A movie duration that floors to one millisecond below its declared value."""
    track = video_track(movie_duration=2000, timescale=90_000, samples=[(60, 3000)])
    return ftyp() + box(b"moov", mvhd(90_000, 179_999) + track)


def mp4_size_zero_inner_box() -> bytes:
    """A ``size == 0`` box inside ``moov``, which would swallow the video track."""
    payload = bytes([0]) + b"\x00\x00\x00" + (0).to_bytes(4, "big") * 2
    payload += (1000).to_bytes(4, "big") + (2000).to_bytes(4, "big") + bytes(80)
    swallowing = (0).to_bytes(4, "big") + b"mvhd" + payload
    return ftyp() + box(b"moov", swallowing + video_track())


def mp4_many_boxes() -> bytes:
    """More top-level boxes than the bounded element budget allows."""
    return ftyp() + box(b"free", b"") * 4200


# --- Matroska / WebM --------------------------------------------------------


def vint(value: int) -> bytes:
    for length in range(1, 9):
        limit = (1 << (7 * length)) - 1
        if value < limit:
            marker = 1 << (7 * length)
            return (marker | value).to_bytes(length, "big")
    raise ValueError("element size exceeds the EBML bound")


def element(identifier: bytes, payload: bytes) -> bytes:
    return identifier + vint(len(payload)) + payload


def uint_element(identifier: bytes, value: int) -> bytes:
    width = max(1, (value.bit_length() + 7) // 8)
    return element(identifier, value.to_bytes(width, "big"))


def float_element(identifier: bytes, value: float) -> bytes:
    return element(identifier, struct.pack(">d", value))


def string_element(identifier: bytes, value: str) -> bytes:
    return element(identifier, value.encode("ascii"))


def ebml_header(doc_type: str) -> bytes:
    payload = (
        uint_element(b"\x42\x86", 1)
        + uint_element(b"\x42\xf7", 1)
        + uint_element(b"\x42\xf2", 4)
        + uint_element(b"\x42\xf3", 8)
        + string_element(b"\x42\x82", doc_type)
        + uint_element(b"\x42\x87", 2)
        + uint_element(b"\x42\x85", 2)
    )
    return element(b"\x1a\x45\xdf\xa3", payload)


def track_entry(
    *,
    number: int,
    kind: int,
    codec: str,
    width: int = 0,
    height: int = 0,
    default_duration: int = 0,
) -> bytes:
    payload = (
        uint_element(b"\xd7", number)
        + uint_element(b"\x73\xc5", number)
        + uint_element(b"\x83", kind)
        + string_element(b"\x86", codec)
    )
    if default_duration:
        payload += uint_element(b"\x23\xe3\x83", default_duration)
    if width and height:
        payload += element(b"\xe0", uint_element(b"\xb0", width) + uint_element(b"\xba", height))
    return element(b"\xae", payload)


def webm_constant_rate() -> bytes:
    info = element(
        b"\x15\x49\xa9\x66",
        uint_element(b"\x2a\xd7\xb1", 1_000_000) + float_element(b"\x44\x89", 3000.0),
    )
    tracks = element(
        b"\x16\x54\xae\x6b",
        track_entry(
            number=1,
            kind=1,
            codec="V_VP9",
            width=1280,
            height=720,
            default_duration=33_333_333,
        )
        + track_entry(number=2, kind=2, codec="A_OPUS"),
    )
    return ebml_header("webm") + element(b"\x18\x53\x80\x67", info + tracks)


def webm_default_timescale() -> bytes:
    """A Matroska ``Info`` that omits ``TimestampScale`` and takes the format default."""
    info = element(b"\x15\x49\xa9\x66", float_element(b"\x44\x89", 3000.0))
    tracks = element(
        b"\x16\x54\xae\x6b",
        track_entry(number=1, kind=1, codec="V_VP9", width=640, height=360),
    )
    return ebml_header("webm") + element(b"\x18\x53\x80\x67", info + tracks)


def webm_video_only() -> bytes:
    info = element(b"\x15\x49\xa9\x66", uint_element(b"\x2a\xd7\xb1", 1_000_000))
    tracks = element(
        b"\x16\x54\xae\x6b",
        track_entry(number=1, kind=1, codec="V_VP8", width=640, height=640),
    )
    return ebml_header("matroska") + element(b"\x18\x53\x80\x67", info + tracks)


def webm_oversize_element() -> bytes:
    info = element(b"\x15\x49\xa9\x66", uint_element(b"\x2a\xd7\xb1", 1_000_000))
    claimed = b"\x15\x49\xa9\x66" + vint(4096)
    return ebml_header("webm") + element(b"\x18\x53\x80\x67", info) + claimed


def unsupported_container() -> bytes:
    return b"RIFF" + (32).to_bytes(4, "little") + b"AVI LIST" + bytes(16)


FIXTURES = {
    "image-solid-16x16.png": solid_image,
    "image-quadrants-32x32.png": quadrant_image,
    "image-text-band-64x64.png": text_band_image,
    "image-gradient-36x24.jpeg": gradient_image,
    "image-banner-40x10.webp": banner_image,
    "image-tiny-2x2.png": tiny_image,
    "image-truncated.png": truncated_image,
    "video-mp4-constant-rate.mp4": mp4_constant_rate,
    "video-mp4-variable-rate.mp4": mp4_variable_rate,
    "video-mp4-track-header-dimensions.mp4": mp4_track_header_dimensions,
    "video-mp4-large-size-moov.mp4": mp4_large_size_moov,
    "video-mp4-unknown-duration.mp4": mp4_unknown_duration,
    "video-mp4-truncated.mp4": mp4_truncated,
    "video-mp4-truncated-duration.mp4": mp4_truncated_duration,
    "video-mp4-two-video-tracks.mp4": mp4_two_video_tracks,
    "video-mp4-size-zero-inner-box.mp4": mp4_size_zero_inner_box,
    "video-mp4-oversize-claim.mp4": mp4_oversize_claim,
    "video-mp4-bad-box-size.mp4": mp4_bad_box_size,
    "video-mp4-no-movie.mp4": mp4_no_movie,
    "video-mp4-many-boxes.mp4": mp4_many_boxes,
    "video-webm-constant-rate.webm": webm_constant_rate,
    "video-webm-default-timescale.webm": webm_default_timescale,
    "video-webm-video-only.webm": webm_video_only,
    "video-webm-oversize-element.webm": webm_oversize_element,
    "video-unsupported.bin": unsupported_container,
}


def expectations() -> dict[str, object]:
    """Record the exact extraction result, or typed failure, for every fixture."""
    from adhelm.creative_extraction import extract_image_features, extract_video_metadata
    from adhelm.errors import AdhelmError

    results: dict[str, object] = {}
    for name in sorted(FIXTURES):
        data = (DIRECTORY / name).read_bytes()
        extract = extract_image_features if name.startswith("image-") else extract_video_metadata
        try:
            results[name] = {
                "outcome": "EXTRACTED",
                "record": extract(data).model_dump(mode="json"),
            }
        except AdhelmError as error:
            results[name] = {"outcome": "REFUSED", "code": error.code, "exit_code": error.exit_code}
    return results


def main() -> None:
    manifest: dict[str, dict[str, object]] = {}
    for name, builder in sorted(FIXTURES.items()):
        data = builder()
        (DIRECTORY / name).write_bytes(data)
        manifest[name] = {
            "byte_size": len(data),
            "content_digest": hashlib.sha256(data).hexdigest(),
        }
    (DIRECTORY / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "fixtures": manifest}, indent=2, sort_keys=True) + "\n"
    )
    (DIRECTORY / "expected-extraction.json").write_text(
        json.dumps({"schema_version": 1, "results": expectations()}, indent=2, sort_keys=True)
        + "\n"
    )


if __name__ == "__main__":
    main()
