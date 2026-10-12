"""Select retained GA4 Admin evidence for a provider audit from one verified checkpoint.

`adhelm.saved_audit_evidence` selects advertising inventory from `RESOURCE_SNAPSHOT`
records.  GA4 has no such hierarchy: its fixed Admin read is retained as one
`MEASUREMENT_OBSERVATION` for provider `ga4_admin`.  This module selects the
observation that describes the same property as the audited GA4 report.

Everything comes from a single verified ledger checkpoint.  This module makes no
provider request, reads no credential, and appends no record.  The Admin
observation is optional evidence: a property with no retained observation is
audited from its report alone, with the omission named in the provenance and the
configuration dimension left `NOT_READ`.
"""

from __future__ import annotations

import re
from typing import Any, Final

from adhelm.audits import ProviderAuditEvidence, RetainedGa4AdminObservation
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_records import Ga4MeasurementSnapshot, MeasurementObservation
from adhelm.strict_json import validate_json_value

_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
# The report names its property `ga4.property.<id>` and the Admin observation
# names it `ga4_admin.property.<id>`; both carry the same numeric identifier.
_PROPERTY_REF: Final = re.compile(r"ga4(?:_admin)?\.property\.([0-9]{1,20})")


def load_ga4_audit_evidence(
    ledger: Ledger,
    *,
    account_ref: str | None,
    inventory_record_digest: str | None = None,
) -> tuple[ProviderAuditEvidence, dict[str, Any]]:
    """Select the retained GA4 Admin observation for one property, if any.

    Without `inventory_record_digest` the most recent observation for the
    property is selected.  A digest that names no observation of this property
    at the verified checkpoint is refused rather than replaced by another one.
    """
    if inventory_record_digest is not None and _DIGEST.fullmatch(inventory_record_digest) is None:
        raise AdhelmError(
            "INVALID_AUDIT_EVIDENCE_SELECTION",
            "A retained measurement record digest is required.",
            exit_code=2,
        )
    matched = _PROPERTY_REF.fullmatch(account_ref) if account_ref is not None else None
    if matched is None:
        raise AdhelmError(
            "INVALID_AUDIT_EVIDENCE_SELECTION",
            "GA4 audit evidence is selected by a GA4 property reference.",
            exit_code=2,
            next_action="Select the property the audited report describes.",
        )
    retained_account = f"ga4_admin.property.{matched.group(1)}"
    # Recorded on every returned evidence, retained or not, so that the audit can
    # refuse a property that is not the report's own even when nothing is retained
    # for it.  It is evidence input only and is never echoed into the provenance.
    requested_property = f"ga4.property.{matched.group(1)}"
    checkpoint = ledger.checkpoint()
    candidates = [
        record
        for record in checkpoint["records_by_stream"]["changes"]
        if record["record_kind"] == "MEASUREMENT_OBSERVATION"
        and record["provider"] == "ga4_admin"
        and record["account_ref"] == retained_account
    ]
    provenance: dict[str, Any] = {
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "provider": "ga4",
        "retained_admin_observation_count": len(candidates),
    }
    if inventory_record_digest is not None:
        candidates = [
            item for item in candidates if item["record_digest"] == inventory_record_digest
        ]
        if not candidates:
            raise AdhelmError(
                "INVENTORY_SNAPSHOT_NOT_RETAINED",
                "The selected GA4 Admin observation is unavailable for this property at the "
                "verified checkpoint.",
            )
    if not candidates:
        return (
            ProviderAuditEvidence(ga4_requested_property_ref=requested_property),
            {**provenance, "admin_inventory_status": "NOT_RETAINED"},
        )
    record = max(candidates, key=lambda item: item["stream_sequence"])
    observation = validate_json_value(MeasurementObservation, record["payload"])
    snapshot = observation.snapshot
    # Candidates were filtered on ledger provider `ga4_admin`, and
    # `retain_measurement` takes that provider from `snapshot.provider`, so a
    # retained `ga4_admin` observation always holds a GA4 snapshot.  The
    # assertion narrows the type for the checker; it documents an invariant of
    # the ledger and is not a runtime refusal an input could reach.
    assert isinstance(snapshot, Ga4MeasurementSnapshot)
    evidence = ProviderAuditEvidence(
        ga4_admin=RetainedGa4AdminObservation(
            record_digest=record["record_digest"],
            observed_at=observation.read_finished_at,
            inventory=snapshot.inventory,
        ),
        ga4_requested_property_ref=requested_property,
    )
    return evidence, {
        **provenance,
        "admin_inventory_status": "SELECTED",
        "selected_admin_observation_record_digest": record["record_digest"],
        "selected_admin_observation_snapshot_digest": observation.snapshot_digest,
        "selected_admin_observation_read_finished_at": observation.read_finished_at.isoformat(),
    }
