"""Pure, fake-only tests for the fixed Meta campaign PAUSE contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from adhelm.errors import AdhelmError, ExitCode
from adhelm.providers.meta_pause_contract import (
    META_CAMPAIGN_PAUSE_API_VERSION,
    META_CAMPAIGN_PAUSE_HOST,
    META_CAMPAIGN_PAUSE_PLAN_STATUS,
    META_CAMPAIGN_PAUSE_SCOPE,
    MetaCampaignPauseAccountBinding,
    MetaCampaignPauseCampaignBaseline,
    MetaCampaignPauseExecutionBinding,
    MetaCampaignPauseObservationBinding,
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
    revalidate_meta_campaign_pause_planned_action,
)
from adhelm.safety import OperationKind

_T0 = datetime(2030, 1, 1, tzinfo=UTC)


def _account(**updates: object) -> MetaCampaignPauseAccountBinding:
    values: dict[str, object] = {
        "account_ref": "meta_ads.account.act_123456",
        "account_client_ref": "act_123456",
        "timezone": "UTC",
        "currency": "USD",
        "account_status": "7",
    }
    values.update(updates)
    return MetaCampaignPauseAccountBinding(**values)


def _execution(**updates: object) -> MetaCampaignPauseExecutionBinding:
    values: dict[str, object] = {
        "profile_ref": "meta-edit",
        "client_application_ref": "1234567890",
        "principal_ref": "meta_token_principal_v1." + "a" * 64,
        "credential_binding_ref": "credential-binding:sha256:" + "b" * 64,
        "configuration_digest": "c" * 64,
        "profile_configuration_digest": "d" * 64,
        "inspected_at": _T0,
        "expires_at": _T0 + timedelta(hours=1),
        "data_access_expires_at": _T0 + timedelta(hours=2),
    }
    values.update(updates)
    return MetaCampaignPauseExecutionBinding(**values)


def _observation(**updates: object) -> MetaCampaignPauseObservationBinding:
    campaign_values: dict[str, object] = {
        "campaign_ref": "meta_ads.campaign.987654",
        "campaign_client_ref": "987654",
        "account_client_ref": "123456",
        "effective_status": "WITH_ISSUES",
    }
    campaign = MetaCampaignPauseCampaignBaseline(**campaign_values)
    values: dict[str, object] = {
        "campaign": campaign,
        "read_started_at": _T0 + timedelta(seconds=1),
        "read_finished_at": _T0 + timedelta(seconds=2),
    }
    values.update(updates)
    return MetaCampaignPauseObservationBinding(**values)


def _action(
    *,
    workspace: str = "workspace-main",
    account: MetaCampaignPauseAccountBinding | None = None,
    execution: MetaCampaignPauseExecutionBinding | None = None,
    observation: MetaCampaignPauseObservationBinding | None = None,
    expires_at: datetime | None = None,
) -> MetaCampaignPausePlannedAction:
    return build_meta_campaign_pause_plan(
        workspace_profile_ref=workspace,
        account_binding=_account() if account is None else account,
        execution_binding=_execution() if execution is None else execution,
        observation_binding=_observation() if observation is None else observation,
        expires_at=_T0 + timedelta(minutes=10) if expires_at is None else expires_at,
    )


def _invalid(call: object) -> AdhelmError:
    with pytest.raises(AdhelmError) as raised:
        assert callable(call)
        call()
    assert raised.value.code == "INVALID_META_CAMPAIGN_PAUSE_PLAN"
    assert raised.value.exit_code == ExitCode.INVALID_INPUT
    return raised.value


def test_builds_exact_zero_spend_pause_plan_without_execution_authority() -> None:
    action = _action()
    subplan = action.plan.subplans[0]

    assert action.status == META_CAMPAIGN_PAUSE_PLAN_STATUS
    assert len(action.action_id) == 64
    assert action.plan.plan_id == f"meta-campaign-pause-{action.action_id}"
    assert subplan.subplan_id == action.plan.plan_id
    assert subplan.provider == "meta_ads"
    assert subplan.operation is OperationKind.PAUSE
    assert subplan.provider_api_version == META_CAMPAIGN_PAUSE_API_VERSION
    assert subplan.account.account_ref == action.account_binding.account_ref
    assert subplan.account.stable_account_id == action.account_binding.account_client_ref
    assert subplan.account.auth_profile_ref == action.execution_binding.profile_ref
    assert subplan.account.principal_ref == action.execution_binding.principal_ref
    assert subplan.account.client_application_ref == action.execution_binding.client_application_ref
    assert subplan.account.scopes == (META_CAMPAIGN_PAUSE_SCOPE,)
    assert subplan.max_spend.minor_units == 0
    assert subplan.max_spend.currency == "USD"
    assert subplan.egress[0].host == META_CAMPAIGN_PAUSE_HOST
    assert subplan.recovery_actions == (OperationKind.PAUSE,)
    assert subplan.before.revision is None
    assert subplan.after.revision is None
    assert action.execution_authority is False
    assert action.live_validated is False
    assert action.conditional_write_proof is False
    assert action.idempotency_proof is False
    assert action.configuration_readback == "REQUIRED_AFTER_PAUSE"
    assert action.recovery_semantics == "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"


def test_before_state_binds_complete_remote_observation_but_not_local_provenance() -> None:
    original = _action()
    local_changed = _action(
        workspace="workspace-retained",
        execution=_execution(
            configuration_digest="e" * 64,
            profile_configuration_digest="f" * 64,
            inspected_at=_T0 - timedelta(minutes=1),
        ),
        observation=_observation(
            read_started_at=_T0 + timedelta(minutes=1),
            read_finished_at=_T0 + timedelta(minutes=1, seconds=1),
        ),
    )
    effective_changed = _action(
        observation=_observation(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref="meta_ads.campaign.987654",
                campaign_client_ref="987654",
                account_client_ref="123456",
                effective_status="PAUSED",
            )
        )
    )

    assert original.action_id != local_changed.action_id
    assert original.plan.plan_digest != local_changed.plan.plan_digest
    assert original.plan.subplans[0].before == local_changed.plan.subplans[0].before
    assert original.plan.subplans[0].after == local_changed.plan.subplans[0].after
    assert original.action_id != effective_changed.action_id
    assert original.plan.subplans[0].before != effective_changed.plan.subplans[0].before
    assert original.plan.subplans[0].after == effective_changed.plan.subplans[0].after
    assert original.postcondition.effective_delivery == "NOT_PREDICTED"
    assert effective_changed.postcondition.effective_delivery == "NOT_PREDICTED"


@pytest.mark.parametrize(
    "changed",
    [
        lambda: _action(workspace="workspace-other"),
        lambda: _action(account=_account(account_status="8")),
        lambda: _action(execution=_execution(profile_ref="meta-edit-two")),
        lambda: _action(execution=_execution(client_application_ref="1234567891")),
        lambda: _action(execution=_execution(principal_ref="meta_token_principal_v1." + "e" * 64)),
        lambda: _action(
            execution=_execution(credential_binding_ref="credential-binding:sha256:" + "e" * 64)
        ),
        lambda: _action(execution=_execution(configuration_digest="e" * 64)),
        lambda: _action(execution=_execution(profile_configuration_digest="e" * 64)),
        lambda: _action(observation=_observation(read_finished_at=_T0 + timedelta(seconds=3))),
        lambda: _action(expires_at=_T0 + timedelta(minutes=11)),
    ],
)
def test_action_id_commits_every_outer_workspace_execution_and_observation_fact(
    changed: object,
) -> None:
    baseline = _action()
    assert callable(changed)
    replacement = changed()
    assert isinstance(replacement, MetaCampaignPausePlannedAction)
    assert replacement.action_id != baseline.action_id
    assert replacement.plan.plan_digest != baseline.plan.plan_digest


@pytest.mark.parametrize(
    "campaign",
    [
        MetaCampaignPauseCampaignBaseline(
            campaign_ref="meta_ads.campaign.987654",
            campaign_client_ref="987654",
            account_client_ref="123456",
            configured_status="ACTIVE",
            effective_status="ACTIVE",
        ).model_copy(update={"configured_status": "PAUSED"}),
        MetaCampaignPauseCampaignBaseline(
            campaign_ref="meta_ads.campaign.987654",
            campaign_client_ref="987654",
            account_client_ref="123456",
            configured_status="ACTIVE",
            effective_status="ACTIVE",
        ).model_copy(update={"account_client_ref": "999999"}),
    ],
)
def test_rejects_nonactive_or_cross_account_campaign_before_plan_creation(
    campaign: MetaCampaignPauseCampaignBaseline,
) -> None:
    observation = _observation().model_copy(update={"campaign": campaign})
    _invalid(lambda: _action(observation=observation))


def test_preserves_canonical_numeric_account_status_without_inventing_eligibility() -> None:
    action = _action(account=_account(account_status="0"))

    assert action.account_binding.account_status == "0"
    assert action.postcondition.account_status == "0"
    assert action.execution_binding.provider_write_permission == "NOT_PREPROVABLE"


def test_rejects_negative_account_status_without_mapping_numeric_status_to_eligibility() -> None:
    with pytest.raises(ValueError):
        _account(account_status="-1")


@pytest.mark.parametrize(
    "expiry",
    [
        _T0 + timedelta(seconds=2),
        _T0 + timedelta(minutes=15, seconds=3),
        _T0 + timedelta(hours=1, seconds=1),
    ],
)
def test_rejects_expiry_outside_completed_read_inspected_expiry_or_local_freshness(
    expiry: datetime,
) -> None:
    _invalid(lambda: _action(expires_at=expiry))


def test_allows_expiry_exactly_at_an_inspected_expiry_when_still_fresh() -> None:
    execution = _execution(
        expires_at=_T0 + timedelta(minutes=12),
        data_access_expires_at=_T0 + timedelta(minutes=13),
    )
    action = _action(execution=execution, expires_at=_T0 + timedelta(minutes=12))

    assert action.plan.expires_at == _T0 + timedelta(minutes=12)


def test_rejects_reverse_or_unbounded_campaign_read_interval() -> None:
    valid = _observation()
    _invalid(
        lambda: _action(
            observation=valid.model_copy(
                update={
                    "read_started_at": _T0 + timedelta(seconds=3),
                    "read_finished_at": _T0 + timedelta(seconds=2),
                }
            )
        )
    )
    _invalid(
        lambda: _action(
            observation=valid.model_copy(
                update={
                    "read_started_at": _T0,
                    "read_finished_at": _T0 + timedelta(minutes=5, microseconds=1),
                }
            )
        )
    )


def test_rejects_execution_inspection_after_read_or_expired_at_read_completion() -> None:
    _invalid(
        lambda: _action(
            execution=_execution(inspected_at=_T0 + timedelta(seconds=2, microseconds=1))
        )
    )
    _invalid(lambda: _action(execution=_execution(expires_at=_T0 + timedelta(seconds=2))))


def test_revalidate_rejects_tampered_plan_postcondition_and_closed_controls() -> None:
    action = _action()
    for path, value in (
        (("plan", "subplans", 0, "max_spend", "minor_units"), 1),
        (("postcondition", "effective_delivery"), "PAUSED"),
        (("execution_authority",), True),
        (("conditional_write_proof",), True),
        (("idempotency_proof",), True),
    ):
        data = action.to_data()
        cursor: Any = data
        for part in path[:-1]:
            cursor = cursor[part]
        cursor[path[-1]] = value
        _invalid(lambda data=data: revalidate_meta_campaign_pause_planned_action(data))


def test_revalidate_requires_canonical_utc_json_and_exact_schema_shape() -> None:
    action = _action()
    data = action.to_data()
    data["execution_binding"]["inspected_at"] = "2030-01-01T00:00:00+00:00"
    _invalid(lambda: revalidate_meta_campaign_pause_planned_action(data))

    data = action.to_data()
    data["schema_version"] = True
    _invalid(lambda: revalidate_meta_campaign_pause_planned_action(data))

    data = action.to_data()
    data["observation_binding"]["campaign"]["schema_version"] = 1.0
    _invalid(lambda: revalidate_meta_campaign_pause_planned_action(data))

    data = action.to_data()
    data["plan"]["subplans"][0]["max_spend"]["minor_units"] = 0.0
    _invalid(lambda: revalidate_meta_campaign_pause_planned_action(data))

    data = action.to_data()
    data["unexpected"] = "rejected"
    _invalid(lambda: revalidate_meta_campaign_pause_planned_action(data))


def test_to_data_and_revalidation_round_trip_only_closed_serializable_data() -> None:
    action = _action()
    data = action.to_data()
    checked = revalidate_meta_campaign_pause_planned_action(data)

    assert checked == action
    assert checked.to_data() == data
    assert "fixture-subject-token" not in repr(data)
    assert "access_token" not in repr(data).casefold()
