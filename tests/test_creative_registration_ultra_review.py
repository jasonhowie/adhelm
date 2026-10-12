"""Independent adversarial coverage for the installed creative workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

import adhelm.creative_registration as registration_module
from adhelm.creative_registration import (
    CreativeRegistrationReceipt,
    creative_catalog_entries,
    creative_registration_receipt_preview,
    creative_show_data,
    prepare_creative_registration,
    register_creative_request,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger


def _component(text: str) -> dict[str, object]:
    return {
        "component_id": "headline:private",
        "component_role": "HEADLINE",
        "ordinal": 0,
        "exact_text": text,
        "normalized_text": text.casefold(),
        "language": "en-US",
        "provider_slot": "headline",
        "provenance": "DECLARED",
        "confidence_basis_points": 10_000,
        "extraction_status": "DECLARED",
    }


def _text_request(
    *,
    family_id: str = "family:private",
    family_label: str = "PRIVATE-FAMILY-LABEL",
    destination_ref: str = "PRIVATE-DESTINATION",
    text: str = "PRIVATE-EXACT-COPY",
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "creative_family_id": family_id,
        "family_label": family_label,
        "provider_format": "LOCAL_TEXT",
        "locale": "en-US",
        "destination_ref": destination_ref,
        "components": [_component(text)],
        "created_at": created_at,
        "source": {"registration_kind": "TEXT_ONLY"},
    }


def test_forged_result_cannot_substitute_a_real_different_retained_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid digest is insufficient unless it names this exact closed graph."""
    first = register_creative_request(
        tmp_path,
        _text_request(
            family_id="family:first",
            family_label="PRIVATE-FIRST-FAMILY",
            destination_ref="PRIVATE-FIRST-DESTINATION",
            text="PRIVATE-FIRST-COPY",
        ),
    )
    original_append = Ledger.append_creative_registration

    def forged_append(self: Ledger, closure: object) -> dict[str, object]:
        checked = cast(Any, closure)
        return {
            "status": "REGISTERED",
            "creative_version_id": checked.version.creative_version_id,
            "record_digests": {"CREATIVE_VERSION": first.record_digest},
        }

    monkeypatch.setattr(Ledger, "append_creative_registration", forged_append)
    with pytest.raises(AdhelmError) as raised:
        register_creative_request(
            tmp_path,
            _text_request(
                family_id="family:second",
                family_label="PRIVATE-SECOND-FAMILY",
                destination_ref="PRIVATE-SECOND-DESTINATION",
                text="PRIVATE-SECOND-COPY",
            ),
        )

    assert raised.value.code == "CREATIVE_REGISTRATION_UNVERIFIED"
    assert raised.value.exit_code == 6
    assert "PRIVATE" not in raised.value.message
    monkeypatch.setattr(Ledger, "append_creative_registration", original_append)
    versions = [
        row
        for row in Ledger(tmp_path).records("creative")
        if row["record_kind"] == "CREATIVE_VERSION"
    ]
    assert [row["record_digest"] for row in versions] == [first.record_digest]


def test_final_ledger_checkpoint_failure_after_first_child_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct multi-record append must not expose a retryable private failure."""
    original_checkpoint = Ledger.checkpoint
    calls = 0

    def fail_ledger_final_checkpoint(self: Ledger) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AdhelmError("PRIVATE_FINAL_CHECK", "PRIVATE checkpoint detail", exit_code=2)
        return original_checkpoint(self)

    monkeypatch.setattr(Ledger, "checkpoint", fail_ledger_final_checkpoint)
    with pytest.raises(AdhelmError) as raised:
        register_creative_request(tmp_path, _text_request())

    assert raised.value.code == "CREATIVE_REGISTRATION_UNVERIFIED"
    assert raised.value.exit_code == 6
    assert "PRIVATE" not in raised.value.message
    monkeypatch.setattr(Ledger, "checkpoint", original_checkpoint)
    assert any(
        row["record_kind"] == "CREATIVE_VERSION" for row in Ledger(tmp_path).records("creative")
    )


def test_invalid_raw_request_is_rejected_before_checkpoint_or_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _text_request()
    raw["schema_version"] = 1.0
    calls = {"checkpoint": 0, "read_image": 0}

    def unexpected_checkpoint(self: Ledger) -> dict[str, object]:
        calls["checkpoint"] += 1
        raise AssertionError("invalid request reached local history")

    def unexpected_read_image(*_args: object, **_kwargs: object) -> bytes:
        calls["read_image"] += 1
        raise AssertionError("text input reached image vault")

    monkeypatch.setattr(Ledger, "checkpoint", unexpected_checkpoint)
    monkeypatch.setattr(registration_module, "read_image", unexpected_read_image)
    with pytest.raises(AdhelmError) as raised:
        prepare_creative_registration(tmp_path, raw)

    assert raised.value.code == "INVALID_CREATIVE_REGISTRATION"
    assert calls == {"checkpoint": 0, "read_image": 0}


def test_text_registration_never_reads_an_image_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_read_image(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("text-only registration read an image vault object")

    monkeypatch.setattr(registration_module, "read_image", unexpected_read_image)
    receipt = register_creative_request(tmp_path, _text_request())

    assert receipt.registration_kind == "TEXT_ONLY"
    assert receipt.rendition_id is None
    assert receipt.fingerprint_id is None


def test_public_receipt_rejects_constructed_kind_and_control_forgery() -> None:
    raw = creative_registration_receipt_preview()
    for field, alias in (("remote_mutations", 0), ("execution_authority", 0.0)):
        with pytest.raises(ValidationError):
            CreativeRegistrationReceipt.model_validate_json(json.dumps({**raw, field: alias}))
    for forged in (
        CreativeRegistrationReceipt.model_construct(**{**raw, "registration_kind": "TEXT_ONLY"}),
        CreativeRegistrationReceipt.model_construct(
            **{key: value for key, value in raw.items() if key != "record_digest"}
        ),
    ):
        with pytest.raises(ValueError, match="Creative registration receipt is invalid"):
            forged.model_dump(mode="json", warnings="error")


def test_catalog_and_show_are_redacted_historical_no_authority_views(tmp_path: Path) -> None:
    receipt = register_creative_request(tmp_path, _text_request())
    rows, total, cursor = creative_catalog_entries(tmp_path, cursor=0, limit=50)
    shown = creative_show_data(tmp_path, receipt.record_digest)

    assert total == 1
    assert cursor is None
    assert rows == [shown]
    for view in (rows[0], shown):
        assert view["remote_mutations"] is False
        assert view["execution_authority"] is False
        assert view["provider_currentness"] == "NOT_EVALUATED"
        assert view["historical_evidence_status"] == "LOCAL_RETAINED_HISTORY"
        rendered = json.dumps(view, sort_keys=True)
        for private_value in (
            "PRIVATE-FAMILY-LABEL",
            "PRIVATE-DESTINATION",
            "PRIVATE-EXACT-COPY",
            "family:private",
        ):
            assert private_value not in rendered
