"""Exact local Meta PAUSE retention; no provider, credential or approval work."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.meta_pause_plan_records import (
    meta_campaign_pause_plan_record,
    revalidate_meta_campaign_pause_plan_record,
)
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPauseAccountBinding,
    MetaCampaignPauseCampaignBaseline,
    MetaCampaignPauseExecutionBinding,
    MetaCampaignPauseObservationBinding,
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
)
from adhelm.saved_meta_pause_plans import (
    load_meta_campaign_pause_plan,
    meta_campaign_pause_plan_catalog,
    retain_meta_campaign_pause_plan,
)

_START = datetime(2026, 9, 6, 12, tzinfo=UTC)


def _action(*, minutes: int = 10) -> MetaCampaignPausePlannedAction:
    return build_meta_campaign_pause_plan(
        workspace_profile_ref="workspace-main",
        account_binding=MetaCampaignPauseAccountBinding(
            account_ref="meta_ads.account.act_123456",
            account_client_ref="act_123456",
            timezone="UTC",
            currency="USD",
            account_status="1",
        ),
        execution_binding=MetaCampaignPauseExecutionBinding(
            profile_ref="meta-edit",
            client_application_ref="1234567890",
            principal_ref="meta_token_principal_v1." + "a" * 64,
            credential_binding_ref="credential-binding:sha256:" + "b" * 64,
            configuration_digest="c" * 64,
            profile_configuration_digest="d" * 64,
            inspected_at=_START,
            expires_at=_START + timedelta(hours=1),
            data_access_expires_at=_START + timedelta(hours=2),
        ),
        observation_binding=MetaCampaignPauseObservationBinding(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref="meta_ads.campaign.987654",
                campaign_client_ref="987654",
                account_client_ref="123456",
                configured_status="ACTIVE",
                effective_status="WITH_ISSUES",
            ),
            read_started_at=_START + timedelta(seconds=1),
            read_finished_at=_START + timedelta(seconds=2),
        ),
        expires_at=_START + timedelta(minutes=minutes),
    )


def _ledger(root: Path) -> Ledger:
    return Ledger(root, profile_ref="workspace-main")


def test_retained_action_round_trips_from_verified_history_without_current_claims(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    action = _action()
    stored = retain_meta_campaign_pause_plan(ledger, action)
    assert stored["record_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    assert stored["provider"] == "meta_ads"
    assert stored["account_ref"] == action.account_binding.account_ref
    assert stored["source"] == "LOCAL_CLI"
    assert stored["observed_at"] is None
    assert stored["occurred_at"] is None
    assert stored["run_id"] is None
    reloaded, provenance = load_meta_campaign_pause_plan(ledger, stored["record_digest"])
    assert reloaded == action
    assert provenance["plan_digest"] == action.plan.plan_digest
    assert provenance["action_digest"] == digest(action)
    assert provenance["execution_authority"] is False
    assert provenance["requires_current_kernel_checks"] is True
    assert provenance["historical_evidence_status"] == "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
    assert ledger.verify()["status"] == "VERIFIED"
    assert not any(
        row["payload"].get("event_type") in {"APPLY_STARTED", "AUTHORIZATION_GRANTED"}
        for row in ledger.records("changes")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 1.0),
        ("schema_version", True),
        ("provider", "openai_ads"),
        ("execution_authority", 0),
        ("execution_authority", True),
        ("requires_current_kernel_checks", 1),
        ("requires_current_kernel_checks", False),
        ("plan_digest", "e" * 64),
        ("action_digest", "f" * 64),
        ("maximum_incremental_spend", {"minor_units": 0.0, "currency": "USD"}),
        ("maximum_incremental_spend", {"minor_units": 1, "currency": "USD"}),
    ],
)
def test_record_rejects_mismatched_or_coercible_controls(field: str, value: object) -> None:
    data = meta_campaign_pause_plan_record(_action()).model_dump(mode="json")
    data[field] = value
    with pytest.raises(AdhelmError) as error:
        revalidate_meta_campaign_pause_plan_record(data)
    assert error.value.code == "INVALID_META_PAUSE_PLAN_RECORD"


def test_record_requires_full_closed_shape_and_exact_action_reconstruction() -> None:
    original = meta_campaign_pause_plan_record(_action()).model_dump(mode="json")
    for missing in ("execution_authority", "action_digest", "historical_evidence_status"):
        data = {key: value for key, value in original.items() if key != missing}
        with pytest.raises(AdhelmError, match="complete closed"):
            revalidate_meta_campaign_pause_plan_record(data)
    injected = {**original, "grant": {"approved": True}}
    with pytest.raises(AdhelmError):
        revalidate_meta_campaign_pause_plan_record(injected)
    forged = json.loads(json.dumps(original))
    forged["action"]["plan"]["subplans"][0]["egress"][0]["host"] = "unapproved.example"
    forged["action_digest"] = digest(forged["action"])
    forged["plan_digest"] = digest(forged["action"]["plan"])
    with pytest.raises(AdhelmError):
        revalidate_meta_campaign_pause_plan_record(forged)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "ga4_admin"),
        ("account_ref", "meta_ads.account.act_999999"),
        ("source", "PROVIDER_UI"),
        ("run_id", "unexecuted-run"),
        ("observed_at", "2026-09-06T12:00:00Z"),
        ("occurred_at", "2026-09-06T12:00:00Z"),
    ],
)
def test_wrong_envelope_refused_before_any_ledger_directory(
    tmp_path: Path, field: str, value: str
) -> None:
    ledger = _ledger(tmp_path)
    action = _action()
    envelope: dict[str, Any] = {
        "provider": "meta_ads",
        "account_ref": action.account_binding.account_ref,
        "source": "LOCAL_CLI",
    }
    envelope[field] = value
    with pytest.raises(AdhelmError) as error:
        ledger.append(
            "changes",
            "META_CAMPAIGN_PAUSE_PLAN",
            meta_campaign_pause_plan_record(action).model_dump(mode="json"),
            **envelope,
        )
    assert error.value.code == "INVALID_META_PAUSE_PLAN_PROVENANCE"
    assert not ledger.ledger_root.exists()


def test_workspace_mismatch_refuses_before_creating_history(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, profile_ref="wrong-workspace")
    with pytest.raises(AdhelmError) as error:
        retain_meta_campaign_pause_plan(ledger, _action())
    assert error.value.code == "INVALID_META_PAUSE_PLAN_PROVENANCE"
    assert not ledger.ledger_root.exists()


def test_rehashed_wrong_envelope_is_rejected_on_history_scan(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    retain_meta_campaign_pause_plan(ledger, _action())
    segment = next((ledger.ledger_root / "changes").glob("*.jsonl"))
    records = [json.loads(line) for line in segment.read_bytes().splitlines()]
    assert records[-1]["record_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    records[-1]["source"] = "PROVIDER_UI"
    records[-1].pop("record_digest")
    records[-1]["record_digest"] = digest(records[-1])
    segment.write_bytes(b"".join(canonical_bytes(row) + b"\n" for row in records))
    with pytest.raises(AdhelmError) as error:
        ledger.checkpoint()
    assert error.value.code == "INVALID_META_PAUSE_PLAN_PROVENANCE"


def test_final_recheck_precedes_payload_bytes_and_failure_preserves_prior_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    first = retain_meta_campaign_pause_plan(ledger, _action())
    original = ledger._append_bytes
    events: list[str] = []

    def append_bytes(path: Path, data: bytes) -> None:
        if json.loads(data)["record_kind"] == "META_CAMPAIGN_PAUSE_PLAN":
            events.append("plan-bytes")
        original(path, data)

    def recheck() -> None:
        events.append("local-recheck")

    monkeypatch.setattr(ledger, "_append_bytes", append_bytes)
    retain_meta_campaign_pause_plan(ledger, _action(minutes=11), before_durable_append=recheck)
    assert events == ["local-recheck", "plan-bytes"]
    before = ledger.checkpoint()["checkpoint_digest"]

    def refuse() -> None:
        raise AdhelmError("CONFIGURATION_CHANGED", "Local state changed.")

    with pytest.raises(AdhelmError) as error:
        retain_meta_campaign_pause_plan(ledger, _action(minutes=12), before_durable_append=refuse)
    assert error.value.code == "CONFIGURATION_CHANGED"
    assert ledger.checkpoint()["checkpoint_digest"] == before
    assert load_meta_campaign_pause_plan(ledger, first["record_digest"])[0] == _action()


def test_historical_catalog_is_newest_first_and_paginates_exactly(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = retain_meta_campaign_pause_plan(ledger, _action())
    second = retain_meta_campaign_pause_plan(ledger, _action(minutes=11))
    page = meta_campaign_pause_plan_catalog(ledger, limit=1)
    assert page["total_count"] == 2
    assert page["next_cursor"] == "1"
    assert page["plans"][0]["record_digest"] == second["record_digest"]
    next_page = meta_campaign_pause_plan_catalog(ledger, cursor=1, limit=1)
    assert next_page["plans"][0]["record_digest"] == first["record_digest"]
    assert next_page["next_cursor"] is None
    assert next_page["checkpoint_digest"] == page["checkpoint_digest"]
    assert meta_campaign_pause_plan_catalog(ledger, cursor=2)["plans"] == []
    with pytest.raises(AdhelmError):
        meta_campaign_pause_plan_catalog(ledger, cursor=3)


@pytest.mark.parametrize("selection", [None, True, "A" * 64, "a" * 63, "../plan"])
def test_malformed_load_selection_fails_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: object
) -> None:
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(ledger, "checkpoint", lambda: pytest.fail("no checkpoint for bad input"))
    with pytest.raises(AdhelmError) as error:
        load_meta_campaign_pause_plan(ledger, selection)  # type: ignore[arg-type]
    assert error.value.code == "INVALID_META_PAUSE_PLAN_SELECTION"


@pytest.mark.parametrize(("cursor", "limit"), [(True, 1), (0, True), (-1, 1), (0, 501), (0, 0)])
def test_malformed_catalog_pagination_fails_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cursor: int, limit: int
) -> None:
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(ledger, "checkpoint", lambda: pytest.fail("no checkpoint for bad input"))
    with pytest.raises(AdhelmError) as error:
        meta_campaign_pause_plan_catalog(ledger, cursor=cursor, limit=limit)
    assert error.value.code == "INVALID_META_PAUSE_PLAN_SELECTION"
