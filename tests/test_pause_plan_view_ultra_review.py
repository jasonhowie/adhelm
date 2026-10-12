"""Adversarial checks for the local, redacted OpenAI pause-plan review."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from datetime import timedelta

import pytest
from test_openai_pause import NOW, _action, _credentials, _Scenario

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.plan_views import preview_openai_pause
from adhelm.providers.openai_pause_contract import (
    OpenAIPauseBinding,
    OpenAIPausePlannedAction,
    _build_plan,
)

BindingFactory = Callable[[OpenAIPausePlannedAction], OpenAIPauseBinding]


def _rebuilt_constructed_action(
    action: OpenAIPausePlannedAction, binding: OpenAIPauseBinding
) -> OpenAIPausePlannedAction:
    """Build a self-consistent model-constructed action without provider access."""
    return OpenAIPausePlannedAction.model_construct(
        binding=binding,
        plan=_build_plan(binding, expires_at=action.plan.expires_at),
    )


@pytest.mark.parametrize(
    "binding_factory",
    (
        pytest.param(
            lambda action: action.binding.model_copy(update={"account_timezone": "not/a/timezone"}),
            id="non_iana_timezone",
        ),
        pytest.param(
            lambda action: action.binding.model_copy(
                update={
                    "campaign": action.binding.campaign.model_copy(
                        update={"bidding_type": "unreviewed_bid_mode"}
                    )
                }
            ),
            id="unsupported_bid_mode",
        ),
        pytest.param(
            lambda action: action.binding.model_copy(
                update={
                    "campaign": action.binding.campaign.model_copy(
                        update={"campaign_client_ref": "campaign/with/slash"}
                    )
                }
            ),
            id="invalid_native_campaign_id",
        ),
    ),
)
def test_constructed_source_bypasses_fail_before_redaction_or_transport(
    binding_factory: BindingFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _Scenario()
    action, _ = _action(scenario)
    forged = _rebuilt_constructed_action(action, binding_factory(action))
    reads = list(scenario.requests)

    def unexpected_redaction(*_: object) -> str:
        pytest.fail("Invalid plans must fail before using the local sharing key")

    monkeypatch.setattr("adhelm.plan_views.opaque_ref", unexpected_redaction)
    with pytest.raises(AdhelmError) as error:
        preview_openai_pause(forged, opaque_key=b"v" * 32, now=NOW)
    assert error.value.code == "INVALID_PLAN"
    assert scenario.requests == reads and scenario.posts == 0


def test_malformed_constructed_nested_value_cannot_emit_its_representation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _PrivateNestedValue:
        __slots__ = ()

        def __repr__(self) -> str:
            return "private-repr-sentinel-4e3ab6e334db"

    scenario = _Scenario()
    action, _ = _action(scenario)
    forged_subplan = action.plan.subplans[0].model_copy(update={"max_spend": _PrivateNestedValue()})
    forged_plan = action.plan.model_copy(update={"subplans": (forged_subplan,)})
    forged = OpenAIPausePlannedAction.model_construct(binding=action.binding, plan=forged_plan)
    reads = list(scenario.requests)

    def unexpected_redaction(*_: object) -> str:
        pytest.fail("Malformed plans must fail before using the local sharing key")

    monkeypatch.setattr("adhelm.plan_views.opaque_ref", unexpected_redaction)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(AdhelmError) as error:
            preview_openai_pause(forged, opaque_key=b"v" * 32, now=NOW)

    sentinel = "private-repr-sentinel-4e3ab6e334db"
    assert error.value.code == "INVALID_PLAN"
    assert sentinel not in str(error.value)
    assert all(sentinel not in str(item.message) for item in caught)
    captured = capsys.readouterr()
    assert sentinel not in captured.out and sentinel not in captured.err
    assert scenario.requests == reads and scenario.posts == 0


def test_review_preserves_non_null_schedule_and_source_spend_while_redacting_bindings() -> None:
    scenario = _Scenario()
    scenario.campaign["end_time"] = scenario.campaign["start_time"] + 3_600
    credentials = _credentials(profile="private-slot-54f3dc19", secret="private-key-54f3dc19")
    action, credentials = _action(scenario, credentials)
    reads = list(scenario.requests)

    review = preview_openai_pause(action, opaque_key=b"v" * 32, now=NOW)

    campaign = action.binding.campaign
    assert review.before.bidding_type == review.after.bidding_type == campaign.bidding_type
    assert review.before.lifetime_spend_limit_micros == campaign.lifetime_spend_limit_micros
    assert review.after.lifetime_spend_limit_micros == campaign.lifetime_spend_limit_micros
    assert review.before.start_at == review.after.start_at == campaign.start_at
    assert review.before.end_at == review.after.end_at == campaign.end_at
    assert (
        review.maximum_incremental_spend.model_dump()
        == action.plan.subplans[0].max_spend.model_dump()
    )
    assert review.expires_at == action.plan.expires_at
    assert review.expired_at_review is False
    assert review.review_authorizes_execution is False
    assert review.capability_status == "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    assert scenario.requests == reads and scenario.posts == 0

    serialized = canonical_bytes(review)
    for private_value in (
        action.binding.account_ref,
        action.binding.account_client_ref,
        action.binding.profile_ref,
        action.binding.credential_binding_ref,
        campaign.campaign_ref,
        campaign.campaign_client_ref,
        credentials.secret,
    ):
        assert private_value.encode() not in serialized
    assert credentials.binding_key not in serialized


def test_review_marks_expiry_at_the_exact_instant_without_rewriting_the_plan() -> None:
    action, _ = _action(_Scenario())
    review_time = action.plan.expires_at - timedelta(microseconds=1)
    current = preview_openai_pause(action, opaque_key=b"v" * 32, now=review_time)
    expired = preview_openai_pause(action, opaque_key=b"v" * 32, now=action.plan.expires_at)

    assert current.expired_at_review is False
    assert expired.expired_at_review is True
    assert current.plan_digest == expired.plan_digest == action.plan.plan_digest
    assert current.subplan_digests == expired.subplan_digests == action.plan.subplan_digests
    assert current.expires_at == expired.expires_at == action.plan.expires_at
