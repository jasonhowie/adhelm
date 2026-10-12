"""Independent fake-only boundary tests for the fixed Meta PAUSE adapter."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never, cast

import httpx
import pytest
from test_meta_pause_kernel import _action
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime

from adhelm import meta_ads_token_edit_session as edit_session
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.providers import meta_campaign_pause_adapter as adapter_module
from adhelm.providers.meta_campaign_pause_adapter import (
    _configured_meta_campaign_pause_adapter,
)
from adhelm.providers.meta_campaign_state import MetaCampaignState
from adhelm.providers.meta_pause_contract import MetaCampaignPausePlannedAction
from adhelm.safety import DirectGrant, EffectStatus, SafetyKernel, new_direct_grant
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _ReviewApprovalHost:
    host_ref = "ultra-meta-pause-adapter-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        interaction_ref = grant.user_interaction_ref
        return isinstance(interaction_ref, str) and (
            interaction_ref == "ultra-meta-pause-adapter-interaction"
        )


class _PostReadbackHandler:
    """A closed fake Graph sequence for one already-planned PAUSE operation."""

    def __init__(
        self,
        runtime: _Runtime,
        *,
        drift_identity_after_post: bool = False,
        set_cookie_on_campaign_get: int | None = None,
    ) -> None:
        self._runtime = runtime
        self._drift_identity_after_post = drift_identity_after_post
        self._set_cookie_on_campaign_get = set_cookie_on_campaign_get
        self._expires_at = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
        self._data_access_expires_at = int((datetime.now(UTC) + timedelta(hours=2)).timestamp())
        self.post_count = 0
        self.campaign_get_count = 0
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        assert request.url.host == "graph.facebook.com"
        if request.method == "POST":
            assert request.url.path == "/v26.0/987654321/"
            assert request.url.query == b""
            assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
            assert request.content == b"status=PAUSED"
            self.post_count += 1
            assert self.post_count == 1, "the fixed PAUSE POST must not retry"
            return httpx.Response(200, request=request, content=b'{"success":true}')

        assert request.method == "GET"
        if request.url.path == "/v26.0/debug_token":
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": {
                        "app_id": "67890",
                        "user_id": "99999",
                        "is_valid": True,
                        "type": "USER",
                        "scopes": ["ads_management", "public_profile"],
                        "expires_at": self._expires_at,
                        "data_access_expires_at": self._data_access_expires_at,
                    }
                },
            )
        assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
        if request.url.path == "/v26.0/act_12345":
            currency = "EUR" if self._drift_identity_after_post and self.post_count else "USD"
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "12345",
                    "currency": currency,
                    "timezone_name": "UTC",
                    "account_status": 1,
                },
            )
        assert request.url.path == "/v26.0/987654321"
        assert request.url.params["fields"] == "id,account_id,configured_status,effective_status"
        self.campaign_get_count += 1
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "987654321",
                "account_id": "12345",
                "configured_status": "PAUSED" if self.post_count else "ACTIVE",
                "effective_status": "WITH_ISSUES",
            },
            headers=(
                {"set-cookie": "unexpected=1; Domain=graph.facebook.com; Path=/"}
                if self._set_cookie_on_campaign_get == self.campaign_get_count
                else None
            ),
        )


def _planned(runtime: _Runtime) -> tuple[Ledger, MetaCampaignPausePlannedAction]:
    receipt = runtime.plan()
    ledger = Ledger(runtime.root)
    action, _provenance = load_meta_campaign_pause_plan(ledger, receipt["data"]["record_digest"])
    return ledger, action


def _record_direct_grant(
    kernel: SafetyKernel, action: MetaCampaignPausePlannedAction
) -> DirectGrant:
    plan = action.plan
    grant = new_direct_grant(
        authorization_id="ultra-meta-pause-adapter-grant",
        plan=plan,
        grantor_ref="ultra-meta-pause-adapter-grantor",
        host_ref=_ReviewApprovalHost.host_ref,
        user_interaction_ref="ultra-meta-pause-adapter-interaction",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    kernel.record_direct_grant(plan, grant, _ReviewApprovalHost())
    return grant


@pytest.mark.parametrize(
    "content",
    [
        b'{"success": 1}',
        b'{"success": "true"}',
        b'{"success": false}',
        b'{"success": true, "extra": "x"}',
        b'{"success": true, "success": true}',
        b"[]",
        b"{",
    ],
)
def test_local_acknowledgement_rejects_aliases_duplicates_and_extra_fields(content: bytes) -> None:
    response = httpx.Response(200, content=content)

    with pytest.raises(ValueError, match="acknowledgement"):
        adapter_module.MetaCampaignPauseGuardedAdapter._require_acknowledgement(response)


@pytest.mark.parametrize(
    "ledger_root,profile_ref",
    [
        ("other-workspace", "default"),
        (".", "different-workspace-profile"),
    ],
)
def test_adapter_factory_rejects_wrong_ledger_before_edit_session_or_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_root: str,
    profile_ref: str,
) -> None:
    action = _action("default")
    directory = tmp_path
    ledger = Ledger(directory / ledger_root, profile_ref=profile_ref)

    def unexpected_session(*args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise AssertionError("wrong-workspace factory input reached session or credential activity")

    monkeypatch.setattr(
        adapter_module.edit_session,
        "configured_meta_ads_token_edit_session",
        unexpected_session,
    )

    with pytest.raises(AdhelmError) as error:
        with _configured_meta_campaign_pause_adapter(directory, action, ledger):
            pytest.fail("wrong-workspace adapter factory unexpectedly yielded")

    assert error.value.code == "META_CAMPAIGN_PAUSE_ADAPTER_INVALID"


def test_issued_post_for_a_different_campaign_is_rejected_before_provider_work(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, action = _planned(runtime)
    before_paths = list(runtime.paths)

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        session = object.__getattribute__(adapter, "_session")
        wrong_post = edit_session._issue_fixed_meta_campaign_pause_post(
            session,
            campaign_id="987654322",
            plan_digest=action.plan.plan_digest,
            subplan_digest=action.plan.subplans[0].subplan_digest,
        )
        object.__setattr__(adapter, "_post", wrong_post)

        with pytest.raises(AdhelmError) as error:
            adapter.capabilities(action.plan.subplans[0])

    assert error.value.code == "META_CAMPAIGN_PAUSE_ADAPTER_INVALID"
    # The one initial inspection/account read belongs to factory admission;
    # the mismatched issued writer may not cause an additional request.
    assert runtime.paths == before_paths + ["/v26.0/debug_token", "/v26.0/act_12345"]


def test_no_active_permit_stays_a_local_refusal_without_a_post(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, action = _planned(runtime)
    campaign = action.observation_binding.campaign
    active = MetaCampaignState(
        campaign_client_ref=campaign.campaign_client_ref,
        campaign_ref=campaign.campaign_ref,
        account_client_ref=campaign.account_client_ref,
        configured_status="ACTIVE",
        effective_status=campaign.effective_status,
    )

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        monkeypatch.setattr(
            adapter_module.MetaCampaignPauseGuardedAdapter,
            "_kernel_started",
            staticmethod(lambda *_args: True),
        )
        monkeypatch.setattr(
            adapter_module.MetaCampaignPauseGuardedAdapter,
            "_current_campaign_state",
            lambda _adapter, _action: active,
        )
        before_paths = list(runtime.paths)
        with pytest.raises(AdhelmError) as error:
            adapter.apply_guarded(
                action.plan.subplans[0],
                idempotency_key="ultra-invalid-permit",
                permit=object.__new__(adapter_module.EgressPermit),
            )

    assert error.value.code == "KERNEL_EGRESS_PERMIT_REQUIRED"
    assert runtime.paths == before_paths


def test_post_ack_identity_drift_stays_ambiguous_without_readback_claim(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, action = _planned(runtime)
    handler = _PostReadbackHandler(runtime, drift_identity_after_post=True)
    monkeypatch.setattr(runtime, "handle", handler)
    kernel = SafetyKernel(runtime.root, ledger)
    grant = _record_direct_grant(kernel, action)

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        result = kernel.execute(action.plan, grant, {"meta_ads": adapter})
        assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
        assert result.recovery_required is True
        assert adapter.verify(action.plan.subplans[0]).verified is False

    assert handler.post_count == 1
    assert not [
        row
        for row in ledger.records("changes")
        if row["record_kind"] == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION"
    ]


def test_readback_append_failure_after_ack_is_ambiguous_and_does_not_retry(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, action = _planned(runtime)
    handler = _PostReadbackHandler(runtime)
    monkeypatch.setattr(runtime, "handle", handler)
    kernel = SafetyKernel(runtime.root, ledger)
    grant = _record_direct_grant(kernel, action)
    original_append = ledger.append

    def fail_only_readback(
        stream: str,
        record_kind: str,
        payload: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        if record_kind == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION":
            raise OSError("synthetic readback append interruption")
        return cast(dict[str, object], original_append(stream, record_kind, payload, **kwargs))

    monkeypatch.setattr(ledger, "append", fail_only_readback)
    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        result = kernel.execute(action.plan, grant, {"meta_ads": adapter})
        assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
        assert result.recovery_required is True
        assert adapter.verify(action.plan.subplans[0]).verified is False

    assert handler.post_count == 1
    assert not [
        row
        for row in ledger.records("changes")
        if row["record_kind"] == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION"
    ]
    assert (
        len(
            [
                row
                for row in ledger.records("changes")
                if row["payload"].get("event_type") == "APPLY_STARTED"
            ]
        )
        == 1
    )


def test_final_baseline_cookie_blocks_the_fixed_post_after_permit_consumption(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, action = _planned(runtime)
    # Kernel preflight performs campaign GET #1; apply_guarded's final baseline
    # is GET #2. The latter must not be allowed to add an ambient Cookie to POST.
    handler = _PostReadbackHandler(runtime, set_cookie_on_campaign_get=2)
    monkeypatch.setattr(runtime, "handle", handler)
    kernel = SafetyKernel(runtime.root, ledger)
    grant = _record_direct_grant(kernel, action)

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        result = kernel.execute(action.plan, grant, {"meta_ads": adapter})
        assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
        assert result.recovery_required is True

    assert handler.campaign_get_count >= 2
    assert handler.post_count == 0
    assert not [
        row
        for row in ledger.records("changes")
        if row["record_kind"] == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION"
    ]
