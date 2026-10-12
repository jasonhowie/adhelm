"""Deterministic, redacted exports from verified ledger checkpoints."""

from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
from typing import Any

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.privacy import opaque_ref

PROJECTION_VERSION = 3
_FORMULA_PREFIXES = ("'", "=", "+", "-", "@", "\t", "\r")


def escape_formula(value: str) -> str:
    """Apply the reversible CSV text-cell rule from the storage contract."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def unescape_formula(value: str) -> str:
    if len(value) > 1 and value.startswith("'") and value[1:].startswith(_FORMULA_PREFIXES):
        return value[1:]
    return value


def export_verified(
    ledger: Ledger,
    output_dir: Path,
    *,
    as_of: int | None = None,
    include_marketing_text: bool = False,
    opaque_key: bytes | None = None,
) -> dict[str, Any]:
    """Write a deterministic JSONL/CSV bundle for a verified checkpoint.

    The ledger selects the current verified cross-stream vector atomically.
    ``as_of`` is retained only as a compatibility assertion: it must equal
    every selected watermark, so it can never combine independently sampled
    stream heads.
    """
    if opaque_key is None or len(opaque_key) < 32:
        raise AdhelmError(
            "EXPORT_KEY_REQUIRED",
            "A private local pseudonymization key is required before exporting.",
            next_action="Configure the OS-backed export key; no directory was created.",
        )
    checkpoint = ledger.checkpoint()
    if checkpoint.get("status") != "VERIFIED_CHECKPOINT":
        raise AdhelmError(
            "CHECKPOINT_INVALID", "Ledger did not return a verified export checkpoint."
        )
    streams = checkpoint.get("streams")
    records_by_stream = checkpoint.get("records_by_stream")
    if not isinstance(streams, dict) or not isinstance(records_by_stream, dict):
        raise AdhelmError(
            "CHECKPOINT_INVALID", "Ledger did not return a verified export checkpoint."
        )
    selected: dict[str, list[dict[str, Any]]] = {}
    checkpoints: dict[str, dict[str, Any]] = {}
    for stream in ("changes", "performance", "creative"):
        state = streams.get(stream)
        records = records_by_stream.get(stream)
        if not isinstance(state, dict) or not isinstance(records, list):
            raise AdhelmError(
                "CHECKPOINT_INVALID", "Ledger checkpoint has an invalid stream vector."
            )
        high = state.get("high_watermark")
        if not isinstance(high, int) or high < 0:
            raise AdhelmError("CHECKPOINT_INVALID", "Ledger checkpoint high-watermark is invalid.")
        if as_of is not None and high != as_of:
            raise AdhelmError(
                "CHECKPOINT_SELECTION_INVALID",
                "A scalar as-of selection must equal every verified checkpoint watermark.",
            )
        selected[stream] = records
        stream_id = state.get("stream_id")
        if not isinstance(stream_id, str):
            raise AdhelmError("CHECKPOINT_INVALID", "Ledger checkpoint stream identity is invalid.")
        checkpoints[stream] = {
            "stream_id": _opaque(stream_id, opaque_key),
            "high_watermark": high,
            "head_digest": state.get("head_digest"),
        }

    output = Path(output_dir)
    if any(part.is_symlink() for part in (output, *output.absolute().parents)):
        raise AdhelmError(
            "UNSAFE_EXPORT_DESTINATION", "Export paths cannot traverse symbolic links."
        )
    if output.exists():
        raise AdhelmError(
            "EXPORT_DESTINATION_NOT_EMPTY",
            "Export destination exists; choose a new output directory.",
        )
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    key = opaque_key
    all_records = [
        record for stream in ("changes", "performance", "creative") for record in selected[stream]
    ]
    all_records.sort(key=lambda r: (r["stream_id"], r["stream_sequence"]))

    tables = _tables(all_records, key=key, include_marketing_text=include_marketing_text)
    nodes: list[dict[str, Any]] = []
    for record in all_records:
        safe_record = _export_record(record, key, include_marketing_text)
        _scalar_nodes(safe_record["record_id"], "", safe_record, nodes)
    tables["record-nodes.csv"] = (["record_id", "pointer", "node_type", "value"], nodes)
    files: dict[str, dict[str, Any]] = {}
    jsonl_path = output / "records.jsonl"
    jsonl_payload = b"".join(
        canonical_bytes(_export_record(record, key, include_marketing_text)) + b"\n"
        for record in all_records
    )
    _write_bytes(jsonl_path, jsonl_payload)
    files[jsonl_path.name] = {
        "rows": len(all_records),
        "sha256": hashlib.sha256(jsonl_payload).hexdigest(),
    }
    for name, (headers, rows) in sorted(tables.items()):
        data = _csv_bytes(headers, rows)
        _write_bytes(output / name, data)
        files[name] = {"rows": len(rows), "sha256": hashlib.sha256(data).hexdigest()}

    manifest = {
        "schema_version": 1,
        "projection_version": PROJECTION_VERSION,
        "projection_as_of": checkpoints,
        "filters": {
            "as_of": as_of,
            "include_marketing_text": include_marketing_text,
            "checkpoint_digest": checkpoint.get("checkpoint_digest"),
        },
        "formula_escape": {"enabled": True, "prefixes": list(_FORMULA_PREFIXES)},
        "reconstruction": "record-nodes.csv preserves the entire redacted normalized record; "
        "RFC6901 pointers, explicit object/array/null/scalar types; "
        "remove one reversible escape apostrophe from text values.",
        "tables": {
            name: {"headers": headers, "parent_contract": _parent_contract(name)}
            for name, (headers, _) in sorted(tables.items())
        },
        "files": files,
    }
    manifest["manifest_digest"] = digest(manifest)
    manifest_bytes = canonical_bytes(manifest) + b"\n"
    _write_bytes(output / "manifest.json", manifest_bytes)
    return manifest


def _opaque(value: str | None, key: bytes) -> str | None:
    if value is None:
        return None
    return opaque_ref(key, value)


def _opaque_identifier(value: Any, key: bytes) -> str | None:
    """Pseudonymize every identifier representation rather than dropping it.

    Provider APIs sometimes use numeric identifiers.  Canonical integer text is
    enough for HMAC stability and avoids exporting that number in cleartext.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AdhelmError(
            "INVALID_PROJECTION_VALUE", "Reference values must be strings or integers."
        )
    return opaque_ref(key, str(value))


