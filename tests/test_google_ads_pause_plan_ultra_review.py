"""Independent adversarial review of Google Ads PAUSE plan retention facts."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_pause_plan_records import (
    google_ads_campaign_pause_plan_record,
    revalidate_google_ads_campaign_pause_plan_record,
)
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.providers.google_ads_pause_contract import (
    GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE,
    GoogleAdsCampaignPauseAccountBinding,
    GoogleAdsCampaignPauseCampaignBaseline,
    GoogleAdsCampaignPauseExecutionBinding,
    GoogleAdsCampaignPauseObservationBinding,
    GoogleAdsCampaignPausePlannedAction,
    build_google_ads_campaign_pause_plan,
    revalidate_google_ads_campaign_pause_planned_action,
)
from adhelm.safety import AccountIdentity
from adhelm.saved_google_ads_pause_plans import (
    google_ads_campaign_pause_plan_catalog,
    retain_google_ads_campaign_pause_plan,
)

_START = datetime(2030, 1, 1, tzinfo=UTC)
_CUSTOMER = "1234567890"
_CAMPAIGN = "9223372036854775807"
_PRINCIPAL = "google_oidc_principal_v1." + "a" * 64
_CREDENTIAL_BINDING = "credential-binding:sha256:" + "b" * 64


def _account(**updates: object) -> GoogleAdsCampaignPauseAccountBinding:
    values: dict[str, object] = {
        "account_ref": f"google_ads.customer.{_CUSTOMER}",
        "account_client_ref": _CUSTOMER,
        "currency": "USD",
        "timezone": "UTC",
        "account_status": "CLOSED",
    }
    values.update(updates)
    return GoogleAdsCampaignPauseAccountBinding.model_validate(values)


def _execution(**updates: object) -> GoogleAdsCampaignPauseExecutionBinding:
    values: dict[str, object] = {
        "profile_ref": "google-ads-edit",
        "client_application_ref": "independent-google-ads.apps.googleusercontent.com",
        "principal_ref": _PRINCIPAL,
        "login_customer_client_ref": "0987654321",
        "credential_binding_ref": _CREDENTIAL_BINDING,
        "configuration_digest": "c" * 64,
        "profile_configuration_digest": "d" * 64,
        "resolved_at": _START,
        "expires_at": _START + timedelta(hours=1),
    }
    values.update(updates)
    return GoogleAdsCampaignPauseExecutionBinding.model_validate(values)


def _observation(**updates: object) -> GoogleAdsCampaignPauseObservationBinding:
    campaign_values: dict[str, object] = {
        "campaign_ref": f"google_ads.campaign.{_CUSTOMER}.{_CAMPAIGN}",
        "campaign_client_ref": _CAMPAIGN,
        "account_ref": f"google_ads.customer.{_CUSTOMER}",
        "account_client_ref": _CUSTOMER,
        "status": "ENABLED",
    }
    values: dict[str, object] = {
        "campaign": GoogleAdsCampaignPauseCampaignBaseline.model_validate(campaign_values),
        "read_started_at": _START + timedelta(seconds=1),
        "read_finished_at": _START + timedelta(seconds=2),
    }
    values.update(updates)
    return GoogleAdsCampaignPauseObservationBinding.model_validate(values)


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
        expires_at=_START + timedelta(minutes=10) if expires_at is None else expires_at,
    )


def _set_path(data: dict[str, object], path: tuple[str | int, ...], value: object) -> None:
    current: Any = data
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = value


def _assert_invalid_action(value: object) -> None:
    with pytest.raises(AdhelmError) as raised:
        revalidate_google_ads_campaign_pause_planned_action(value)
    assert raised.value.code == "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    assert raised.value.exit_code == ExitCode.INVALID_INPUT


def test_exact_plan_is_non_authorizing_and_state_fingerprints_stay_remote_only() -> None:
    original = _action()
    binding_changed = _action(
        execution=_execution(profile_configuration_digest="e" * 64),
    )
    remote_changed = _action(account=_account(account_status="SUSPENDED"))

    original_subplan = original.plan.subplans[0]
    assert original.execution_authority is False
    assert original.live_validated is False
    assert original.conditional_write_proof is False
    assert original.idempotency_proof is False
    assert original.current_state_status == "RECHECK_REQUIRED_BEFORE_EGRESS"
    assert original.configuration_readback == "REQUIRED_AFTER_PAUSE"
    assert original.postcondition.configured_status == "PAUSED"
    assert original.postcondition.effective_delivery == "NOT_PREDICTED"
    assert original_subplan.operation.value == "PAUSE"
    assert original_subplan.max_spend.minor_units == 0
    assert type(original_subplan.account) is AccountIdentity
    assert original_subplan.account.scopes == (GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE,)
    assert original_subplan.before.revision is None
    assert original_subplan.after.revision is None

    # A local declared binding affects the executable plan but never the
    # allowlisted remote before/after fingerprints.
    assert original.action_id != binding_changed.action_id
    assert original.plan.plan_digest != binding_changed.plan.plan_digest
    assert original_subplan.before == binding_changed.plan.subplans[0].before
    assert original_subplan.after == binding_changed.plan.subplans[0].after

    # A remote customer fact affects only the remote baseline/precondition.
    assert original_subplan.before != remote_changed.plan.subplans[0].before
    assert original_subplan.after != remote_changed.plan.subplans[0].after


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("workspace_binding", "schema_version"), 1.0),
        (("account_binding", "schema_version"), True),
        (("execution_binding", "resolved_at"), "2030-01-01T00:00:00.000000Z"),
        (("observation_binding", "campaign", "status"), " ENABLED "),
        (("plan", "schema_version"), 1.0),
        (("plan", "subplans", 0, "max_spend", "minor_units"), 0.0),
        (("plan", "subplans", 0, "provider"), " google_ads "),
        (("plan", "subplans", 0, "account", "account_ref"), " google_ads.customer.1234567890 "),
        (
            ("execution_binding", "required_scope"),
            " https://www.googleapis.com/auth/adwords ",
        ),
        (
            ("plan", "subplans", 0, "account", "scopes"),
            [" https://www.googleapis.com/auth/adwords "],
        ),
        (("plan", "subplans", 0, "before", "revision"), "invented-provider-revision"),
        (("plan", "subplans", 0, "after", "revision"), "invented-provider-revision"),
        (("plan", "subplans", 0, "egress", 0, "host"), " googleads.googleapis.com "),
        (("plan", "subplans", 0, "egress", 0, "purpose"), " fixed Google Ads v25 campaign pause "),
    ],
)
def test_revalidation_rejects_raw_scalar_aliases_before_normalization(
    path: tuple[str | int, ...], replacement: object
) -> None:
    raw = deepcopy(_action().to_data())
    _set_path(raw, path, replacement)

    _assert_invalid_action(raw)


def test_time_and_cross_customer_edges_fail_closed_without_rebuilding_a_plan() -> None:
    exact_five_minutes = GoogleAdsCampaignPauseObservationBinding(
        campaign=_observation().campaign,
        read_started_at=_START + timedelta(seconds=1),
        read_finished_at=_START + timedelta(minutes=5, seconds=1),
    )
    assert _action(observation=exact_five_minutes).plan.expires_at == _START + timedelta(minutes=10)

    with pytest.raises(ValidationError):
        GoogleAdsCampaignPauseObservationBinding(
            campaign=_observation().campaign,
            read_started_at=_START + timedelta(seconds=1),
            read_finished_at=_START + timedelta(minutes=5, seconds=1, microseconds=1),
        )

    with pytest.raises(AdhelmError) as late_resolution:
        _action(execution=_execution(resolved_at=_START + timedelta(seconds=2)))
    assert late_resolution.value.code == "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"

    with pytest.raises(AdhelmError) as expiry_at_finished_read:
        _action(expires_at=_START + timedelta(seconds=2))
    assert expiry_at_finished_read.value.code == "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"

    with pytest.raises(AdhelmError) as freshness:
        _action(expires_at=_START + timedelta(minutes=15, seconds=2, microseconds=1))
    assert freshness.value.code == "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"

    other_customer = GoogleAdsCampaignPauseCampaignBaseline(
        campaign_ref="google_ads.campaign.0000000001.42",
        campaign_client_ref="42",
        account_ref="google_ads.customer.0000000001",
        account_client_ref="0000000001",
        status="ENABLED",
    )
    with pytest.raises(AdhelmError) as cross_account:
        _action(observation=_observation(campaign=other_customer))
    assert cross_account.value.code == "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"


def test_constructed_action_and_cross_workspace_record_never_reach_durable_append(
    tmp_path: Path,
) -> None:
    action = _action()
    malformed = action.model_copy(update={"plan": object()})
    calls: list[str] = []

    def callback() -> None:
        calls.append("called")

    ledger = Ledger(tmp_path / "malformed", profile_ref="workspace-main")
    with pytest.raises(AdhelmError) as malformed_error:
        retain_google_ads_campaign_pause_plan(ledger, malformed, before_durable_append=callback)
    assert malformed_error.value.code == "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    assert calls == []
    assert not ledger.ledger_root.exists()

    wrong_workspace = Ledger(tmp_path / "other", profile_ref="different-workspace")
    with pytest.raises(AdhelmError) as workspace_error:
        retain_google_ads_campaign_pause_plan(
            wrong_workspace, action, before_durable_append=callback
        )
    assert workspace_error.value.code == "INVALID_GOOGLE_ADS_PAUSE_PLAN_PROVENANCE"
    assert calls == []
    assert not wrong_workspace.ledger_root.exists()


def test_callback_failure_preserves_checkpoint_and_catalog_facts(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, profile_ref="workspace-main")
    first = retain_google_ads_campaign_pause_plan(ledger, _action())
    before = ledger.checkpoint()["checkpoint_digest"]
    calls: list[str] = []

    def final_check() -> None:
        calls.append("final")
        raise AdhelmError("CONFIGURATION_CHANGED", "fixture local drift")

    with pytest.raises(AdhelmError) as raised:
        retain_google_ads_campaign_pause_plan(
            ledger,
            _action(expires_at=_START + timedelta(minutes=11)),
            before_durable_append=final_check,
        )
    assert raised.value.code == "CONFIGURATION_CHANGED"
    assert calls == ["final"]
    assert ledger.checkpoint()["checkpoint_digest"] == before
    assert google_ads_campaign_pause_plan_catalog(ledger)["total_count"] == 1
    records = [
        row
        for row in ledger.records("changes")
        if row["record_kind"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    ]
    assert [row["record_digest"] for row in records] == [first["record_digest"]]


def test_review_and_export_use_hmac_refs_without_execution_or_credential_fields(
    tmp_path: Path,
) -> None:
    from adhelm.google_ads_pause_plan_views import review_google_ads_campaign_pause_plan

    action = _action()
    record = google_ads_campaign_pause_plan_record(action)
    review = review_google_ads_campaign_pause_plan(
        record,
        opaque_key=b"k" * 32,
        reviewed_at=_START + timedelta(minutes=1),
    )
    rendered_review = str(review.model_dump(mode="json"))
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
        GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE,
    )
    assert all(raw not in rendered_review for raw in raw_values)
    assert review.workspace_profile_ref.startswith("opaque:")
    assert review.account_ref.startswith("opaque:")
    assert review.campaign_ref.startswith("opaque:")
    assert review.auth_profile_ref.startswith("opaque:")
    assert review.client_application_ref.startswith("opaque:")
    assert review.execution_authority is False
    assert review.review_authorizes_execution is False
    assert review.remote_side_effects is False

    ledger = Ledger(tmp_path / "workspace", profile_ref="workspace-main")
    retain_google_ads_campaign_pause_plan(ledger, action)
    output = tmp_path / "export"
    export_verified(ledger, output, opaque_key=b"k" * 32)
    exported = (output / "records.jsonl").read_text(encoding="utf-8")
    assert all(raw not in exported for raw in raw_values)
    assert '"action"' not in exported
    assert '"execution_binding"' not in exported
    assert '"credential_binding_ref"' not in exported


def test_complete_record_revalidation_rejects_extra_session_marker_or_copied_digest() -> None:
    record = google_ads_campaign_pause_plan_record(_action())
    raw = record.model_dump(mode="json")
    raw["action"]["execution_binding"]["session_continuity_marker"] = "not-durable"
    with pytest.raises(AdhelmError) as marker_error:
        revalidate_google_ads_campaign_pause_plan_record(raw)
    assert marker_error.value.code == "INVALID_GOOGLE_ADS_PAUSE_PLAN_RECORD"

    copied = record.model_copy(update={"action_digest": "e" * 64})
    with pytest.raises(AdhelmError) as digest_error:
        revalidate_google_ads_campaign_pause_plan_record(copied)
    assert digest_error.value.code == "INVALID_GOOGLE_ADS_PAUSE_PLAN_RECORD"


def test_record_revalidation_rejects_redundant_wire_timestamp_spelling() -> None:
    raw = google_ads_campaign_pause_plan_record(_action()).model_dump(mode="json")
    raw["expires_at"] = "2030-01-01T00:10:00.000000Z"

    with pytest.raises(AdhelmError) as error:
        revalidate_google_ads_campaign_pause_plan_record(raw)
    assert error.value.code == "INVALID_GOOGLE_ADS_PAUSE_PLAN_RECORD"
