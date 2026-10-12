"""Readback facts survive export without becoming an inventory or settlement claim."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_meta_pause_kernel import (
    _CrashAfterMetaStart,
    _CrashAfterStartMetaAdapter,
    _signed_material,
)

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.inventory import _snapshot
from adhelm.ledger import Ledger
from adhelm.meta_pause_readback_records import MetaCampaignPauseReadbackObservation
from adhelm.projections import _export_record, _tables
from adhelm.providers.meta_pause_contract import MetaCampaignPausePlannedAction

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_KIND = "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION"
_T0 = datetime(2026, 9, 6, 12, tzinfo=UTC)


def _payload() -> dict[str, Any]:
    return MetaCampaignPauseReadbackObservation(
        action_id="a" * 64,
        plan_digest="b" * 64,
        subplan_digest="c" * 64,
        attempt_ref="attempt:synthetic",
        account_ref="meta_ads.account.act_123456",
        campaign_ref="meta_ads.campaign.987654",
        observation_interval_start=_T0,
        observation_interval_end=_T0 + timedelta(seconds=1),
        effective_status="WITH_ISSUES",
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"raw_response": "provider-marker"},
        {"configured_status": "ACTIVE"},
        {"effective_status": "SPEND_STOPPED"},
        {"provider_api_version": "v25.0"},
        {"acknowledgement_semantics": "PROVIDER_CONFIRMED"},
        {"campaign_ref": "meta_ads.campaign.0"},
        {"plan_digest": "x" * 64},
        {"observation_interval_end": "2026-09-06T11:59:59Z"},
        {"observation_interval_end": "2026-09-06T12:05:01Z"},
        {"observation_interval_end": "2026-09-06T12:00:01"},
    ],
)
def test_readback_model_rejects_unbounded_or_untrue_shape(change: dict[str, Any]) -> None:
    import json

    with pytest.raises(ValidationError):
        MetaCampaignPauseReadbackObservation.model_validate_json(json.dumps(_payload() | change))


def _started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Ledger, MetaCampaignPausePlannedAction, dict[str, Any]]:
    kernel, ledger, action, grant = _signed_material(tmp_path, monkeypatch)
    adapter = _CrashAfterStartMetaAdapter(action, ledger=ledger, state_after_crash=True)
    with pytest.raises(_CrashAfterMetaStart):
        kernel.execute(action.plan, grant, {"meta_ads": adapter})
    start = next(
        row
        for row in ledger.records("changes")
        if row["payload"].get("event_type") == "APPLY_STARTED"
    )
    payload = _payload() | {
        "action_id": action.action_id,
        "plan_digest": action.plan.plan_digest,
        "subplan_digest": action.plan.subplans[0].subplan_digest,
        "attempt_ref": start["payload"]["idempotency_ref"],
        "account_ref": action.plan.subplans[0].account.account_ref,
        "campaign_ref": action.observation_binding.campaign.campaign_ref,
    }
    return ledger, action, payload


def _append(
    ledger: Ledger, action: MetaCampaignPausePlannedAction, payload: dict[str, Any]
) -> dict[str, Any]:
    return ledger.append(
        "changes",
        _KIND,
        payload,
        provider="meta_ads",
        account_ref=action.plan.subplans[0].account.account_ref,
        run_id=action.plan.plan_id,
        observed_at=payload["observation_interval_end"],
    )


def test_readback_requires_retained_action_and_durable_start(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path).append("changes", _KIND, _payload(), provider="meta_ads")
    assert error.value.code == "INVALID_META_PAUSE_READBACK_HISTORY"


@pytest.mark.parametrize(
    "field", ["attempt_ref", "plan_digest", "subplan_digest", "account_ref", "campaign_ref"]
)
def test_readback_cross_record_binding_rejects_other_attempt_or_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    ledger, action, payload = _started(tmp_path, monkeypatch)
    changed = (
        "d" * 64
        if field.endswith("digest")
        else "meta_ads.campaign.12345"
        if field == "campaign_ref"
        else "other-ref"
    )
    before = ledger.records("changes")
    with pytest.raises(AdhelmError) as error:
        _append(ledger, action, payload | {field: changed})
    assert error.value.code == "INVALID_META_PAUSE_READBACK_HISTORY"
    assert ledger.records("changes") == before


def test_readback_retains_status_and_exports_without_changing_inventory_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, action, payload = _started(tmp_path, monkeypatch)
    snapshot = _snapshot(
        provider_api_version="v26.0",
        resource_type="CAMPAIGN",
        resource_ref=payload["campaign_ref"],
        parents=[payload["account_ref"]],
        state={"status": "WITH_ISSUES", "currency": "USD", "daily_budget_minor": 500},
        status="WITH_ISSUES",
    )
    inventory = ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        snapshot,
        provider="meta_ads",
        account_ref=payload["account_ref"],
    )
    observation = _append(ledger, action, payload)
    assert ledger.records("changes")[-1] == observation
    assert observation["payload"]["effective_status"] == "WITH_ISSUES"
    diff = ledger.snapshot_diff(snapshot, provider="meta_ads", account_ref=payload["account_ref"])
    assert diff == {"changed": False, "previous_record_id": inventory["record_id"]}
    assert ledger.unresolved_starts(), "A factual readback never settles an uncertain write"
    exported = _export_record(observation, b"x" * 32, False)
    assert exported["payload"]["account_ref"] != payload["account_ref"]
    assert exported["payload"]["campaign_ref"] != payload["campaign_ref"]
    assert exported["payload"]["effective_status"] == "WITH_ISSUES"
    tables = _tables([observation], key=b"x" * 32, include_marketing_text=False)
    assert not tables["resource-snapshots.csv"][1]
    assert len(tables["events.csv"][1]) == 1
    assert digest(observation["payload"]) == digest(payload)
    with pytest.raises(AdhelmError) as duplicate:
        _append(ledger, action, payload)
    assert duplicate.value.code == "INVALID_META_PAUSE_READBACK_HISTORY"