def _is_reference_field(field: str) -> bool:
    return field in {
        "campaign_join_key",
        "source_account_ref",
        "id",
        "identifier",
        "identifiers",
        "destination_profiles",
    } or field.endswith(("_ref", "_refs", "_id", "_ids", "_handle", "_handles"))


def _is_customer_supplied_name_field(field: str) -> bool:
    """Customer-supplied advertising names never leave in cleartext.

    Campaign join maps and explicitly requested Google Ads report labels can
    retain campaign names locally. Both are pseudonymized with the same keyed
    reference as a provider identifier, keeping a name stable across exports
    without revealing it. Marketing-text export preferences do not release
    campaign labels; named report views have their own explicit disclosure.
    """
    return field in {"provider_campaign_name", "google_ads_campaign_name"}


def _is_reference_classification(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    classification = value.upper()
    return classification == "RESOURCE_REF" or "IDENTIFIER" in classification


def _export_record(
    record: dict[str, Any], key: bytes, include_marketing_text: bool
) -> dict[str, Any]:
    result = dict(record)
    for name in ("record_id", "account_ref", "profile_ref", "stream_id", "run_id"):
        result[name] = _opaque_identifier(record[name], key)
    result["payload"] = _redact_payload(record["payload"], key, include_marketing_text)
    return result


def _redact_payload(
    value: Any,
    key: bytes,
    include_marketing_text: bool,
    field: str = "",
    *,
    reference_value: bool = False,
) -> Any:
    if isinstance(value, list):
        return [
            _redact_payload(
                item,
                key,
                include_marketing_text,
                field,
                reference_value=reference_value or _is_reference_field(field),
            )
            for item in value
        ]
    if not isinstance(value, dict):
        if reference_value or _is_reference_field(field) or _is_customer_supplied_name_field(field):
            return _opaque_identifier(value, key)
        if not include_marketing_text and field in {
            "exact_text",
            "normalized_text",
            "claim",
            "mechanism",
            "audience_frame",
            "family_label",
        }:
            return None
        return value
    if value.get("event_type") == "GA4_KEY_EVENT_CANDIDATE_COMPILED":
        from adhelm.ga4_key_event_candidate_views import ga4_key_event_candidate_review

        # Event names, field names, native property/stream IDs, and replayable
        # target/request data remain local even when marketing text is enabled.
        value = ga4_key_event_candidate_review(value)
    if value.get("event_type") == "GA4_KEY_EVENT_CREATE_PLAN":
        from adhelm.ga4_create_plan_views import ga4_key_event_create_plan_public_record

        # Native request paths/bodies and local keyed inventory markers never
        # leave this boundary, including when marketing text is enabled.
        value = ga4_key_event_create_plan_public_record(value)
    if value.get("event_type") == "META_CAMPAIGN_PAUSE_PLAN":
        from adhelm.meta_pause_plan_views import _meta_campaign_pause_plan_export_hmac_input

        # A retained Meta pause action contains local credential/configuration
        # provenance and closed mutation facts.  Export only this narrow
        # historical review preimage, then let the recursive pass below HMAC
        # every retained native reference regardless of marketing-text policy.
        value = _meta_campaign_pause_plan_export_hmac_input(value)
    if value.get("event_type") == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN":
        from adhelm.google_ads_pause_plan_views import (
            _google_ads_campaign_pause_plan_export_hmac_input,
        )

        # Retained Google Ads PAUSE proposals contain local identity and
        # credential-binding facts. Only their closed historical review
        # preimage enters the recursive HMAC pass; marketing-text preferences
        # never release the operational references or reusable local binding.
        value = _google_ads_campaign_pause_plan_export_hmac_input(value)
    if value.get("event_type") == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION" and (
        "execution_observation" in value
    ):
        value = {name: item for name, item in value.items() if name != "execution_observation"}
        value["execution_observation_status"] = (
            "RETAINED_LOCAL_OAUTH_BINDING_NOT_AUTHENTICATED_BY_EXPORT"
        )
    if value.get("event_type") == "APPROVAL_RECEIPT_CLAIMED":
        # Portable artifacts describe local consumption, never carry a usable
        # signed receipt or a receipt-selected trust configuration. This is an
        # allowlist so new local evidence fields cannot leak into exports.
        fields = {
            "schema_version",
            "event_type",
            "evidence_scope",
            "workspace_profile_ref",
            "source_plan_record_digest",
            "configuration_digest",
            "public_key_fingerprint",
            "challenge_digest",
            "challenge_nonce_digest",
            "receipt_digest",
            "receipt_id",
            "receipt_kind",
            "accepted_at",
        }
        value = {name: item for name, item in value.items() if name in fields}
    classification = value.get("value_classification")
    return {
        name: _redact_payload(
            item,
            key,
            include_marketing_text,
            name,
            reference_value=(
                (
                    name in {"before_value", "after_value"}
                    and _is_reference_classification(classification)
                )
                or (name == "dimension_value" and value.get("dimension_type") == "GA4_EVENT")
            ),
        )
        for name, item in value.items()
    }


