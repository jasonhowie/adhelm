import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from adhelm.canonical import canonical_bytes, digest
from adhelm.creative import (
    CreativeComponent,
    CreativeFamilyRecord,
    CreativeVersion,
    ServingCreativeManifest,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.projections import escape_formula, export_verified, unescape_formula
from adhelm.reporting import NormalizedReport


def performance_payload(
    creative_version_id: str, source_checkpoint: dict[str, Any]
) -> dict[str, object]:
    payload = {
        "observation_key": "performance:one",
        "observation_revision": 1,
        "source_report_digest": "a" * 64,
        "report_envelope": {
            "schema_version": 1,
            "report_id": "report:one",
            "provider": "google_ads",
            "source_account_ref": "account:one",
            "query_contract_hash": "b" * 64,
            "window_start": "2026-01-01",
            "window_end": "2026-01-01",
            "source_timezone": "UTC",
            "report_timezone": "UTC",
            "currency": "USD",
            "attribution_model": "provider",
            "observed_at": "2026-01-02T00:00:00Z",
            "provider_api_version": "v1",
            "connector_version": "v1",
            "source_ledger_checkpoints": [
                {
                    "stream_id": state["stream_id"],
                    "high_watermark_sequence": state["high_watermark"],
                    "head_digest": state["head_digest"],
                }
                for _, state in sorted(source_checkpoint["streams"].items())
            ],
        },
        "performance_rows": [
            {
                "schema_version": 1,
                "report_id": "report:one",
                "resource_level": "AD",
                "metric_grain": "AD",
                "resource_type": "AD",
                "resource_ref": "ad:one",
                "campaign_ref": "campaign:one",
                "creative_version_id": creative_version_id,
                "spend": {"minor_units": 20, "currency": "USD"},
                "impressions": 3,
                "clicks": 1,
                "data_quality_statuses": ["CREATIVE_NOT_EXPOSED"],
            }
        ],
        "conversion_rows": [],
    }
    report = NormalizedReport.model_validate_json(
        canonical_bytes(
            {
                "envelope": payload["report_envelope"],
                "performance_rows": payload["performance_rows"],
                "conversion_rows": payload["conversion_rows"],
            }
        )
    )
    payload["source_report_digest"] = digest(report)
    return payload


def test_formula_escape_is_reversible_for_apostrophe_and_formula_prefixes() -> None:
    for value in ("=SUM(A1:A2)", "+1", "-1", "@name", "'already", "plain", "\tcell"):
        assert unescape_formula(escape_formula(value)) == value


def _creative_version() -> CreativeVersion:
    return CreativeVersion(
        creative_family_id="family:one",
        manifest=ServingCreativeManifest(
            provider_format="OPENAI_TEXT",
            locale="en",
            destination_ref="destination:landing",
            surface_kind="TEXT_ONLY",
            rendition_refs=(),
            components=(
                CreativeComponent(
                    component_id="headline",
                    component_role="HEADLINE",
                    ordinal=0,
                    exact_text="=Unsafe sheet text",
                    normalized_text="unsafe sheet text",
                    language="en",
                    provider_slot="headline",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
            ),
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def _node_value(row: dict[str, str]) -> Any:
    kind = row["node_type"]
    if kind == "object":
        return {}
    if kind == "array":
        return []
    if kind == "null":
        return None
    if kind == "boolean":
        return row["value"] == "true"
    if kind == "integer":
        return int(row["value"])
    return unescape_formula(row["value"])


def _reconstruct_nodes(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Rebuild a redacted normalized record from its RFC6901 node table."""
    by_pointer = {row["pointer"]: row for row in rows}
    root = _node_value(by_pointer[""])
    assert isinstance(root, dict)
    values: dict[str, Any] = {"": root}
    for pointer in sorted(
        (item for item in by_pointer if item), key=lambda item: (item.count("/"), item)
    ):
        row = by_pointer[pointer]
        parent_pointer, _, escaped = pointer.rpartition("/")
        parent = values[parent_pointer]
        key = escaped.replace("~1", "/").replace("~0", "~")
        value = _node_value(row)
        if isinstance(parent, dict):
            parent[key] = value
        else:
            assert isinstance(parent, list)
            index = int(key)
            assert index == len(parent)
            parent.append(value)
        values[pointer] = value
    return root


def test_export_is_deterministic_redacted_and_has_child_tables(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, profile_ref="private-profile")
    creative = _creative_version()
    ledger.append(
        "changes",
        "CHANGE_EVENT",
        {
            "event_type": "APPLY_STARTED",
            "operation": "CREATE",
            "origin": "LOCAL_CLI",
            "resource_type": "CAMPAIGN",
            "resource_ref": "campaign:one",
            "parent_resource_refs": ["parent:one", "parent:two"],
            "effect_status": "NO_REMOTE_WRITE",
            "idempotency_ref": "attempt:one",
            "policy_id": "policy:one",
            "evidence_refs": ["evidence:one"],
            "policy_reservation": [{"currency": "USD", "minor_units": 20}],
            "field_deltas": [
                {
                    "path": "parent_resource_ref",
                    "before_status": "KNOWN",
                    "before_value": "provider-parent-before",
                    "after_status": "KNOWN",
                    "after_value": "provider-parent-after",
                    "value_classification": "RESOURCE_REF",
                },
                {
                    "path": "provider_id",
                    "before_status": "KNOWN",
                    "before_value": "provider-id-before",
                    "after_status": "KNOWN",
                    "after_value": "provider-id-after",
                    "value_classification": "IDENTIFIER",
                },
            ],
        },
        provider="google_ads",
        account_ref="account:one",
    )
    ledger.append(
        "creative",
        "CREATIVE_FAMILY",
        CreativeFamilyRecord(
            creative_family_id=creative.creative_family_id,
            family_label="Fixture family",
            created_at=creative.created_at,
            provenance="CUSTOMER_LOCAL",
        ).model_dump(mode="json"),
    )
    ledger.append("creative", "CREATIVE_VERSION", creative.to_ledger_payload())
    source_checkpoint = ledger.checkpoint()
    ledger.append(
        "performance",
        "PERFORMANCE_OBSERVATION",
        performance_payload(creative.creative_version_id, source_checkpoint),
        provider="google_ads",
        account_ref="account:one",
    )

    first = export_verified(ledger, tmp_path / "one", opaque_key=b"a" * 32)
    second = export_verified(ledger, tmp_path / "two", opaque_key=b"a" * 32)
    assert first["manifest_digest"] == second["manifest_digest"]
    assert (tmp_path / "one" / "policy-reservations.csv").exists()
    components = (tmp_path / "one" / "creative-components.csv").read_text()
    assert "Unsafe sheet text" not in components
    events = (tmp_path / "one" / "events.csv").read_text()
    assert "account:one" not in events
    assert "opaque:" in events
    assert (tmp_path / "one" / "performance.csv").read_bytes().endswith(b"\r\n")
    bundle = b"".join(path.read_bytes() for path in (tmp_path / "one").iterdir())
    for raw in (
        "private-profile",
        "account:one",
        "campaign:one",
        "parent:one",
        "parent:two",
        "evidence:one",
        "provider-parent-before",
        "provider-parent-after",
        "provider-id-before",
        "provider-id-after",
        creative.creative_version_id,
    ):
        assert raw.encode() not in bundle

    records = [
        json.loads(line) for line in (tmp_path / "one" / "records.jsonl").read_text().splitlines()
    ]
    nodes = list(csv.DictReader((tmp_path / "one" / "record-nodes.csv").read_text().splitlines()))
    for record in records:
        matching = [row for row in nodes if row["record_id"] == record["record_id"]]
        assert _reconstruct_nodes(matching) == record

    with pytest.raises(AdhelmError) as raised:
        export_verified(ledger, tmp_path / "invalid-as-of", as_of=0, opaque_key=b"a" * 32)
    assert raised.value.code == "CHECKPOINT_SELECTION_INVALID"
    assert not (tmp_path / "invalid-as-of").exists()
