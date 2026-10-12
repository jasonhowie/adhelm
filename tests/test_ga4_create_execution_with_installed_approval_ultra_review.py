"""Independent fake-only boundaries for installed GA4 CREATE execution.

The tests use the existing synthetic bridge, OAuth, and HTTP fixtures. They do
not launch a helper or host, use a protected credential store, or contact GA4.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_ga4_create_execution_with_installed_approval import (
    _execute,
    _install_synthetic_bridge,
)
from test_ga4_create_execution_workflow import _material

from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.providers import ga4_admin_key_event_create_adapter as adapter_module
from adhelm.safety import EffectStatus
from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def _claim_count(ledger: object) -> int:
    return sum(
        record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
        for record in ledger.records("changes")  # type: ignore[union-attr]
    )


def test_same_plan_grant_with_changed_durable_identity_stops_before_oauth(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]

    def alter_grant(accepted: AcceptedNativeApproval) -> AcceptedNativeApproval:
        assert accepted.direct_grant is not None
        return replace(
            accepted,
            direct_grant=accepted.direct_grant.model_copy(
                update={"grantor_ref": "logical:unrecorded-grantor"}
            ),
        )

    bridge_adapters = _install_synthetic_bridge(
        monkeypatch,
        current=material[7],
        after_acceptance=alter_grant,
    )
    monkeypatch.setattr(
        adapter_module,
        "_configured_ga4_create_adapter",
        lambda *_a, **_k: pytest.fail("forged grant resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "USER_APPROVAL_HOST_REQUIRED"
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    assert _claim_count(ledger) == 1
    assert not scenario.calls


def test_plan_expiry_after_native_claim_blocks_oauth_and_provider(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]
    action, _ = load_ga4_key_event_create_plan(ledger, material[0].plan_record_digest)

    def expire_after_claim(_accepted: AcceptedNativeApproval) -> None:
        material[7][0] = action.plan.expires_at

    bridge_adapters = _install_synthetic_bridge(
        monkeypatch,
        current=material[7],
        after_acceptance=expire_after_claim,
    )
    monkeypatch.setattr(
        adapter_module,
        "_configured_ga4_create_adapter",
        lambda *_a, **_k: pytest.fail("expired source resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "PLAN_EXPIRED"
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    assert _claim_count(ledger) == 1
    assert not scenario.calls


def test_configuration_lock_is_released_during_native_review_wait(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    configuration, scenario = material[6], material[-1]

    def rewrite_identical_configuration(_accepted: AcceptedNativeApproval) -> None:
        save_configuration(tmp_path, configuration, replace=True)

    bridge_adapters = _install_synthetic_bridge(
        monkeypatch,
        current=material[7],
        after_acceptance=rewrite_identical_configuration,
    )
    result = _execute(tmp_path, material)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    assert scenario.posts == 1
