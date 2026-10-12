"""Plan review exposes critical facts without authority, identifiers or fresh reads."""

from datetime import datetime, timedelta

import pytest
from test_openai_pause import NOW, _action, _Scenario

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.plan_views import preview_openai_pause


def test_review_preserves_exact_plan_and_changed_and_unchanged_facts() -> None:
    scenario = _Scenario()
    action, credentials = _action(scenario)
    reads = list(scenario.requests)
    review = preview_openai_pause(action, opaque_key=b"v" * 32, now=NOW)
    assert review.plan_digest == action.plan.plan_digest
    assert review.subplan_digests == action.plan.subplan_digests
    assert review.before.status == "active" and review.after.status == "paused"
    before, after = review.before.model_dump(), review.after.model_dump()
    before.pop("status")
    after.pop("status")
    assert before == after
    assert (
        review.before.lifetime_spend_limit_micros
        == action.binding.campaign.lifetime_spend_limit_micros
    )
    assert review.maximum_incremental_spend.minor_units == 0
    assert review.maximum_incremental_spend.currency == action.binding.account_currency
    assert review.expires_at == action.plan.expires_at
    assert review.review_authorizes_execution is False
    assert review.ambiguous_attempt == "UNRESOLVED_NO_BLIND_RETRY"
    assert review.residual_delivery_or_billing == "MAY_CONTINUE_AFTER_PAUSE"
    assert review.provider_conditional_write == review.provider_idempotency == "NOT_DOCUMENTED"
    assert scenario.requests == reads and scenario.posts == 0
    serialized = canonical_bytes(review)
    for forbidden in (
        action.binding.account_client_ref,
        action.binding.campaign.campaign_client_ref,
        credentials.secret,
        action.binding.credential_binding_ref,
    ):
        assert forbidden.encode() not in serialized
    assert credentials.binding_key not in serialized
    assert review.account_ref.startswith("opaque:")
    assert review.campaign_ref.startswith("opaque:")
    assert review.profile_ref.startswith("opaque:")


def test_review_is_deterministic_and_expired_plans_remain_inspectable() -> None:
    action, _ = _action(_Scenario())
    first = preview_openai_pause(action, opaque_key=b"v" * 32, now=NOW)
    second = preview_openai_pause(action, opaque_key=b"v" * 32, now=NOW)
    assert first == second and first.review_digest == second.review_digest
    assert first.expired_at_review is False
    expired = preview_openai_pause(action, opaque_key=b"v" * 32, now=action.plan.expires_at)
    assert expired.expired_at_review is True
    assert expired.plan_digest == first.plan_digest
    assert expired.review_authorizes_execution is False


def test_changing_sharing_key_does_not_change_the_plan() -> None:
    action, _ = _action(_Scenario())
    first = preview_openai_pause(action, opaque_key=b"v" * 32, now=NOW)
    second = preview_openai_pause(action, opaque_key=b"w" * 32, now=NOW)
    assert first.account_ref != second.account_ref
    assert first.campaign_ref != second.campaign_ref
    assert first.profile_ref != second.profile_ref
    assert first.plan_digest == second.plan_digest
    assert first.before == second.before and first.after == second.after


@pytest.mark.parametrize("key", [b"", b"v" * 31, "v" * 32, None])
def test_invalid_key_fails_without_reads(key: object) -> None:
    scenario = _Scenario()
    action, _ = _action(scenario)
    reads = list(scenario.requests)
    with pytest.raises(AdhelmError) as error:
        preview_openai_pause(action, opaque_key=key, now=NOW)
    assert error.value.code == "EXPORT_KEY_REQUIRED"
    assert scenario.requests == reads


@pytest.mark.parametrize("now", [datetime(2030, 1, 1), "2030-01-01", None])
def test_invalid_time_fails_without_reads(now: object) -> None:
    scenario = _Scenario()
    action, _ = _action(scenario)
    reads = list(scenario.requests)
    with pytest.raises(AdhelmError) as error:
        preview_openai_pause(action, opaque_key=b"v" * 32, now=now)
    assert error.value.code == "CLOCK_INVALID"
    assert scenario.requests == reads


def test_copied_nested_plan_cannot_bypass_reconstruction() -> None:
    scenario = _Scenario()
    action, _ = _action(scenario)
    forged = action.model_copy(
        update={"plan": action.plan.model_copy(update={"expires_at": NOW + timedelta(hours=3)})}
    )
    # Subplan expiry is unchanged, so this is not the exact reconstructed action.
    reads = list(scenario.requests)
    with pytest.raises(AdhelmError) as error:
        preview_openai_pause(forged, opaque_key=b"v" * 32, now=NOW)
    assert error.value.code == "INVALID_PLAN"
    assert scenario.requests == reads
