"""Golden local image-structure extraction: deterministic, bounded, and honest.

The tests use only checked-in synthetic fixtures and temporary local files.
They make no provider, credential-store, network, model, or OCR call.
"""

from __future__ import annotations

import hashlib
import json
import random
from importlib import util
from pathlib import Path
from types import ModuleType

import pytest

from adhelm import assets
from adhelm.canonical import canonical_bytes
from adhelm.creative import AssetAnalysisRecord, CreativeAssetVersion, analyze_image
from adhelm.creative_extraction import (
    MAX_IMAGE_BYTES,
    AssetStructureRecord,
    ImageStructureFeatures,
    extract_image_features,
    extract_vault_image_features,
    extract_video_metadata,
)
from adhelm.errors import AdhelmError
from adhelm.privacy import validate_data

FIXTURES = Path(__file__).parent / "fixtures" / "creative"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())["fixtures"]
EXPECTED = json.loads((FIXTURES / "expected-extraction.json").read_text())["results"]
MEDIA = sorted(MANIFEST)
IMAGES = [name for name in MEDIA if name.startswith("image-")]


def generator() -> ModuleType:
    """Load the checked-in fixture builders without putting them on the path."""
    spec = util.spec_from_file_location(
        "adhelm_creative_fixture_generator", FIXTURES / "generate.py"
    )
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract(name: str, data: bytes) -> object:
    return (
        extract_image_features(data) if name.startswith("image-") else extract_video_metadata(data)
    )


def test_checked_in_fixture_bytes_match_their_recorded_manifest() -> None:
    present = sorted(
        item.name
        for item in FIXTURES.iterdir()
        if item.is_file()
        and item.name not in {"generate.py", "manifest.json", "expected-extraction.json"}
    )
    assert present == MEDIA
    total = 0
    for name in MEDIA:
        data = (FIXTURES / name).read_bytes()
        total += len(data)
        assert MANIFEST[name]["byte_size"] == len(data)
        assert MANIFEST[name]["content_digest"] == hashlib.sha256(data).hexdigest()
    assert total < 200 * 1024


@pytest.mark.parametrize("name", MEDIA)
def test_golden_extraction_matches_the_checked_in_expectation(name: str) -> None:
    data = (FIXTURES / name).read_bytes()
    expected = EXPECTED[name]
    if expected["outcome"] == "REFUSED":
        with pytest.raises(AdhelmError) as error:
            extract(name, data)
        assert error.value.code == expected["code"]
        assert error.value.exit_code == expected["exit_code"]
        return
    record = extract(name, data)
    assert record.model_dump(mode="json") == expected["record"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "name", [item for item in MEDIA if EXPECTED[item]["outcome"] == "EXTRACTED"]
)
def test_repeated_extraction_is_byte_identical(name: str) -> None:
    data = (FIXTURES / name).read_bytes()
    results = {canonical_bytes(extract(name, data)) for _ in range(3)}
    assert len(results) == 1


def test_layout_grid_locates_a_text_band_without_extracting_any_text() -> None:
    features = extract_image_features((FIXTURES / "image-text-band-64x64.png").read_bytes())
    assert features.layout_status == "EXTRACTED"
    assert len(features.layout_cells) == 16
    dense = {
        (cell.row, cell.column)
        for cell in features.layout_cells
        if cell.edge_density_basis_points >= 1_000
    }
    assert dense == {(3, 0), (3, 1), (3, 2)}
    assert features.text_presence == "TEXT_LIKELY"
    assert features.extracted_text_status == "NOT_EXTRACTED_NO_LOCAL_OCR"
    payload = features.model_dump(mode="json")
    assert set(payload) & {"pixels", "text", "exact_text", "thumbnail", "preview"} == set()
    assert payload["pixel_data_status"] == "NOT_RETAINED"


def test_high_contrast_blocks_are_not_reported_as_likely_text() -> None:
    features = extract_image_features((FIXTURES / "image-quadrants-32x32.png").read_bytes())
    assert features.contrast_basis_points > 9_000
    assert features.text_presence == "TEXT_UNLIKELY"
    assert features.text_presence_basis_points == 0
    corners = {
        (cell.row, cell.column): cell.mean_luminance_basis_points for cell in features.layout_cells
    }
    assert corners[(0, 0)] == 10_000
    assert corners[(0, 3)] == 0


def test_an_image_smaller_than_the_grid_reports_an_unevaluated_layout() -> None:
    features = extract_image_features((FIXTURES / "image-tiny-2x2.png").read_bytes())
    assert features.layout_status == "NOT_EVALUATED_IMAGE_TOO_SMALL"
    assert features.layout_cells == ()
    assert features.text_presence == "NOT_EVALUATED"
    assert features.text_presence_basis_points is None


