"""Pure, fake-only tests for the Google Ads campaign PAUSE plan contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_protocol import (
    GOOGLE_OIDC_CANONICAL_ISSUER,
    derive_google_oidc_principal_ref,
)
from adhelm.providers.google_ads_pause_contract import (
    GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION,
    GOOGLE_ADS_CAMPAIGN_PAUSE_HOST,
    GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_STATUS,
    GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE,
    GoogleAdsCampaignPauseAccountBinding,
    GoogleAdsCampaignPauseCampaignBaseline,
    GoogleAdsCampaignPauseExecutionBinding,
    GoogleAdsCampaignPauseObservationBinding,
    GoogleAdsCampaignPausePlannedAction,
    build_google_ads_campaign_pause_plan,
    revalidate_google_ads_campaign_pause_planned_action,
)
from adhelm.safety import OperationKind

_T0 = datetime(2030, 1, 1, tzinfo=UTC)
_CUSTOMER = "1234567890"
_CAMPAIGN = "987654321"
_CLIENT = "m55-google-ads.apps.googleusercontent.com"
_PRINCIPAL = derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, "m55-subject")


def _account(**updates: object) -> GoogleAdsCampaignPauseAccountBinding:
    values: dict[str, object] = {
        "account_ref": f"google_ads.customer.{_CUSTOMER}",
        "account_client_ref": _CUSTOMER,
        "currency": "USD",
        "timezone": "UTC",
        "account_status": "CLOSED",
    }
    values.update(updates)
    return GoogleAdsCampaignPauseAccountBinding(**values)


def _execution(**updates: object) -> GoogleAdsCampaignPauseExecutionBinding:
    values: dict[str, object] = {
        "profile_ref": "google-ads-edit",
        "client_application_ref": _CLIENT,
        "principal_ref": _PRINCIPAL,
        "login_customer_client_ref": "0987654321",
        "credential_binding_ref": "credential-binding:sha256:" + "a" * 64,
        "configuration_digest": "b" * 64,
        "profile_configuration_digest": "c" * 64,
        "resolved_at": _T0,
        "expires_at": _T0 + timedelta(hours=1),
    }
    values.update(updates)
    return GoogleAdsCampaignPauseExecutionBinding(**values)


def _observation(**updates: object) -> GoogleAdsCampaignPauseObservationBinding:
    campaign = GoogleAdsCampaignPauseCampaignBaseline(
        campaign_ref=f"google_ads.campaign.{_CUSTOMER}.{_CAMPAIGN}",
        campaign_client_ref=_CAMPAIGN,
        account_ref=f"google_ads.customer.{_CUSTOMER}",
        account_client_ref=_CUSTOMER,
    )
    values: dict[str, object] = {
        "campaign": campaign,
        "read_started_at": _T0 + timedelta(seconds=1),
        "read_finished_at": _T0 + timedelta(seconds=2),
    }
    values.update(updates)
    return GoogleAdsCampaignPauseObservationBinding(**values)


def _action(
    *,
    workspace: str = "workspace-main",
    account: GoogleAdsCampaignPauseAccountBinding | None = None,
    execution: GoogleAdsCampaignPauseExecutionBinding | None = None,
    observation: GoogleAdsCampaignPauseObservationBinding | None = None,
    expires_at: datetime | None = None,
) -> GoogleAdsCampaignPausePlannedAction:
    return build_google_ads_campaign_pause_plan(
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
    assert raised.value.code == "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    assert raised.value.exit_code == ExitCode.INVALID_INPUT
    return raised.value


def test_builds_exact_zero_spend_non_authorizing_enabled_to_paused_plan() -> None:
    action = _action()
    subplan = action.plan.subplans[0]

    assert action.status == GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_STATUS
    assert action.plan.plan_id == f"google-ads-campaign-pause-{action.action_id}"
    assert subplan.subplan_id == action.plan.plan_id
    assert subplan.provider == "google_ads"
    assert subplan.operation is OperationKind.PAUSE
    assert subplan.provider_api_version == GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION
    assert subplan.account.account_ref == action.account_binding.account_ref
    assert subplan.account.stable_account_id == _CUSTOMER
    assert subplan.account.auth_profile_ref == action.execution_binding.profile_ref
    assert subplan.account.principal_ref == _PRINCIPAL
    assert subplan.account.client_application_ref == _CLIENT
    assert subplan.account.scopes == (GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE,)
    assert subplan.max_spend.minor_units == 0
    assert subplan.max_spend.currency == "USD"
    assert subplan.egress[0].host == GOOGLE_ADS_CAMPAIGN_PAUSE_HOST
    assert subplan.recovery_actions == (OperationKind.PAUSE,)
    assert subplan.before.revision is None
    assert subplan.after.revision is None
    assert action.observation_binding.campaign.status == "ENABLED"
    assert action.postcondition.configured_status == "PAUSED"
    assert action.postcondition.effective_delivery == "NOT_PREDICTED"
    assert action.execution_authority is False
    assert action.live_validated is False
    assert action.conditional_write_proof is False
    assert action.idempotency_proof is False


def test_before_state_binds_remote_account_campaign_not_local_provenance() -> None:
    original = _action()
    changed_local = _action(
        workspace="workspace-retained",
        execution=_execution(
            configuration_digest="d" * 64,
            profile_configuration_digest="e" * 64,
            resolved_at=_T0 - timedelta(minutes=1),
        ),
        observation=_observation(
            read_started_at=_T0 + timedelta(minutes=1),
            read_finished_at=_T0 + timedelta(minutes=1, seconds=1),
        ),
    )
    changed_account = _action(account=_account(account_status="ENABLED"))

    assert original.action_id != changed_local.action_id
    assert original.plan.plan_digest != changed_local.plan.plan_digest
    assert original.plan.subplans[0].before == changed_local.plan.subplans[0].before
    assert original.plan.subplans[0].after == changed_local.plan.subplans[0].after
    assert original.plan.subplans[0].before != changed_account.plan.subplans[0].before
    assert original.plan.subplans[0].after != changed_account.plan.subplans[0].after


@pytest.mark.parametrize(
    "changed",
    [
        lambda: _action(workspace="workspace-other"),
        lambda: _action(account=_account(currency="EUR")),
        lambda: _action(account=_account(timezone="America/Chicago")),
        lambda: _action(account=_account(account_status="SUSPENDED")),
        lambda: _action(execution=_execution(profile_ref="google-ads-edit-two")),
        lambda: _action(
            execution=_execution(client_application_ref="other.apps.googleusercontent.com")
        ),
        lambda: _action(
            execution=_execution(
                principal_ref=derive_google_oidc_principal_ref(
                    GOOGLE_OIDC_CANONICAL_ISSUER, "another-subject"
                )
            )
        ),
        lambda: _action(
            execution=_execution(credential_binding_ref="credential-binding:sha256:" + "f" * 64)
        ),
        lambda: _action(execution=_execution(login_customer_client_ref=None)),
        lambda: _action(execution=_execution(configuration_digest="d" * 64)),
        lambda: _action(execution=_execution(profile_configuration_digest="e" * 64)),
        lambda: _action(observation=_observation(read_finished_at=_T0 + timedelta(seconds=3))),
        lambda: _action(expires_at=_T0 + timedelta(minutes=11)),
    ],
)
def test_action_id_commits_every_outer_fact(changed: object) -> None:
    baseline = _action()
    assert callable(changed)
    replacement = changed()
    assert isinstance(replacement, GoogleAdsCampaignPausePlannedAction)
    assert replacement.action_id != baseline.action_id
    assert replacement.plan.plan_digest != baseline.plan.plan_digest


@pytest.mark.parametrize("status", ["CANCELED", "CLOSED", "ENABLED", "SUSPENDED"])
def test_closed_customer_status_is_retained_without_an_eligibility_claim(status: str) -> None:
    action = _action(account=_account(account_status=status))

    assert action.account_binding.account_status == status
    assert action.postcondition.account_status == status
    assert action.execution_binding.provider_write_permission == "NOT_PREPROVABLE"


@pytest.mark.parametrize("status", ["UNKNOWN", "UNSPECIFIED", "PAUSED", "REMOVED"])
def test_rejects_unknown_or_non_customer_account_status(status: str) -> None:
    with pytest.raises(ValueError):
        _account(account_status=status)


def test_rejects_non_enabled_or_cross_account_campaign_before_plan_creation() -> None:
    baseline = _observation().campaign
    non_enabled = baseline.model_copy(update={"status": "PAUSED"})
    cross_account = baseline.model_copy(
        update={
            "account_ref": "google_ads.customer.1111111111",
            "account_client_ref": "1111111111",
            "campaign_ref": "google_ads.campaign.1111111111.987654321",
        }
    )
    _invalid(
        lambda: _action(observation=_observation().model_copy(update={"campaign": non_enabled}))
    )
    _invalid(
        lambda: _action(observation=_observation().model_copy(update={"campaign": cross_account}))
    )


@pytest.mark.parametrize(
    "expiry",
    [
        _T0 + timedelta(seconds=2),
        _T0 + timedelta(minutes=15, seconds=3),
        _T0 + timedelta(hours=1, seconds=1),
    ],
)
def test_rejects_expiry_outside_observation_token_or_local_freshness(expiry: datetime) -> None:
    _invalid(lambda: _action(expires_at=expiry))


def test_allows_expiry_exactly_at_declared_token_expiry_when_still_fresh() -> None:
    execution = _execution(expires_at=_T0 + timedelta(minutes=12))
    action = _action(execution=execution, expires_at=_T0 + timedelta(minutes=12))

    assert action.plan.expires_at == _T0 + timedelta(minutes=12)


def test_rejects_reverse_or_unbounded_read_interval_and_wrong_resolution_order() -> None:
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
                    "read_finished_at": _T0 + timedelta(minutes=5, seconds=1, microseconds=1),
                }
            )
        )
    )
    _invalid(
        lambda: _action(
            execution=_execution(resolved_at=_T0 + timedelta(seconds=1, microseconds=1))
        )
    )


def test_revalidate_rejects_tampered_plan_postcondition_and_controls() -> None:
    action = _action()
    for path, value in (
        (("plan", "subplans", 0, "max_spend", "minor_units"), 1),
        (("plan", "subplans", 0, "before", "revision"), "provider-revision"),
        (("postcondition", "effective_delivery"), "DELIVERED"),
        (("execution_authority",), True),
        (("conditional_write_proof",), True),
        (("idempotency_proof",), True),
    ):
        data = action.to_data()
        cursor: Any = data
        for member in path[:-1]:
            cursor = cursor[member]
        cursor[path[-1]] = value
        _invalid(lambda data=data: revalidate_google_ads_campaign_pause_planned_action(data))


def test_revalidate_requires_canonical_utc_json_and_exact_scalar_shape() -> None:
    action = _action()
    for path, value in (
        (("execution_binding", "resolved_at"), "2030-01-01T00:00:00+00:00"),
        (("execution_binding", "expires_at"), "2030-01-01T01:00:00.000000Z"),
        (("schema_version",), True),
        (("observation_binding", "campaign", "schema_version"), 1.0),
        (("plan", "subplans", 0, "max_spend", "minor_units"), 0.0),
    ):
        data = action.to_data()
        cursor: Any = data
        for member in path[:-1]:
            cursor = cursor[member]
        cursor[path[-1]] = value
        _invalid(lambda data=data: revalidate_google_ads_campaign_pause_planned_action(data))
    data = action.to_data()
    data["unexpected"] = "rejected"
    _invalid(lambda: revalidate_google_ads_campaign_pause_planned_action(data))


def test_revalidate_rejects_raw_secret_slots_or_session_marker_fields() -> None:
    action = _action()
    for name, value in (
        ("credential_ref", "oauth-slot"),
        ("secondary_credential_ref", "developer-slot"),
        ("session_marker", "a" * 64),
        ("bearer_token", "private-token"),
    ):
        data = action.to_data()
        data["execution_binding"][name] = value
        _invalid(lambda data=data: revalidate_google_ads_campaign_pause_planned_action(data))


def test_to_data_round_trips_closed_data_without_secret_or_session_surface() -> None:
    action = _action()
    data = action.to_data()
    checked = revalidate_google_ads_campaign_pause_planned_action(data)

    assert checked == action
    assert checked.to_data() == data
    rendered = repr(data).casefold()
    assert "bearer" not in rendered
    assert "developer-token" not in rendered
    assert "session_marker" not in rendered
    assert "credential_ref" not in rendered
    assert set(data["execution_binding"]) == {
        "schema_version",
        "binding_kind",
        "profile_ref",
        "client_application_ref",
        "principal_ref",
        "required_scope",
        "login_customer_client_ref",
        "credential_binding_ref",
        "configuration_digest",
        "profile_configuration_digest",
        "resolved_at",
        "expires_at",
        "local_association_status",
        "provider_write_permission",
    }
