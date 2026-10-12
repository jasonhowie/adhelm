"""Closed local creative-registration workflow coverage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image
from pydantic import ValidationError

from adhelm import __version__
from adhelm.canonical import canonical_bytes
from adhelm.creative_registration import (
    CreativeImageAnalysisReceipt,
    CreativeRegistrationReceipt,
    creative_catalog_entries,
    creative_registration_receipt_preview,
    creative_show_data,
    prepare_creative_registration,
    register_creative_request,
    retain_local_image_analysis,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import ResultEnvelope


def _png(colour: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 2), colour).save(output, format="PNG")
    return output.getvalue()


def _component(text: str = "Private headline") -> dict[str, object]:
    return {
        "component_id": "component:headline",
        "component_role": "HEADLINE",
        "ordinal": 0,
        "exact_text": text,
        "normalized_text": text,
        "language": "en-US",
        "provider_slot": "headline",
        "provenance": "DECLARED",
        "confidence_basis_points": 10_000,
        "extraction_status": "DECLARED",
    }


def _image_request(
    source: dict[str, object],
    *,
    family_id: str = "family:local",
    created_at: str = "2026-01-01T00:00:00Z",
    text: str = "Private headline",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "creative_family_id": family_id,
        "family_label": "Private family label",
        "provider_format": "LOCAL_IMAGE",
        "locale": "en-US",
        "destination_ref": "destination:private",
        "components": [_component(text)],
        "created_at": created_at,
        "source": source,
    }


def _text_request(*, created_at: str = "2026-01-01T00:00:00Z") -> dict[str, object]:
    return {
        **_image_request({"registration_kind": "TEXT_ONLY"}, created_at=created_at),
        "provider_format": "LOCAL_TEXT",
    }


def _image_source(receipt: object) -> dict[str, object]:
    data = receipt.model_dump(mode="json", warnings="error")
    return {
        "registration_kind": "IDENTITY_IMAGE",
        "asset_record_digest": data["asset_record_digest"],
        "asset_analysis_record_digest": data["asset_analysis_record_digest"],
    }


def test_identity_image_registration_rechecks_vault_bytes_and_exact_sources(tmp_path: Path) -> None:
    source = retain_local_image_analysis(tmp_path, _png("#123456"))
    request = _image_request(_image_source(source))

    prepared = prepare_creative_registration(tmp_path, request)
    assert prepared.rendition.source_relation_status == "PROVEN_SOURCE_ASSET"
    assert prepared.fingerprint.analysis_surface == "FINAL_RENDITION"

    result = register_creative_request(tmp_path, request)
    assert result.local_registration_status == "RETAINED_VERIFIED_LOCAL_ONLY"
    assert result.rendition_id == prepared.rendition.rendition_id
    assert result.fingerprint_id == prepared.fingerprint.fingerprint_id

    asset_path = tmp_path / "assets" / "sha256" / source.asset_content_digest
    asset_path.write_bytes(b"not the retained image")
    with pytest.raises(AdhelmError) as raised:
        prepare_creative_registration(tmp_path, request)
    assert raised.value.code in {"ASSET_CORRUPT", "ASSET_CHANGED"}


def test_second_image_version_preserves_family_time_and_uses_declared_version_time(
    tmp_path: Path,
) -> None:
    first = retain_local_image_analysis(tmp_path, _png("#123456"))
    first_request = _image_request(_image_source(first), created_at="2026-01-01T00:00:00Z")
    first_receipt = register_creative_request(tmp_path, first_request)

    second = retain_local_image_analysis(tmp_path, _png("#654321"))
    second_request = _image_request(
        _image_source(second),
        created_at="2026-02-01T01:02:03.123456Z",
        text="Second private headline",
    )
    second_receipt = register_creative_request(tmp_path, second_request)

    assert first_receipt.creative_version_id != second_receipt.creative_version_id
    assert second_receipt.version_created_at == datetime(2026, 2, 1, 1, 2, 3, 123456, UTC)
    records = Ledger(tmp_path).checkpoint()["records_by_stream"]["creative"]
    family = next(item for item in records if item["record_kind"] == "CREATIVE_FAMILY")
    assert family["payload"]["created_at"] == "2026-01-01T00:00:00Z"
    second_version = next(
        item
        for item in records
        if item["record_kind"] == "CREATIVE_VERSION"
        and item["record_digest"] == second_receipt.record_digest
    )
    assert second_version["payload"]["created_at"] == "2026-02-01T01:02:03.123456Z"


def test_text_only_registration_is_closed_and_idempotent(tmp_path: Path) -> None:
    request = _text_request()
    first = register_creative_request(tmp_path, request)
    second = register_creative_request(tmp_path, request)

    assert first.registration_kind == "TEXT_ONLY"
    assert first.rendition_id is None
    assert first.fingerprint_id is None
    assert second.record_digest == first.record_digest
    assert second.registration_recovery_status == "ALREADY_REGISTERED"


def test_shaped_append_result_cannot_claim_durable_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def forged_append(self: Ledger, closure: object) -> dict[str, object]:
        captured["closure"] = closure
        version = cast(Any, closure).version
        return {
            "status": "REGISTERED",
            "checkpoint_digest": "0" * 64,
            "creative_version_id": version.creative_version_id,
            "record_digests": {"CREATIVE_VERSION": "0" * 64},
        }

    monkeypatch.setattr(Ledger, "append_creative_registration", forged_append)
    with pytest.raises(AdhelmError) as raised:
        register_creative_request(tmp_path, _text_request())
    assert raised.value.code == "CREATIVE_REGISTRATION_UNVERIFIED"
    assert Ledger(tmp_path).checkpoint()["records_by_stream"]["creative"] == []
    assert "closure" in captured


def test_staged_final_fingerprint_is_visible_and_exact_retry_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = retain_local_image_analysis(tmp_path, _png("#123456"))
    request = _image_request(_image_source(source))
    original = Ledger._append_local_creative_record_locked

    def fail_fingerprint(
        self: Ledger, kind: str, payload: dict[str, Any], state: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if kind == "CREATIVE_FINGERPRINT":
            raise OSError("test post-version failure")
        return original(self, kind, payload, state)

    monkeypatch.setattr(Ledger, "_append_local_creative_record_locked", fail_fingerprint)
    with pytest.raises(AdhelmError) as raised:
        register_creative_request(tmp_path, request)
    assert raised.value.code == "CREATIVE_REGISTRATION_UNVERIFIED"

    staged_record = next(
        item
        for item in Ledger(tmp_path).records("creative")
        if item["record_kind"] == "CREATIVE_VERSION"
    )
    rows, total, _ = creative_catalog_entries(tmp_path, cursor=0, limit=50)
    assert total == 1
    assert rows[0]["final_rendition_evidence"] == "STAGED_FINAL_RENDITION_EVIDENCE_MISSING"
    assert (
        creative_show_data(tmp_path, staged_record["record_digest"])["final_rendition_evidence"]
        == "STAGED_FINAL_RENDITION_EVIDENCE_MISSING"
    )

    monkeypatch.setattr(Ledger, "_append_local_creative_record_locked", original)
    recovered = register_creative_request(tmp_path, request)
    assert recovered.registration_recovery_status == "RECOVERED_STAGING"
    rows, _, _ = creative_catalog_entries(tmp_path, cursor=0, limit=50)
    assert rows[0]["final_rendition_evidence"] == "FINAL_RENDITION_EVIDENCE_RETAINED"


def test_public_analysis_receipt_rejects_raw_false_aliases_and_partial_retention() -> None:
    raw = {
        "schema_version": 1,
        "asset_version_id": "asset:sha256:" + "a" * 64,
        "asset_analysis_id": "asset-analysis:sha256:" + "a" * 64,
        "asset_content_digest": "a" * 64,
        "mime_type": "image/png",
        "width_px": 1,
        "height_px": 1,
        "aspect_ratio": "1:1",
        "palette_color_count": 1,
        "observation_persisted": False,
        "asset_record_digest": None,
        "asset_analysis_record_digest": None,
        "asset_availability": "NOT_RETAINED_EPHEMERAL",
        "analysis_provenance": "LOCAL_EXTRACTED",
        "remote_mutations": False,
        "execution_authority": False,
    }
    for key, value in (("remote_mutations", 0), ("execution_authority", 0.0)):
        with pytest.raises(ValidationError):
            CreativeImageAnalysisReceipt.model_validate_json(json.dumps({**raw, key: value}))
    with pytest.raises(ValidationError):
        CreativeImageAnalysisReceipt.model_validate_json(
            json.dumps({**raw, "asset_record_digest": "a" * 64})
        )


def test_public_registration_receipt_rejects_false_aliases_and_constructed_controls() -> None:
    raw = creative_registration_receipt_preview()
    for key, value in (("remote_mutations", 0), ("execution_authority", 0.0)):
        with pytest.raises(ValidationError):
            CreativeRegistrationReceipt.model_validate_json(json.dumps({**raw, key: value}))
    for forged in (
        CreativeRegistrationReceipt.model_construct(**{**raw, "execution_authority": True}),
        CreativeRegistrationReceipt.model_validate_json(json.dumps(raw)).model_copy(
            update={"remote_mutations": True}
        ),
    ):
        with pytest.raises(ValueError, match="Creative registration receipt is invalid"):
            forged.model_dump(mode="json", warnings="error")


def test_registration_receipt_preview_fits_minimum_output_with_microseconds() -> None:
    preview = creative_registration_receipt_preview()
    envelope = ResultEnvelope(
        tool_version=__version__, command="creative register", outcome="OK", data=preview
    )
    assert len(canonical_bytes(envelope)) + 1 <= 1024
    assert preview["registration_recovery_status"] == "ALREADY_REGISTERED"
    assert preview["version_created_at"].endswith(".999999Z")


def test_post_append_readback_ambiguity_is_fixed_and_never_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_append = Ledger.append_creative_registration
    original_checkpoint = Ledger.checkpoint
    appended = {"value": False}

    def append_then_mark(self: Ledger, closure: object) -> dict[str, object]:
        result = original_append(self, closure)  # type: ignore[arg-type]
        appended["value"] = True
        return result

    def fail_only_post_append_checkpoint(self: Ledger) -> dict[str, object]:
        if appended["value"]:
            raise AdhelmError("PRIVATE_POST_APPEND", "private post-append detail", exit_code=2)
        return original_checkpoint(self)

    monkeypatch.setattr(Ledger, "append_creative_registration", append_then_mark)
    monkeypatch.setattr(Ledger, "checkpoint", fail_only_post_append_checkpoint)
    with pytest.raises(AdhelmError) as raised:
        register_creative_request(tmp_path, _text_request())
    assert raised.value.code == "CREATIVE_REGISTRATION_UNVERIFIED"
    assert "PRIVATE_POST_APPEND" not in raised.value.message

    monkeypatch.setattr(Ledger, "checkpoint", original_checkpoint)
    assert any(
        item["record_kind"] == "CREATIVE_VERSION" for item in Ledger(tmp_path).records("creative")
    )
