"""A composite summary cannot replace participant-specific durable proof."""

from pathlib import Path

import pytest

from adhelm.ledger import Ledger


@pytest.mark.parametrize("event", ["RUN_VERIFIED", "PROVIDER_RECONCILED"])
@pytest.mark.parametrize("mismatch", ["missing_proof", "other_run", "other_resource"])
def test_composite_closure_requires_matching_durable_proof(
    tmp_path: Path, event: str, mismatch: str
) -> None:
    ledger = Ledger(tmp_path)
    participant = {
        "provider": "google_ads",
        "account_ref": "account:one",
        "resource_ref": "subplan:one",
        "subplan_digest": "b" * 64,
    }
    barrier = {
        "plan_digest": "a" * 64,
        "approval_ref": "approval:one",
        "authorization_digest": "c" * 64,
        "participants": [participant],
    }
    evidence = ["evidence:sha256:" + "d" * 64]
    ledger.append(
        "changes", "COMPOSITE_RUN", {**barrier, "event_type": "RUN_STARTED"}, run_id="run:one"
    )
    proof = {
        "event_type": "VERIFICATION_RESULT" if event == "RUN_VERIFIED" else "RECONCILIATION_RESULT",
        "operation": "CREATE",
        "origin": "LOCAL_CLI",
        "resource_type": "CAMPAIGN",
        "resource_ref": participant["resource_ref"],
        "effect_status": "APPLIED_VERIFIED",
        "plan_digest": barrier["plan_digest"],
        "approval_ref": barrier["approval_ref"],
        "subplan_digest": participant["subplan_digest"],
        "evidence_refs": evidence,
    }
    if mismatch != "missing_proof":
        ledger.append(
            "changes",
            "CHANGE_EVENT",
            {**proof, "resource_ref": "other:resource"} if mismatch == "other_resource" else proof,
            provider="google_ads",
            account_ref="account:one",
            run_id="other:run" if mismatch == "other_run" else "run:one",
        )
    closing = {**barrier, "event_type": event}
    if event == "PROVIDER_RECONCILED":
        closing.update(
            reconciled_subplan_digest="b" * 64,
            effect_status="APPLIED_VERIFIED",
            evidence_refs=evidence,
        )
    ledger.append("changes", "COMPOSITE_RUN", closing, run_id="run:one")
    assert len(ledger.unresolved_runs()) == 1
    ledger.append(
        "changes",
        "CHANGE_EVENT",
        proof,
        provider="google_ads",
        account_ref="account:one",
        run_id="run:one",
    )
    ledger.append("changes", "COMPOSITE_RUN", closing, run_id="run:one")
    assert ledger.unresolved_runs() == []
