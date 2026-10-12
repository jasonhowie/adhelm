from __future__ import annotations

from copy import deepcopy

import pytest

from adhelm.artifact_detail import present_document
from adhelm.audit import AuditResult
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.reporting import NormalizedReport


def _restore_compact(value: object) -> object:
    if isinstance(value, list):
        return [_restore_compact(child) for child in value]
    if not isinstance(value, dict):
        return value
    marker = value.get("$artifact_compact_rows")
    if isinstance(marker, dict) and marker.get("version") == 1:
        assert marker["inheritance"] == (
            "Each per_row_values entry inherits shared_fields; its own keys take precedence."
        )
        shared = _restore_compact(value["shared_fields"])
        assert isinstance(shared, dict)
        rows = value["per_row_values"]
        assert isinstance(rows, list)
        restored_rows: list[object] = []
        for row in rows:
            restored = _restore_compact(row)
            assert isinstance(restored, dict)
            restored_rows.append({**shared, **restored})
        return restored_rows
    return {key: _restore_compact(child) for key, child in value.items()}


def _redacted_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact_kind": "normalized_report",
        "source_evidence": {"source_record_digest": "a" * 64, "checkpoint_digest": "b" * 64},
        "content": {
            "report": {
                "performance_rows": [
                    {
                        "resource_ref": "hmac:opaque-a",
                        "resource_level": "CAMPAIGN",
                        "metric_grain": "CAMPAIGN",
                        "spend": {"minor_units": 123, "currency": "USD"},
                        "data_quality_statuses": ["PARTIAL_PAGE_FAILURE"],
                        "unavailable_reason": None,
                        "evidence_ref": "a" * 64,
                    },
                    {
                        "resource_ref": "hmac:opaque-b",
                        "resource_level": "CAMPAIGN",
                        "metric_grain": "CAMPAIGN",
                        "spend": {"minor_units": 456, "currency": "USD"},
                        "data_quality_statuses": ["PARTIAL_PAGE_FAILURE"],
                        "unavailable_reason": None,
                        "evidence_ref": "a" * 64,
                    },
                    {
                        "resource_ref": "hmac:opaque-c",
                        "resource_level": "CAMPAIGN",
                        "metric_grain": "CAMPAIGN",
                        "spend": {"minor_units": 789, "currency": "USD"},
                        "data_quality_statuses": ["PARTIAL_PAGE_FAILURE"],
                        "unavailable_reason": None,
                        "evidence_ref": "a" * 64,
                    },
                ],
                "primitive_statuses": ["PARTIAL", "UNAVAILABLE"],
            }
        },
    }


def test_compact_is_smaller_lossless_and_preserves_order_and_nulls() -> None:
    document = _redacted_document()
    original = deepcopy(document)
    compact = present_document(document, "compact")
    content = compact["content"]
    assert isinstance(content, dict)
    report = content["report"]
    assert isinstance(report, dict)
    rows = report["performance_rows"]
    assert isinstance(rows, dict)
    marker = rows["$artifact_compact_rows"]
    assert isinstance(marker, dict)
    assert marker["version"] == 1
    shared_fields = rows["shared_fields"]
    assert isinstance(shared_fields, dict)
    assert shared_fields["unavailable_reason"] is None
    assert report["primitive_statuses"] == ["PARTIAL", "UNAVAILABLE"]
    assert _restore_compact(compact) == original
    assert document == original
    assert len(canonical_bytes(compact)) < len(canonical_bytes(original))


def test_compact_preserves_heterogeneous_rows_and_does_not_force_larger_wrappers() -> None:
    document = {
        "rows": [
            {"resource_ref": "hmac:a", "metric_grain": "CAMPAIGN", "spend": None},
            {"resource_ref": "hmac:b", "metric_grain": "AD", "spend": None},
        ],
        "heterogeneous": [{"left": 1}, {"right": 2}],
    }
    compact = present_document(document, "compact")
    assert _restore_compact(compact) == document
    rows = compact["rows"]
    assert rows == document["rows"]
    assert compact["heterogeneous"] == document["heterogeneous"]


def test_compact_keeps_boolean_and_integer_fields_distinct_when_factoring() -> None:
    """Python equality must not turn a JSON number into a JSON boolean."""
    document = {
        "rows": [
            {"common": "x" * 2_000, "value": True, "row": "first"},
            {"common": "x" * 2_000, "value": 1, "row": "second"},
        ]
    }

    compact = present_document(document, "compact")
    rows = compact["rows"]
    assert isinstance(rows, dict)
    assert "$artifact_compact_rows" in rows
    shared = rows["shared_fields"]
    assert isinstance(shared, dict)
    assert "value" not in shared
    per_row_values = rows["per_row_values"]
    assert isinstance(per_row_values, list)
    assert per_row_values[0]["value"] is True
    assert per_row_values[1]["value"] == 1
    assert type(per_row_values[1]["value"]) is int
    assert _restore_compact(compact) == document


def test_standard_and_detailed_do_not_add_source_values_to_redacted_document() -> None:
    document = _redacted_document()
    standard = present_document(document, "standard")
    source_schema = deepcopy(NormalizedReport.model_json_schema())
    properties = source_schema.setdefault("properties", {})
    assert isinstance(properties, dict)
    properties["private_source_value"] = {
        "type": "string",
        "default": "unredacted-customer-value",
        "examples": ["unredacted-customer-value"],
    }
    detailed = present_document(document, "detailed", source_schema)
    assert standard == document
    assert standard is not document
    assert detailed["$artifact_detail"] == {"version": 1, "detail": "detailed"}
    assert detailed["document"] == document
    field_guide = detailed["field_guide"]
    assert isinstance(field_guide, dict)
    assert field_guide["kind"] == "static_schema_field_guide"
    assert "private_source_value" in str(field_guide)
    assert "unredacted-customer-value" not in str(field_guide)
    assert canonical_bytes(detailed) != canonical_bytes(standard)
    audit_detailed = present_document(document, "detailed", AuditResult.model_json_schema())
    audit_guide = audit_detailed["field_guide"]
    assert isinstance(audit_guide, dict)
    assert "$.findings" in str(audit_guide["fields"])


def test_detail_and_json_document_boundaries_are_explicit() -> None:
    with pytest.raises(AdhelmError, match="detail is unsupported"):
        present_document({"value": 1}, "verbose")  # type: ignore[arg-type]
    with pytest.raises(AdhelmError, match="source schema"):
        present_document({"value": 1}, "detailed")
    with pytest.raises(AdhelmError, match="JSON-compatible"):
        present_document({"value": {"not": {1, 2}}}, "standard")
