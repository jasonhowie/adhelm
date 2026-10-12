"""Independent M40 adversarial checks for exact historical Meta PAUSE plans."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from adhelm.errors import AdhelmError, ExitCode
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
    revalidate_meta_campaign_pause_planned_action,
)
from adhelm.saved_meta_pause_plans import (
    meta_campaign_pause_plan_catalog,
    retain_meta_campaign_pause_plan,
)

_START = datetime(2030, 1, 1, tzinfo=UTC)


def _account(**updates: object) -> MetaCampaignPauseAccountBinding:
    values: dict[str, object] = {
        "account_ref": "meta_ads.account.act_123456",
        "account_client_ref": "act_123456",
        "timezone": "UTC",
        "currency": "USD",
        "account_status": "7",
    }
    values.update(updates)
    return MetaCampaignPauseAccountBinding.model_validate(values)


def _execution(**updates: object) -> MetaCampaignPauseExecutionBinding:
    values: dict[str, object] = {
        "profile_ref": "meta-edit",
        "client_application_ref": "1234567890",
        "principal_ref": "meta_token_principal_v1." + "a" * 64,
        "credential_binding_ref": "credential-binding:sha256:" + "b" * 64,
        "configuration_digest": "c" * 64,
        "profile_configuration_digest": "d" * 64,
        "inspected_at": _START,
        "expires_at": _START + timedelta(hours=1),
        "data_access_expires_at": _START + timedelta(hours=2),
    }
    values.update(updates)
    return MetaCampaignPauseExecutionBinding.model_validate(values)


def _observation(**updates: object) -> MetaCampaignPauseObservationBinding:
    values: dict[str, object] = {
        "campaign": MetaCampaignPauseCampaignBaseline(
            campaign_ref="meta_ads.campaign.987654",
            campaign_client_ref="987654",
            account_client_ref="123456",
            configured_status="ACTIVE",
            effective_status="WITH_ISSUES",
        ),
        "read_started_at": _START + timedelta(seconds=1),
        "read_finished_at": _START + timedelta(seconds=2),
    }
    values.update(updates)
    return MetaCampaignPauseObservationBinding.model_validate(values)


def _action(
    *,
    account: MetaCampaignPauseAccountBinding | None = None,
    execution: MetaCampaignPauseExecutionBinding | None = None,
    observation: MetaCampaignPauseObservationBinding | None = None,
    expires_at: datetime | None = None,
) -> MetaCampaignPausePlannedAction:
    return build_meta_campaign_pause_plan(
        workspace_profile_ref="workspace-main",
        account_binding=_account() if account is None else account,
        execution_binding=_execution() if execution is None else execution,
        observation_binding=_observation() if observation is None else observation,
        expires_at=_START + timedelta(minutes=10) if expires_at is None else expires_at,
    )


def _set_path(data: dict[str, object], path: tuple[str | int, ...], value: object) -> None:
    current: Any = data
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = value


def _assert_invalid_action(data: object) -> None:
    with pytest.raises(AdhelmError) as raised:
        revalidate_meta_campaign_pause_planned_action(data)
    assert raised.value.code == "INVALID_META_CAMPAIGN_PAUSE_PLAN"
    assert raised.value.exit_code == ExitCode.INVALID_INPUT


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("workspace_binding", "schema_version"), 1.0),
        (("account_binding", "schema_version"), 1.0),
        (("execution_binding", "schema_version"), 1.0),
        (("observation_binding", "schema_version"), 1.0),
        (("postcondition", "schema_version"), 1.0),
        (("plan", "schema_version"), 1.0),
        (("plan", "subplans", 0, "max_spend", "minor_units"), 0.0),
        (("plan", "subplans", 0, "provider"), " meta_ads "),
        (("plan", "subplans", 0, "account", "account_ref"), " meta_ads.account.act_123456 "),
        (("plan", "subplans", 0, "account", "scopes"), [" ads_management "]),
        (("plan", "subplans", 0, "egress", 0, "host"), " graph.facebook.com "),
        (("plan", "subplans", 0, "egress", 0, "purpose"), " fixed Meta Graph v26 campaign pause "),
    ],
)
def test_revalidation_rejects_nested_numeric_aliases_and_whitespace_normalization(
    path: tuple[str | int, ...], replacement: object
) -> None:
    raw = deepcopy(_action().to_data())
    _set_path(raw, path, replacement)

    _assert_invalid_action(raw)


def test_outer_local_binding_changes_produce_a_new_executable_plan_digest() -> None:
    original = _action()
    changed = _action(
        execution=_execution(
            data_access_expires_at=_START + timedelta(hours=3),
        )
    )

    assert (
        original.execution_binding.data_access_expires_at
        != changed.execution_binding.data_access_expires_at
    )
    assert original.plan.subplans[0].before == changed.plan.subplans[0].before
    assert original.plan.subplans[0].after == changed.plan.subplans[0].after
    assert original.action_id != changed.action_id
    assert original.plan.plan_digest != changed.plan.plan_digest
    assert original.plan.subplans[0].subplan_digest != changed.plan.subplans[0].subplan_digest


def test_state_refs_are_remote_fingerprints_without_local_revision_labels() -> None:
    original = _action()
    effective_changed = _action(
        observation=_observation(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref="meta_ads.campaign.987654",
                campaign_client_ref="987654",
                account_client_ref="123456",
                configured_status="ACTIVE",
                effective_status="PAUSED",
            )
        )
    )

    original_subplan = original.plan.subplans[0]
    changed_subplan = effective_changed.plan.subplans[0]
    assert original_subplan.before.revision is None
    assert original_subplan.after.revision is None
    assert changed_subplan.before.revision is None
    assert changed_subplan.after.revision is None
    assert original_subplan.before != changed_subplan.before
    assert original_subplan.after == changed_subplan.after
    assert original.postcondition.effective_delivery == "NOT_PREDICTED"
    assert effective_changed.postcondition.effective_delivery == "NOT_PREDICTED"


def test_constructed_action_or_record_never_reaches_retention_callback(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, profile_ref="workspace-main")
    valid = _action()
    malformed_action = valid.model_copy(update={"plan": object()})
    calls: list[str] = []

    def callback() -> None:
        calls.append("callback")

    with pytest.raises(AdhelmError) as action_error:
        retain_meta_campaign_pause_plan(
            ledger,
            malformed_action,
            before_durable_append=callback,
        )
    assert action_error.value.code == "INVALID_META_CAMPAIGN_PAUSE_PLAN"
    assert calls == []
    assert not ledger.ledger_root.exists()

    malformed_record = object.__new__(type(meta_campaign_pause_plan_record(valid)))
    with pytest.raises(AdhelmError) as record_error:
        revalidate_meta_campaign_pause_plan_record(malformed_record)
    assert record_error.value.code == "INVALID_META_PAUSE_PLAN_RECORD"


def test_final_callback_failure_does_not_append_a_second_plan(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, profile_ref="workspace-main")
    first = retain_meta_campaign_pause_plan(ledger, _action())
    before = ledger.checkpoint()["checkpoint_digest"]
    calls: list[str] = []

    def final_check() -> None:
        calls.append("final")
        raise AdhelmError(
            "CONFIGURATION_CHANGED",
            "The retained local facts changed before persistence.",
            exit_code=ExitCode.INVALID_INPUT,
        )

    with pytest.raises(AdhelmError) as raised:
        retain_meta_campaign_pause_plan(
            ledger,
            _action(expires_at=_START + timedelta(minutes=11)),
            before_durable_append=final_check,
        )
    assert raised.value.code == "CONFIGURATION_CHANGED"
    assert calls == ["final"]
    assert ledger.checkpoint()["checkpoint_digest"] == before
    retained = [
        row for row in ledger.records("changes") if row["record_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    ]
    assert [row["record_digest"] for row in retained] == [first["record_digest"]]


def test_redacted_review_and_export_exclude_raw_execution_and_native_references(
    tmp_path: Path,
) -> None:
    from adhelm.meta_pause_plan_views import review_meta_campaign_pause_plan
    from adhelm.projections import export_verified

    action = _action()
    record = meta_campaign_pause_plan_record(action)
    review = review_meta_campaign_pause_plan(
        record,
        opaque_key=b"k" * 32,
        reviewed_at=_START + timedelta(minutes=1),
    )
    review_data = review.model_dump(mode="json")
    rendered_review = str(review_data)
    raw_values = (
        action.workspace_binding.workspace_profile_ref,
        action.account_binding.account_ref,
        action.account_binding.account_client_ref,
        action.execution_binding.profile_ref,
        action.execution_binding.client_application_ref,
        action.execution_binding.principal_ref,
        action.execution_binding.credential_binding_ref,
        action.execution_binding.configuration_digest,
        action.execution_binding.profile_configuration_digest,
        action.observation_binding.campaign.campaign_ref,
        action.observation_binding.campaign.campaign_client_ref,
        "ads_management",
    )
    assert all(raw not in rendered_review for raw in raw_values)
    assert review_data["workspace_profile_ref"].startswith("opaque:")
    assert review_data["account_ref"].startswith("opaque:")
    assert review_data["campaign_ref"].startswith("opaque:")
    assert review_data["auth_profile_ref"].startswith("opaque:")
    assert review_data["client_application_ref"].startswith("opaque:")
    assert review_data["baseline"] == {
        "configured_status": "ACTIVE",
        "effective_status": "WITH_ISSUES",
    }
    assert review_data["postcondition"] == {
        "postcondition_kind": "CONFIGURATION_ONLY",
        "configured_status": "PAUSED",
        "effective_delivery": "NOT_PREDICTED",
        "configuration_only": True,
    }
    assert review_data["execution_authority"] is False
    assert review_data["review_authorizes_execution"] is False
    assert review_data["remote_side_effects"] is False

    ledger = Ledger(tmp_path / "workspace", profile_ref="workspace-main")
    retain_meta_campaign_pause_plan(ledger, action)
    output = tmp_path / "export"
    export_verified(ledger, output, opaque_key=b"k" * 32)
    exported = (output / "records.jsonl").read_text(encoding="utf-8")
    assert all(raw not in exported for raw in raw_values)
    assert '"action"' not in exported
    assert '"execution_binding"' not in exported
    assert '"credential_binding_ref"' not in exported

    catalog = meta_campaign_pause_plan_catalog(ledger)
    assert all(raw not in str(catalog) for raw in raw_values)


def test_review_preflights_bounds_and_reports_expiry_without_currentness_claim() -> None:
    from adhelm.meta_pause_plan_views import review_meta_campaign_pause_plan

    record = meta_campaign_pause_plan_record(_action())
    with pytest.raises(AdhelmError) as too_small:
        review_meta_campaign_pause_plan(
            record,
            opaque_key=b"k" * 32,
            reviewed_at=_START + timedelta(minutes=1),
            max_output_bytes=1,
        )
    assert too_small.value.code == "OUTPUT_LIMIT_EXCEEDED"

    expired = review_meta_campaign_pause_plan(
        record,
        opaque_key=b"k" * 32,
        reviewed_at=_START + timedelta(minutes=10),
    )
    assert expired.expiry_status == "EXPIRED_AT_REVIEW"
    assert expired.execution_authority is False
    assert expired.review_authorizes_execution is False
    assert expired.current_state_status == "RECHECK_REQUIRED_BEFORE_EGRESS"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("execution_authority", True),
        ("account_status", "-1"),
    ],
)
def test_forged_review_and_record_cannot_serialize_as_safe_historical_output(
    field: str, replacement: object
) -> None:
    from adhelm.meta_pause_plan_views import review_meta_campaign_pause_plan

    record = meta_campaign_pause_plan_record(_action())
    review = review_meta_campaign_pause_plan(
        record,
        opaque_key=b"k" * 32,
        reviewed_at=_START + timedelta(minutes=1),
    )
    forged_review = review.model_copy(update={field: replacement})
    with pytest.raises(ValueError):
        forged_review.model_dump(mode="json")

    forged_record = record.model_copy(update={"action_digest": "e" * 64})
    with pytest.raises(AdhelmError) as raised:
        review_meta_campaign_pause_plan(
            forged_record,
            opaque_key=b"k" * 32,
            reviewed_at=_START + timedelta(minutes=1),
        )
    assert raised.value.code == "INVALID_META_PAUSE_PLAN_RECORD"
