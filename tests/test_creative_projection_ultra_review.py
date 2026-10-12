"""Independent redaction and fidelity checks for creative history projections."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adhelm.creative import (
    CreativeComponent,
    CreativeFamilyRecord,
    CreativeMessage,
    CreativeVersion,
    ServingCreativeManifest,
)
from adhelm.ledger import Ledger
from adhelm.projections import export_verified


def _creative() -> CreativeVersion:
    return CreativeVersion(
        creative_family_id="family:SENTINEL-LOCAL-ID",
        manifest=ServingCreativeManifest(
            provider_format="RESPONSIVE_SEARCH",
            locale="en-US",
            destination_ref="destination:SENTINEL-LOCAL-ID",
            surface_kind="TEXT_ONLY",
            rendition_refs=(),
            components=(
                CreativeComponent(
                    component_id="headline",
                    component_role="HEADLINE",
                    ordinal=0,
                    exact_text="SENTINEL EXACT CREATIVE COPY",
                    normalized_text="sentinel normalized creative copy",
                    language="en",
                    provider_slot="headline_1",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
            ),
        ),
        message=CreativeMessage(
            claim="SENTINEL CREATIVE CLAIM",
            mechanism="SENTINEL CREATIVE MECHANISM",
            audience_frame="SENTINEL CREATIVE AUDIENCE",
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _append_creative(ledger: Ledger, creative: CreativeVersion) -> None:
    ledger.append(
        "creative",
        "CREATIVE_FAMILY",
        CreativeFamilyRecord(
            creative_family_id=creative.creative_family_id,
            # The legacy register command used the local ID as this label.
            # Treat it as marketing/free text at the sharing boundary.
            family_label=creative.creative_family_id,
            created_at=creative.created_at,
            provenance="CUSTOMER_LOCAL",
        ).model_dump(mode="json"),
    )
    ledger.append("creative", "CREATIVE_VERSION", creative.to_ledger_payload())


def _records(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (directory / "records.jsonl").read_text().splitlines()]


def test_creative_free_text_stays_local_until_marketing_text_is_explicit(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "history")
    creative = _creative()
    _append_creative(ledger, creative)

    default_dir = tmp_path / "default"
    explicit_dir = tmp_path / "explicit"
    export_verified(ledger, default_dir, opaque_key=b"k" * 32)
    export_verified(
        ledger,
        explicit_dir,
        opaque_key=b"k" * 32,
        include_marketing_text=True,
    )

    raw_values = (
        creative.creative_family_id,
        "SENTINEL EXACT CREATIVE COPY",
        "sentinel normalized creative copy",
        "SENTINEL CREATIVE CLAIM",
        "SENTINEL CREATIVE MECHANISM",
        "SENTINEL CREATIVE AUDIENCE",
    )
    default_bundle = b"".join(path.read_bytes() for path in default_dir.iterdir())
    explicit_bundle = b"".join(path.read_bytes() for path in explicit_dir.iterdir())
    for value in raw_values:
        assert value.encode() not in default_bundle
        assert value.encode() in explicit_bundle

    default_records = _records(default_dir)
    family = next(
        record for record in default_records if record["record_kind"] == "CREATIVE_FAMILY"
    )
    version = next(
        record for record in default_records if record["record_kind"] == "CREATIVE_VERSION"
    )
    assert family["payload"]["family_label"] is None
    message = version["payload"]["message"]
    assert message == {
        "audience_frame": None,
        "claim": None,
        "mechanism": None,
        "hook": None,
        "objection": None,
        "offer": None,
        "posture": None,
        "specificity": None,
        "tone": [],
    }


def test_creative_version_csv_uses_the_retained_serving_manifest_digest(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "history")
    creative = _creative()
    _append_creative(ledger, creative)

    output = tmp_path / "export"
    manifest = export_verified(ledger, output, opaque_key=b"k" * 32)

    assert manifest["projection_version"] == 2

    rows = list(csv.DictReader((output / "creative-versions.csv").read_text().splitlines()))
    assert rows == [
        {
            "record_id": rows[0]["record_id"],
            "creative_family_id": rows[0]["creative_family_id"],
            "creative_version_id": rows[0]["creative_version_id"],
            "provider_format": "RESPONSIVE_SEARCH",
            "locale": "en-US",
            "serving_manifest_digest": creative.manifest.serving_manifest_digest,
        }
    ]
    assert "content_digest" not in rows[0]