def _scalar_nodes(record_id: str, pointer: str, value: Any, rows: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        kind = "object"
    elif isinstance(value, list):
        kind = "array"
    elif value is None:
        kind = "null"
    elif isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, int):
        kind = "integer"
    else:
        kind = "string"
    rows.append(
        {
            "record_id": record_id,
            "pointer": pointer,
            "node_type": kind,
            "value": None if kind in {"object", "array", "null"} else value,
        }
    )
    if isinstance(value, dict):
        for key, child in sorted(value.items()):
            escaped = key.replace("~", "~0").replace("/", "~1")
            _scalar_nodes(record_id, pointer + "/" + escaped, child, rows)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scalar_nodes(record_id, pointer + "/" + str(index), child, rows)


def _tables(
    records: list[dict[str, Any]], *, key: bytes, include_marketing_text: bool
) -> dict[str, tuple[list[str], list[dict[str, Any]]]]:
    tables: dict[str, tuple[list[str], list[dict[str, Any]]]] = {
        "events.csv": (
            [
                "record_id",
                "stream",
                "sequence",
                "record_kind",
                "recorded_at",
                "provider",
                "account_ref",
                "policy_id",
                "record_digest",
            ],
            [],
        ),
        "change-fields.csv": (
            [
                "record_id",
                "ordinal",
                "path",
                "before_status",
                "before_value",
                "after_status",
                "after_value",
                "value_classification",
            ],
            [],
        ),
        "policy-reservations.csv": (
            ["record_id", "ordinal", "policy_id", "currency", "minor_units"],
            [],
        ),
        "resource-snapshots.csv": (
            [
                "record_id",
                "snapshot_id",
                "resource_type",
                "resource_ref",
                "remote_fingerprint",
                "normalized_state_digest",
                "effective_status",
            ],
            [],
        ),
        "performance.csv": (
            [
                "record_id",
                "ordinal",
                "report_id",
                "resource_level",
                "resource_ref",
                "campaign_ref",
                "spend_minor",
                "impressions",
                "clicks",
                "join_status",
            ],
            [],
        ),
        "conversions.csv": (
            [
                "record_id",
                "ordinal",
                "report_id",
                "resource_ref",
                "conversion_source",
                "conversion_action_ref",
                "conversion_count",
                "conversion_count_decimal",
                "conversion_kind",
                "campaign_ref",
                "date",
                "conversion_value_minor",
                "currency",
                "join_status",
            ],
            [],
        ),
        "creative-versions.csv": (
            [
                "record_id",
                "creative_family_id",
                "creative_version_id",
                "provider_format",
                "locale",
                "serving_manifest_digest",
            ],
            [],
        ),
        "creative-assets.csv": (
            ["record_id", "asset_version_id", "content_digest", "availability_status"],
            [],
        ),
        "creative-renditions.csv": (
            [
                "record_id",
                "rendition_id",
                "source_asset_version_ref",
                "content_digest",
                "provider_format",
                "locale",
            ],
            [],
        ),
        "creative-components.csv": (
            [
                "record_id",
                "ordinal",
                "component_id",
                "component_role",
                "exact_text",
                "normalized_text",
                "normalized_cta_intent",
            ],
            [],
        ),
        "creative-colors.csv": (
            ["record_id", "ordinal", "hex", "proportion_basis_points", "role"],
            [],
        ),
        "creative-fingerprints.csv": (
            [
                "record_id",
                "fingerprint_id",
                "creative_version_id",
                "analyzer_name",
                "analyzer_version",
            ],
            [],
        ),
        "creative-experiments.csv": (
            ["record_id", "experiment_id", "hypothesis", "primary_metric"],
            [],
        ),
        "creative-deployments.csv": (
            [
                "record_id",
                "deployment_id",
                "creative_family_id",
                "creative_version_id",
                "effective_from",
                "effective_to",
                "association_status",
            ],
            [],
        ),
        "creative-performance.csv": (
            [
                "record_id",
                "ordinal",
                "creative_version_id",
                "resource_ref",
                "spend_minor",
                "impressions",
                "clicks",
            ],
            [],
        ),
    }
    for record in records:
        exported = _export_record(record, key, include_marketing_text)
        payload = exported["payload"]
        if not isinstance(payload, dict):
            raise AdhelmError("INVALID_PROJECTION_VALUE", "Ledger payload must be an object.")
        record_id = exported["record_id"]
        table = tables["events.csv"][1]
        table.append(
            {
                "record_id": record_id,
                "stream": exported["stream_id"],
                "sequence": exported["stream_sequence"],
                "record_kind": exported["record_kind"],
                "recorded_at": exported["recorded_at"],
                "provider": exported["provider"],
                "account_ref": exported["account_ref"],
                "policy_id": payload.get("policy_id"),
                "record_digest": exported["record_digest"],
            }
        )
        if exported["record_kind"] == "CHANGE_EVENT":
            for ordinal, delta in enumerate(payload.get("field_deltas", []), start=1):
                tables["change-fields.csv"][1].append(
                    {"record_id": record_id, "ordinal": ordinal, **delta}
                )
            for ordinal, reservation in enumerate(payload.get("policy_reservation", []), start=1):
                tables["policy-reservations.csv"][1].append(
                    {
                        "record_id": record_id,
                        "ordinal": ordinal,
                        "policy_id": payload.get("policy_id"),
                        "currency": reservation["currency"],
                        "minor_units": reservation["minor_units"],
                    }
                )
        elif exported["record_kind"] == "RESOURCE_SNAPSHOT":
            tables["resource-snapshots.csv"][1].append(
                {
                    "record_id": record_id,
                    "snapshot_id": payload["snapshot_id"],
                    "resource_type": payload["resource_type"],
                    "resource_ref": payload["resource_ref"],
                    "remote_fingerprint": payload["remote_fingerprint"],
                    "normalized_state_digest": payload["normalized_state_digest"],
                    "effective_status": payload["effective_status"],
                }
            )
        elif exported["record_kind"] == "PERFORMANCE_OBSERVATION":
            report_id = (payload.get("report_envelope") or {}).get("report_id")
            for ordinal, row in enumerate(payload.get("performance_rows", []), start=1):
                tables["performance.csv"][1].append(
                    {
                        "record_id": record_id,
                        "ordinal": ordinal,
                        "report_id": report_id,
                        "resource_level": row.get("resource_level"),
                        "resource_ref": row.get("resource_ref"),
                        "campaign_ref": row.get("campaign_ref"),
                        "spend_minor": _minor_units(row.get("spend")),
                        "impressions": row.get("impressions"),
                        "clicks": row.get("clicks"),
                        "join_status": row.get("join_status"),
                    }
                )
                if row.get("creative_version_id"):
                    tables["creative-performance.csv"][1].append(
                        {
                            "record_id": record_id,
                            "ordinal": ordinal,
                            "creative_version_id": row.get("creative_version_id"),
                            "resource_ref": row.get("resource_ref"),
                            "spend_minor": _minor_units(row.get("spend")),
                            "impressions": row.get("impressions"),
                            "clicks": row.get("clicks"),
                        }
                    )
            for ordinal, row in enumerate(payload.get("conversion_rows", []), start=1):
                tables["conversions.csv"][1].append(
                    {
                        "record_id": record_id,
                        "ordinal": ordinal,
                        "report_id": report_id,
                        "resource_ref": row.get("resource_ref"),
                        "conversion_source": row.get("conversion_source"),
                        "conversion_action_ref": row.get("conversion_action_ref"),
                        "conversion_count": row.get("conversion_count"),
                        "conversion_count_decimal": row.get("conversion_count_decimal"),
                        "conversion_kind": row.get("conversion_kind"),
                        "campaign_ref": row.get("campaign_ref"),
                        "date": row.get("provider_extensions", {}).get("google_ads_date"),
                        "conversion_value_minor": _minor_units(row.get("conversion_value")),
                        "currency": _currency(row.get("conversion_value")),
                        "join_status": row.get("join_status"),
                    }
                )
        elif record["stream_id"].endswith(":creative"):
            _creative_rows(
                tables,
                record_id,
                exported["record_kind"],
                payload,
                include_marketing_text,
            )
    for _, rows in tables.values():
        rows.sort(
            key=lambda row: tuple("" if value is None else str(value) for value in row.values())
        )
    return tables