@pytest.mark.parametrize("name", IMAGES)
def test_identity_and_biometric_fields_are_always_explicit_non_evaluations(name: str) -> None:
    expected = EXPECTED[name]
    if expected["outcome"] != "EXTRACTED":
        pytest.skip("The corrupted fixture never produces a record")
    record = expected["record"]
    assert record["face_presence_status"] == "NOT_EVALUATED_NO_BIOMETRIC_INFERENCE"
    assert record["identity_inference_status"] == "NOT_EVALUATED_PROHIBITED"
    assert record["protected_trait_inference_status"] == "NOT_EVALUATED_PROHIBITED"
    assert record["logo_presence_status"] == "NOT_EVALUATED_NO_LOCAL_DETECTOR"
    assert record["extracted_text_status"] == "NOT_EXTRACTED_NO_LOCAL_OCR"


def test_feature_identity_binds_every_extracted_value() -> None:
    features = extract_image_features((FIXTURES / "image-solid-16x16.png").read_bytes())
    assert ImageStructureFeatures(**features.model_dump()) == features
    for change in (
        {"text_presence": "TEXT_LIKELY"},
        {"width_px": 32, "aspect_ratio": "2:1"},
        {"contrast_basis_points": features.contrast_basis_points + 1},
        {"layout_status": "NOT_EVALUATED_IMAGE_TOO_SMALL", "layout_cells": ()},
    ):
        forged = features.model_copy(update=change).model_dump()
        with pytest.raises(ValueError):
            ImageStructureFeatures(**forged)


def test_shared_analysis_fields_stay_byte_identical_with_the_existing_analyzer() -> None:
    data = (FIXTURES / "image-gradient-36x24.jpeg").read_bytes()
    features = extract_image_features(data)
    assert canonical_bytes(features.to_image_analysis()) == canonical_bytes(analyze_image(data))


def test_structure_binds_a_retained_asset_analysis_as_a_sibling_record() -> None:
    data = (FIXTURES / "image-quadrants-32x32.png").read_bytes()
    analysis = analyze_image(data)
    asset = CreativeAssetVersion.from_image(data, analysis)
    retained = AssetAnalysisRecord.from_analysis(asset, analysis)
    features = extract_image_features(data)
    record = AssetStructureRecord.from_asset_analysis(retained, features)
    assert record.asset_analysis_ref == retained.asset_analysis_id
    assert record.asset_version_ref == asset.asset_version_id
    assert record.asset_content_digest == asset.content_digest
    assert AssetStructureRecord(**record.model_dump()) == record
    # The retained analysis payload and identity are untouched by the sibling.
    assert canonical_bytes(retained) == canonical_bytes(
        AssetAnalysisRecord.from_analysis(asset, analysis)
    )


def test_structure_cannot_bind_a_different_asset_analysis() -> None:
    first = (FIXTURES / "image-quadrants-32x32.png").read_bytes()
    second = (FIXTURES / "image-solid-16x16.png").read_bytes()
    analysis = analyze_image(first)
    retained = AssetAnalysisRecord.from_analysis(
        CreativeAssetVersion.from_image(first, analysis), analysis
    )
    with pytest.raises(AdhelmError) as error:
        AssetStructureRecord.from_asset_analysis(retained, extract_image_features(second))
    assert error.value.code == "ASSET_STRUCTURE_MISMATCH"
    assert error.value.exit_code == 2


def test_the_private_vault_reader_produces_the_same_features(tmp_path: Path) -> None:
    data = (FIXTURES / "image-banner-40x10.webp").read_bytes()
    root = tmp_path / "vault"
    assets.store_image(root, data, hashlib.sha256(data).hexdigest())
    features = extract_vault_image_features(root, hashlib.sha256(data).hexdigest())
    assert canonical_bytes(features) == canonical_bytes(extract_image_features(data))


def test_a_missing_vault_object_fails_before_extraction(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError) as error:
        extract_vault_image_features(tmp_path / "vault", "0" * 64)
    assert error.value.code in {"ASSET_NOT_AVAILABLE", "ASSET_STORAGE_UNSUPPORTED"}


def test_every_persisted_record_passes_the_sensitive_text_screen() -> None:
    for name in MEDIA:
        expectation = EXPECTED[name]
        if expectation["outcome"] == "EXTRACTED":
            validate_data(expectation["record"])


def test_container_free_text_reaching_a_record_is_screened_before_it_exists() -> None:
    """A hostile container label cannot become a persisted record field."""
    build = generator()
    hostile = build.ebml_header("a@b.co") + build.element(
        b"\x18\x53\x80\x67",
        build.element(b"\x15\x49\xa9\x66", build.uint_element(b"\x2a\xd7\xb1", 1_000_000)),
    )
    with pytest.raises(AdhelmError) as error:
        extract_video_metadata(hostile)
    assert error.value.code == "SENSITIVE_DATA_RISK"


def test_oversized_image_bytes_fail_closed_before_decoding() -> None:
    with pytest.raises(AdhelmError) as error:
        extract_image_features(b"\x89PNG\r\n\x1a\n" * 4, maximum_bytes=8)
    assert error.value.code == "CREATIVE_MEDIA_TOO_LARGE"
    assert error.value.exit_code == 2
    assert MAX_IMAGE_BYTES == 20 * 1024 * 1024


