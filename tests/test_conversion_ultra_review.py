"""Regression checks found during the conversion-history independent review."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm.canonical import canonical_bytes, digest
from adhelm.conversion_contracts import ConversionParameter, CustomerDeclaredStatement
from adhelm.errors import AdhelmError
from adhelm.ledger import MAX_RECORD_BYTES, MAX_SEGMENT_BYTES, Ledger
from adhelm.projections import export_verified


def _large_snapshot(ordinal: int, *, name_bytes: int = 600_000) -> dict[str, object]:
    # ``@`` cannot begin the privacy screen's email local-part, keeping this
    # storage-boundary fixture linear while still creating a valid large name.
    state = {"name": "@" * name_bytes, "status": "PAUSED"}
    return {
        "snapshot_id": f"conversion-review.snapshot.{ordinal}",
        "provider_api_version": "fixture-v1",
        "connector_version": "fixture",
        "resource_type": "CAMPAIGN",
        "resource_ref": f"conversion-review.resource.{ordinal}",
        "parent_resource_refs": [],
        "remote_fingerprint": f"fixture-fingerprint-{ordinal}",
        "normalized_state": state,
        "normalized_state_digest": digest(state),
        "effective_status": "PAUSED",
        "source_updated_at": None,
        "data_quality_statuses": [],
    }


def test_large_valid_segment_survives_rotation_checkpoint_and_export(tmp_path: Path) -> None:
    """The per-record cap must not truncate a valid configured multi-record segment."""
    ledger = Ledger(tmp_path / "data", segment_bytes=1_500_000)
    for ordinal in range(3):
        ledger.append(
            "changes",
            "RESOURCE_SNAPSHOT",
            _large_snapshot(ordinal),
            provider="google_ads",
            account_ref="fixture-account",
        )

    segments = sorted((tmp_path / "data" / "ledger" / "changes").glob("*.jsonl"))
    assert len(segments) == 2
    assert segments[0].stat().st_size > MAX_RECORD_BYTES

    verified = ledger.verify("changes")
    assert verified["streams"]["changes"]["records"] == 5
    checkpoint = ledger.checkpoint()
    assert checkpoint["streams"]["changes"]["high_watermark"] == 5

    export_dir = tmp_path / "export"
    manifest = export_verified(ledger, export_dir, opaque_key=b"k" * 32)
    assert manifest["files"]["records.jsonl"]["rows"] == 5
    assert (export_dir / "records.jsonl").stat().st_size > MAX_RECORD_BYTES


def test_segment_reader_fails_closed_at_the_configured_bounded_limit(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "data", segment_bytes=1024)
    changes = tmp_path / "data" / "ledger" / "changes"
    changes.mkdir(parents=True)
    path = changes / "00000000000000000001.jsonl"
    path.write_bytes(b"x" * (1024 + (2 * MAX_RECORD_BYTES) + 1))

    with pytest.raises(AdhelmError) as error:
        ledger.records("changes")
    assert error.value.code == "LEDGER_SEGMENT_TOO_LARGE"


@pytest.mark.parametrize("segment_bytes", (True, 1023, 1024.0, float("nan"), MAX_SEGMENT_BYTES + 1))
def test_segment_size_configuration_is_integer_and_bounded(
    segment_bytes: object, tmp_path: Path
) -> None:
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path / "data", segment_bytes=segment_bytes)  # type: ignore[arg-type]
    assert error.value.code == "INVALID_SEGMENT_SIZE"


def test_smallest_segment_accepts_one_near_maximum_lf_terminated_record(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "data", segment_bytes=1024)
    record = ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        _large_snapshot(0, name_bytes=MAX_RECORD_BYTES - 10_000),
        provider="google_ads",
        account_ref="fixture-account",
    )

    assert len(canonical_bytes(record) + b"\n") <= MAX_RECORD_BYTES
    assert ledger.verify("changes")["streams"]["changes"]["records"] == 3


def test_reader_counts_the_lf_when_rejecting_an_oversized_physical_record(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "data")
    ledger._ensure_dirs("changes")
    segment = ledger._new_segment("changes", {"head_digest": None, "last_sequence": 0})
    state = ledger._scan_stream("changes")
    base = ledger._make_record(
        "changes",
        "RESOURCE_SNAPSHOT",
        _large_snapshot(0, name_bytes=0),
        sequence=2,
        previous_digest=state["head_digest"],
        provider="google_ads",
        account_ref="fixture-account",
        run_id=None,
        source="LOCAL_CLI",
        occurred_at=None,
        observed_at=None,
    )
    raw = canonical_bytes(
        ledger._make_record(
            "changes",
            "RESOURCE_SNAPSHOT",
            _large_snapshot(0, name_bytes=MAX_RECORD_BYTES - len(canonical_bytes(base))),
            sequence=2,
            previous_digest=state["head_digest"],
            provider="google_ads",
            account_ref="fixture-account",
            run_id=None,
            source="LOCAL_CLI",
            occurred_at=None,
            observed_at=None,
        )
    )
    assert len(raw) == MAX_RECORD_BYTES
    ledger._append_bytes(segment, raw + b"\n")

    with pytest.raises(AdhelmError) as error:
        ledger.records("changes")
    assert error.value.code == "LEDGER_RECORD_TOO_LARGE"


@pytest.mark.parametrize("segment_bytes", (float("nan"), MAX_SEGMENT_BYTES + 1))
def test_mutated_segment_size_fails_closed_before_reading(
    tmp_path: Path, segment_bytes: object
) -> None:
    ledger = Ledger(tmp_path / "data")
    ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        _large_snapshot(0, name_bytes=1),
        provider="google_ads",
        account_ref="fixture-account",
    )
    ledger.segment_bytes = segment_bytes  # type: ignore[assignment]

    with pytest.raises(AdhelmError) as error:
        ledger.records("changes")
    assert error.value.code == "INVALID_SEGMENT_SIZE"


@pytest.mark.parametrize(
    "name,allowed_values",
    (
        ("diagnosis_type", ("declared",)),
        ("plan_tier", ("cancer",)),
        ("plan_tier", ("cancer-type",)),
        ("plan_tier", ("patient",)),
        ("plan_tier", ("credit",)),
    ),
)
def test_parameter_schema_rejects_sensitive_event_semantics(
    name: str, allowed_values: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        ConversionParameter(
            name=name,
            value_type="LOW_CARDINALITY_ENUM",
            required=True,
            integer_minimum=None,
            integer_maximum=None,
            allowed_values=allowed_values,
        )


def test_customer_statement_cannot_smuggle_a_serialized_event_payload() -> None:
    with pytest.raises(ValidationError, match="raw event or person payload"):
        CustomerDeclaredStatement(
            statement='{"event":"qualified_demo","payload":{"tier":"enterprise"}}'
        )

    statement = CustomerDeclaredStatement(
        statement="A customer-declared qualified demonstration request."
    )
    assert statement.statement == "A customer-declared qualified demonstration request."