def _creative_rows(
    tables: dict[str, tuple[list[str], list[dict[str, Any]]]],
    record_id: str,
    kind: str,
    payload: dict[str, Any],
    include_marketing_text: bool,
) -> None:
    if kind == "CREATIVE_VERSION":
        tables["creative-versions.csv"][1].append(
            {
                "record_id": record_id,
                "creative_family_id": payload.get("creative_family_id"),
                "creative_version_id": payload.get("creative_version_id"),
                "provider_format": payload.get("provider_format"),
                "locale": payload.get("locale"),
                "serving_manifest_digest": payload.get("serving_manifest_digest"),
            }
        )
        for ordinal, component in enumerate(payload.get("components", []), start=1):
            tables["creative-components.csv"][1].append(
                {
                    "record_id": record_id,
                    "ordinal": ordinal,
                    "component_id": component.get("component_id"),
                    "component_role": component.get("component_role"),
                    "exact_text": component.get("exact_text") if include_marketing_text else None,
                    "normalized_text": component.get("normalized_text")
                    if include_marketing_text
                    else None,
                    "normalized_cta_intent": component.get("normalized_cta_intent"),
                }
            )
    elif kind == "CREATIVE_ASSET_VERSION":
        tables["creative-assets.csv"][1].append(
            {
                "record_id": record_id,
                "asset_version_id": payload.get("asset_version_id"),
                "content_digest": payload.get("content_digest"),
                "availability_status": payload.get("availability_status"),
            }
        )
    elif kind == "CREATIVE_RENDITION":
        tables["creative-renditions.csv"][1].append(
            {
                "record_id": record_id,
                "rendition_id": payload.get("rendition_id"),
                "source_asset_version_ref": payload.get("source_asset_version_ref"),
                "content_digest": payload.get("content_digest"),
                "provider_format": payload.get("provider_format"),
                "locale": payload.get("locale"),
            }
        )
    elif kind == "CREATIVE_FINGERPRINT":
        tables["creative-fingerprints.csv"][1].append(
            {
                "record_id": record_id,
                "fingerprint_id": payload.get("fingerprint_id"),
                "creative_version_id": payload.get("creative_version_id"),
                "analyzer_name": payload.get("analyzer_name"),
                "analyzer_version": payload.get("analyzer_version"),
            }
        )
        for ordinal, color in enumerate(payload.get("palette", []), start=1):
            tables["creative-colors.csv"][1].append(
                {
                    "record_id": record_id,
                    "ordinal": ordinal,
                    "hex": color.get("hex"),
                    "proportion_basis_points": color.get("proportion_basis_points"),
                    "role": color.get("role"),
                }
            )
    elif kind == "CREATIVE_DEPLOYMENT":
        tables["creative-deployments.csv"][1].append(
            {
                "record_id": record_id,
                "deployment_id": payload.get("deployment_id"),
                "creative_family_id": payload.get("creative_family_id"),
                "creative_version_id": payload.get("creative_version_id"),
                "effective_from": payload.get("effective_from"),
                "effective_to": payload.get("effective_to"),
                "association_status": payload.get("association_status"),
            }
        )


def _csv_bytes(headers: list[str], rows: list[dict[str, Any]]) -> bytes:
    from io import StringIO

    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\r\n", extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(row.get(key)) for key in headers})
    return output.getvalue().encode("utf-8")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        raise AdhelmError("INVALID_PROJECTION_VALUE", "Nested values require a child CSV table.")
    return escape_formula(str(value))


def _minor_units(value: Any) -> int | None:
    return value.get("minor_units") if isinstance(value, dict) else None


def _currency(value: Any) -> str | None:
    return value.get("currency") if isinstance(value, dict) else None


def _write_bytes(path: Path, data: bytes) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise AdhelmError(
            "UNSAFE_EXPORT_DESTINATION", "Export destination changed to a symbolic link."
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _parent_contract(name: str) -> str:
    if name in {
        "change-fields.csv",
        "performance.csv",
        "conversions.csv",
        "creative-components.csv",
        "creative-colors.csv",
        "creative-performance.csv",
    }:
        return (
            "record_id is the parent foreign key; ordinal preserves source order where applicable."
        )
    return "record_id identifies the canonical ledger record that produced this row."