def test_a_pixel_bound_refusal_never_returns_a_partial_record() -> None:
    with pytest.raises(AdhelmError) as error:
        extract_image_features(
            (FIXTURES / "image-quadrants-32x32.png").read_bytes(), maximum_pixels=16
        )
    assert error.value.code == "CREATIVE_IMAGE_TOO_LARGE"


def test_the_aspect_ratio_class_boundaries_are_exact() -> None:
    """Lock the cut points that ``_image_parameters_digest`` also states literally."""
    from adhelm.creative_extraction import _aspect_ratio_class

    assert _aspect_ratio_class(1600, 1000) == "LANDSCAPE_WIDE"
    assert _aspect_ratio_class(1599, 1000) == "LANDSCAPE"
    assert _aspect_ratio_class(1050, 1000) == "LANDSCAPE"
    assert _aspect_ratio_class(1049, 1000) == "SQUARE"
    assert _aspect_ratio_class(950, 1000) == "SQUARE"
    assert _aspect_ratio_class(949, 1000) == "PORTRAIT"
    assert _aspect_ratio_class(625, 1000) == "PORTRAIT"
    assert _aspect_ratio_class(624, 1000) == "PORTRAIT_TALL"


def test_an_image_above_the_structure_budget_reports_an_unevaluated_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adhelm.creative_extraction as extraction

    monkeypatch.setattr(extraction, "MAX_STRUCTURE_PIXELS", 4)
    features = extract_image_features((FIXTURES / "image-quadrants-32x32.png").read_bytes())
    assert features.layout_status == "NOT_EVALUATED_ABOVE_STRUCTURE_BUDGET"
    assert features.layout_cells == ()
    assert features.text_presence == "NOT_EVALUATED"
    assert features.contrast_basis_points > 9_000


def test_an_exif_orientation_tag_is_disclosed_and_never_applied() -> None:
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (32, 16), (255, 255, 255))
    for x in range(16, 32):
        for y in range(16):
            image.putpixel((x, y), (0, 0, 0))
    upright = BytesIO()
    image.save(upright, format="JPEG", quality=95)
    exif = image.getexif()
    exif[274] = 6
    rotated = BytesIO()
    image.save(rotated, format="JPEG", quality=95, exif=exif)

    plain = extract_image_features(upright.getvalue())
    tagged = extract_image_features(rotated.getvalue())
    assert plain.exif_orientation_status == "NOT_PRESENT"
    assert plain.exif_orientation is None
    assert tagged.exif_orientation_status == "EXTRACTED"
    assert tagged.exif_orientation == 6
    assert plain.layout_measured_in == tagged.layout_measured_in == "STORED_PIXEL_ORIENTATION"
    # The tag is recorded, not applied: identical stored pixels keep one grid.
    assert plain.layout_cells == tagged.layout_cells
    assert plain.image_features_id != tagged.image_features_id


def _mutation_positions(name: str, size: int, *, sample: int = 500) -> list[int]:
    """A deterministic byte-position sample: every position, or a seeded subset."""
    if size <= sample:
        return list(range(size))
    return sorted(random.Random(f"h01-{name}").sample(range(size), sample))


@pytest.mark.parametrize("name", MEDIA)
def test_single_byte_mutations_of_a_fixture_stay_typed(name: str) -> None:
    """Corrupt bytes always produce a record or an ``AdhelmError``, never anything else.

    Third-party decoders and hand-written container parsers fail in whatever way
    their input happens to break them, so this asserts the class of outcome
    rather than one exception type: an untyped escape would abort the CLI with a
    traceback instead of a typed exit code.
    """
    data = (FIXTURES / name).read_bytes()
    outcomes = {"record": 0, "refused": 0}
    for position in _mutation_positions(name, len(data)):
        mutated = bytearray(data)
        mutated[position] ^= 0xFF
        try:
            if name.startswith("image-"):
                # A mutated header can claim a huge canvas; the existing pixel
                # bound keeps the fuzz linear without changing any real result.
                extract_image_features(bytes(mutated), maximum_pixels=65_536)
            else:
                extract_video_metadata(bytes(mutated))
            outcomes["record"] += 1
        except AdhelmError:
            outcomes["refused"] += 1
    assert outcomes["record"] + outcomes["refused"] == len(_mutation_positions(name, len(data)))


def test_the_mutation_sample_is_deterministic_and_covers_every_fixture() -> None:
    total = sum(len(_mutation_positions(name, MANIFEST[name]["byte_size"])) for name in MEDIA)
    assert total >= 500 * sum(1 for name in MEDIA if MANIFEST[name]["byte_size"] > 500)
    assert _mutation_positions("x", 4000) == _mutation_positions("x", 4000)
    assert _mutation_positions("x", 4000) != _mutation_positions("y", 4000)
    assert total > 5_000


def test_an_out_of_range_exif_orientation_is_invalid_rather_than_absent() -> None:
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (32, 16), (200, 40, 40))
    exif = image.getexif()
    exif[274] = 9
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95, exif=exif)
    features = extract_image_features(buffer.getvalue())
    assert features.exif_orientation_status == "INVALID"
    assert features.exif_orientation is None
