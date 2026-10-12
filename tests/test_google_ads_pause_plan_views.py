"""Bounded, redacted historical sharing views for Google Ads PAUSE plans."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.google_ads_pause_plan_records import google_ads_campaign_pause_plan_record
from adhelm.google_ads_pause_plan_views import review_google_ads_campaign_pause_plan
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.providers.google_ads_pause_contract import (
    GoogleAdsCampaignPauseAccountBinding,
    GoogleAdsCampaignPauseCampaignBaseline,
    GoogleAdsCampaignPauseExecutionBinding,
    GoogleAdsCampaignPauseObservationBinding,
    GoogleAdsCampaignPausePlannedAction,
    build_google_ads_campaign_pause_plan,
)
from adhelm.saved_google_ads_pause_plans import retain_google_ads_campaign_pause_plan

_START = datetime(2026, 9, 6, 12, tzinfo=UTC)
_KEY = b"g" * 32


def _action(
    *, minutes: int = 10, login_customer: str | None = "0987654321"
) -> GoogleAdsCampaignPausePlannedAction:
    return build_google_ads_campaign_pause_plan(
        workspace_profile_ref="workspace-private-marker",
        account_binding=GoogleAdsCampaignPauseAccountBinding(
            account_ref="google_ads.customer.1234567890",
            account_client_ref="1234567890",
            currency="USD",
            timezone="UTC",
            account_status="CLOSED",
        ),
        execution_binding=GoogleAdsCampaignPauseExecutionBinding(
            profile_ref="google-ads-edit-private-marker",
            client_application_ref="fixture-google-ads.apps.googleusercontent.com",
            principal_ref="google_oidc_principal_v1." + "a" * 64,
            credential_binding_ref="credential-binding:sha256:" + "b" * 64,
            configuration_digest="c" * 64,
            profile_configuration_digest="d" * 64,
            resolved_at=_START,
            expires_at=_START + timedelta(hours=1),
            login_customer_client_ref=login_customer,
        ),
        observation_binding=GoogleAdsCampaignPauseObservationBinding(
            campaign=GoogleAdsCampaignPauseCampaignBaseline(
                campaign_ref="google_ads.campaign.1234567890.42",
                campaign_client_ref="42",
                account_ref="google_ads.customer.1234567890",
                account_client_ref="1234567890",
            ),
            read_started_at=_START + timedelta(seconds=1),
            read_finished_at=_START + timedelta(seconds=2),
        ),
        expires_at=_START + timedelta(minutes=minutes),
    )


def _record(*, minutes: int = 10, login_customer: str | None = "0987654321") -> object:
    return google_ads_campaign_pause_plan_record(
        _action(minutes=minutes, login_customer=login_customer)
    )


def test_direct_review_is_historical_redacted_and_non_authorizing() -> None:
    record = _record()
    review = review_google_ads_campaign_pause_plan(
        record,
        opaque_key=_KEY,
        reviewed_at=_START + timedelta(minutes=5),
    )

    assert review.action_digest == record.action_digest  # type: ignore[union-attr]
    assert review.plan_digest == record.plan_digest  # type: ignore[union-attr]
    assert review.subplan_digests == record.action.plan.subplan_digests  # type: ignore[union-attr]
    assert review.maximum_incremental_spend.minor_units == 0
    assert review.maximum_incremental_spend.currency == "USD"
    assert review.baseline.configured_status == "ENABLED"
    assert review.postcondition.model_dump(mode="json") == {
        "postcondition_kind": "CONFIGURATION_ONLY",
        "configured_status": "PAUSED",
        "effective_delivery": "NOT_PREDICTED",
        "configuration_only": True,
    }
    assert review.observation_interval.read_started_at == _START + timedelta(seconds=1)
    assert review.observation_interval.read_finished_at == _START + timedelta(seconds=2)
    assert review.expiry_status == "NOT_EXPIRED_AT_REVIEW"
    assert review.execution_authority is False
    assert review.remote_side_effects is False
    assert review.review_authorizes_execution is False
    assert review.provider_conditional_write == "NOT_DOCUMENTED"
    assert review.provider_idempotency == "NOT_DOCUMENTED"
    assert (
        review.effective_delivery_limitation
        == "CONFIGURED_PAUSE_DOES_NOT_PROVE_EFFECTIVE_DELIVERY_OR_ZERO_RESIDUAL_SPEND"
    )

    encoded = canonical_bytes(review)
    for raw in (
        b"workspace-private-marker",
        b"google_ads.customer.1234567890",
        b"1234567890",
        b"google_ads.campaign.1234567890.42",
        b"fixture-google-ads.apps.googleusercontent.com",
        b"google-ads-edit-private-marker",
        b"0987654321",
        record.action.action_id.encode("ascii"),  # type: ignore[union-attr]
        b"credential-binding:sha256:",
        b"google_oidc_principal_v1.",
        b'"configuration_digest"',
        b'"profile_configuration_digest"',
        b'"required_scope"',
    ):
        assert raw not in encoded
    assert review.workspace_profile_ref.startswith("opaque:")
    assert review.account_ref.startswith("opaque:")
    assert review.campaign_ref.startswith("opaque:")
    assert review.auth_profile_ref.startswith("opaque:")
    assert review.client_application_ref.startswith("opaque:")
    assert review.login_customer_client_ref is not None
    assert review.login_customer_client_ref.startswith("opaque:")


def test_optional_login_customer_is_preserved_only_as_a_null_or_opaque_reference() -> None:
    review = review_google_ads_campaign_pause_plan(
        _record(login_customer=None),
        opaque_key=_KEY,
        reviewed_at=_START + timedelta(minutes=5),
    )
    assert review.login_customer_client_ref is None


def test_expired_plan_remains_historical_review_only() -> None:
    review = review_google_ads_campaign_pause_plan(
        _record(minutes=10),
        opaque_key=_KEY,
        reviewed_at=_START + timedelta(minutes=10),
    )
    assert review.expiry_status == "EXPIRED_AT_REVIEW"
    assert review.current_state_status == "RECHECK_REQUIRED_BEFORE_EGRESS"
    assert review.review_authorizes_execution is False


def test_review_cannot_predate_the_completed_historical_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adhelm import projections

    used_keys: list[bytes] = []
    original = projections._redact_payload

    def capture_key(
        value: Any, key: bytes, include_marketing_text: bool, *args: Any, **kwargs: Any
    ) -> Any:
        used_keys.append(key)
        return original(value, key, include_marketing_text, *args, **kwargs)

    monkeypatch.setattr(projections, "_redact_payload", capture_key)
    with pytest.raises(AdhelmError) as error:
        review_google_ads_campaign_pause_plan(
            _record(),
            opaque_key=_KEY,
            reviewed_at=_START + timedelta(seconds=1),
        )
    assert error.value.code == "INVALID_GOOGLE_ADS_PAUSE_PLAN_REVIEW"
    assert set(used_keys) == {b"\0" * 32}


def test_invalid_or_rehashed_record_is_refused_before_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    raw = record.model_dump(mode="json")  # type: ignore[union-attr]
    raw["action"]["observation_binding"]["campaign"]["campaign_client_ref"] = "43"
    raw["action_digest"] = digest(raw["action"])
    raw["plan_digest"] = digest(raw["action"]["plan"])

    def no_redaction(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid action reached the sharing projection")

    monkeypatch.setattr("adhelm.projections._redact_payload", no_redaction)
    with pytest.raises(AdhelmError) as error:
        review_google_ads_campaign_pause_plan(
            raw,
            opaque_key=_KEY,
            reviewed_at=_START + timedelta(minutes=5),
        )
    assert error.value.code == "INVALID_GOOGLE_ADS_PAUSE_PLAN_RECORD"


def test_output_bound_fails_before_the_callers_sharing_key_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    complete = review_google_ads_campaign_pause_plan(
        record,
        opaque_key=_KEY,
        reviewed_at=_START + timedelta(minutes=5),
    )
    used_keys: list[bytes] = []
    from adhelm import projections

    original = projections._redact_payload

    def capture_key(
        value: Any, key: bytes, include_marketing_text: bool, *args: Any, **kwargs: Any
    ) -> Any:
        used_keys.append(key)
        return original(value, key, include_marketing_text, *args, **kwargs)

    monkeypatch.setattr(projections, "_redact_payload", capture_key)
    caller_key = b"q" * 32
    with pytest.raises(AdhelmError) as error:
        review_google_ads_campaign_pause_plan(
            record,
            opaque_key=caller_key,
            reviewed_at=_START + timedelta(minutes=5),
            max_output_bytes=len(canonical_bytes(complete)) - 1,
        )
    assert error.value.code == "OUTPUT_LIMIT_EXCEEDED"
    assert used_keys
    assert set(used_keys) == {b"\0" * 32}


@pytest.mark.parametrize(
    ("opaque_key", "reviewed_at", "maximum", "code"),
    [
        (b"short", _START, 65_536, "EXPORT_KEY_REQUIRED"),
        (b"x" * 32, _START.replace(tzinfo=None), 65_536, "CLOCK_INVALID"),
        (b"x" * 32, _START, True, "INVALID_OUTPUT_LIMIT"),
        (b"x" * 32, _START, 65_537, "INVALID_OUTPUT_LIMIT"),
    ],
)
def test_direct_review_rejects_invalid_controls(
    opaque_key: object, reviewed_at: object, maximum: object, code: str
) -> None:
    with pytest.raises(AdhelmError) as error:
        review_google_ads_campaign_pause_plan(
            _record(),
            opaque_key=opaque_key,  # type: ignore[arg-type]
            reviewed_at=reviewed_at,  # type: ignore[arg-type]
            max_output_bytes=maximum,  # type: ignore[arg-type]
        )
    assert error.value.code == code


def test_jsonl_and_csv_export_apply_google_ads_pause_allowlist_even_with_marketing_text(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "data", profile_ref="workspace-private-marker")
    action = _action()
    retained = retain_google_ads_campaign_pause_plan(ledger, action)
    output = tmp_path / "export"

    manifest = export_verified(
        ledger,
        output,
        opaque_key=_KEY,
        include_marketing_text=True,
    )
    assert manifest["files"]["records.jsonl"]["rows"] >= 1
    rows = [json.loads(line) for line in (output / "records.jsonl").read_bytes().splitlines()]
    google_rows = [row for row in rows if row["record_kind"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"]
    assert len(google_rows) == 1
    payload = google_rows[0]["payload"]
    assert payload["event_type"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    assert payload["action_digest"] == digest(action)
    assert payload["plan_digest"] == action.plan.plan_digest
    assert payload["subplan_digests"] == list(action.plan.subplan_digests)
    assert payload["maximum_incremental_spend"] == {"minor_units": 0, "currency": "USD"}
    assert payload["postcondition"]["effective_delivery"] == "NOT_PREDICTED"
    assert payload["workspace_profile_ref"].startswith("opaque:")
    assert payload["account_ref"].startswith("opaque:")
    assert payload["campaign_ref"].startswith("opaque:")
    assert payload["login_customer_client_ref"].startswith("opaque:")
    for excluded in (
        "action",
        "action_id",
        "principal_ref",
        "credential_binding_ref",
        "configuration_digest",
        "profile_configuration_digest",
        "required_scope",
    ):
        assert excluded not in payload
    assert google_rows[0]["record_digest"] == retained["record_digest"]

    exported = b"".join(path.read_bytes() for path in output.iterdir() if path.is_file())
    for raw in (
        b"workspace-private-marker",
        b"google_ads.customer.1234567890",
        b"1234567890",
        b"google_ads.campaign.1234567890.42",
        b"fixture-google-ads.apps.googleusercontent.com",
        b"google-ads-edit-private-marker",
        b"0987654321",
        action.action_id.encode("ascii"),
        b"credential-binding:sha256:",
        b"google_oidc_principal_v1.",
        b'"configuration_digest"',
        b'"profile_configuration_digest"',
        b'"required_scope"',
    ):
        assert raw not in exported
