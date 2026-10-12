"""Fake-only tests for the fixed guarded Meta campaign PAUSED adapter."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime

from adhelm import _meta_ads_token_session as shared_session
from adhelm import meta_ads_token_edit_session as edit_session
from adhelm.approval_acceptance import PinnedApprovalTrust, accept_approval_receipt
from adhelm.approval_protocol import (
    ApprovalHostConfiguration,
    build_meta_campaign_pause_challenge,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.config import load_configuration
from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.providers import meta_campaign_pause_adapter as pause_adapter
from adhelm.providers.meta_campaign_pause_adapter import _configured_meta_campaign_pause_adapter
from adhelm.safety import EffectStatus, SafetyKernel
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _FixedMetaPauseHandler:
    """Closed fake Graph surface with an exact fixed PAUSED form route."""

    def __init__(
        self,
        *,
        acknowledgement_status: int = 200,
        acknowledgement_body: bytes = b'{"success":true}',
        account_status_after_post: int | None = None,
        inspection_scope: str = "ads_management",
        set_cookie_on_campaign_get: int | None = None,
    ) -> None:
        self._acknowledgement_status = acknowledgement_status
        self._acknowledgement_body = acknowledgement_body
        self._account_status_after_post = account_status_after_post
        self._inspection_scope = inspection_scope
        self._set_cookie_on_campaign_get = set_cookie_on_campaign_get
        self._account_status = 1
        self.configured_status = "ACTIVE"
        self.campaign_get_count = 0
        self.post_count = 0
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "graph.facebook.com"
        self.requests.append((request.method, request.url.path))
        if request.method == "POST":
            return self._post(request)
        assert request.method == "GET"
        return self._get(request)

    def _get(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v26.0/debug_token":
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            now = datetime.now(UTC)
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": {
                        "app_id": "67890",
                        "user_id": "99999",
                        "is_valid": True,
                        "type": "USER",
                        "scopes": [self._inspection_scope, "public_profile"],
                        "expires_at": int((now + timedelta(hours=1)).timestamp()),
                        "data_access_expires_at": int((now + timedelta(hours=2)).timestamp()),
                    }
                },
            )
        assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
        assert "input_token" not in request.url.params
        if path == "/v26.0/act_12345":
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "12345",
                    "currency": "USD",
                    "timezone_name": "UTC",
                    "account_status": self._account_status,
                },
            )
        assert path == "/v26.0/987654321"
        assert request.url.params["fields"] == "id,account_id,configured_status,effective_status"
        self.campaign_get_count += 1
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "987654321",
                "account_id": "12345",
                "configured_status": self.configured_status,
                "effective_status": "WITH_ISSUES",
            },
            headers=(
                {"set-cookie": "unexpected=1; Domain=graph.facebook.com; Path=/"}
                if self._set_cookie_on_campaign_get == self.campaign_get_count
                else None
            ),
        )

    def _post(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v26.0/987654321/"
        assert request.url.query == b""
        assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
        assert request.content == b"status=PAUSED"
        self.post_count += 1
        assert self.post_count == 1, "the fixed Meta mutation may not retry"
        self.configured_status = "PAUSED"
        if self._account_status_after_post is not None:
            self._account_status = self._account_status_after_post
        return httpx.Response(
            self._acknowledgement_status,
            request=request,
            content=self._acknowledgement_body,
            headers={"content-type": "application/json"},
        )


def _approved_action(
    runtime: _Runtime,
) -> tuple[Ledger, object, str]:
    receipt = runtime.plan()
    record_digest = receipt["data"]["record_digest"]
    ledger = Ledger(runtime.root)
    action, _provenance = load_meta_campaign_pause_plan(ledger, record_digest)
    return ledger, action, record_digest


def _direct_grant(
    runtime: _Runtime,
    ledger: Ledger,
    action: object,
    record_digest: str,
) -> tuple[SafetyKernel, object]:
    """Create a local synthetic receipt solely to reach the guarded adapter."""

    now = datetime.now(UTC)
    receipt_at = now + timedelta(microseconds=(-now.microsecond) % 1000)
    configuration = ApprovalHostConfiguration(
        host_ref="host:synthetic-meta-pause-adapter",
        signing_key_ref="approval-key:synthetic-meta-pause-adapter",
    )
    challenge = build_meta_campaign_pause_challenge(
        action,
        grantor_ref="synthetic-local-grantor",
        configuration=configuration,
        nonce=bytes(range(32)),
        issued_at=now,
        now=now,
        expires_at=now + timedelta(seconds=90),
    )
    key = ec.generate_private_key(ec.SECP256R1())
    signing = receipt_signing_payload(
        challenge,
        configuration=configuration,
        receipt_id="approval-receipt:" + "A" * 43,
        issued_at=receipt_at,
    )
    receipt = parse_json(signing.canonical_unsigned_receipt)
    assert type(receipt) is dict
    receipt["signature"] = (
        base64.urlsafe_b64encode(
            key.sign(signing.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
        )
        .decode("ascii")
        .rstrip("=")
    )
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    kernel = SafetyKernel(runtime.root, ledger, clock=lambda: receipt_at)
    accepted = accept_approval_receipt(
        kernel,
        ledger,
        receipt=canonical_bytes(receipt),
        expected_challenge=challenge,
        trust=PinnedApprovalTrust(configuration, public_key, ledger.profile_ref),
        source_plan_record_digest=record_digest,
    )
    assert accepted.direct_grant is not None
    return kernel, accepted.direct_grant


def _prepared_execution(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, handler: _FixedMetaPauseHandler
) -> tuple[Ledger, object, object, str]:
    ledger, action, record_digest = _approved_action(runtime)
    monkeypatch.setattr(runtime, "handle", handler)
    kernel, grant = _direct_grant(runtime, ledger, action, record_digest)
    return ledger, action, kernel, grant


def test_fixed_adapter_confirms_only_exact_ack_and_retains_readback(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _FixedMetaPauseHandler()
    ledger, action, kernel, grant = _prepared_execution(runtime, monkeypatch, handler)

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        result = kernel.execute(action.plan, grant, {"meta_ads": adapter})

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert result.recovery_required is False
    assert handler.post_count == 1
    assert [item for item in handler.requests if item[0] == "POST"] == [
        ("POST", "/v26.0/987654321/")
    ]
    readbacks = [
        row
        for row in ledger.records("changes")
        if row["record_kind"] == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION"
    ]
    assert len(readbacks) == 1
    readback = readbacks[0]
    assert readback["payload"]["configured_status"] == "PAUSED"
    assert readback["payload"]["effective_status"] == "WITH_ISSUES"
    assert readback["payload"]["acknowledgement_semantics"] == (
        "LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"
    )
    assert f"evidence:sha256:{readback['record_digest']}" in result.results[0].evidence_refs
    history = b"".join(path.read_bytes() for path in runtime.root.rglob("*.jsonl"))
    assert _SUBJECT.encode() not in history
    assert _INSPECTOR.encode() not in history


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (201, b'{"success":true}'),
        (200, b'{"success":false}'),
        (200, b'{"success":true,"unexpected":false}'),
        (200, b'{"success":true,"success":true}'),
    ],
)
def test_nonexact_acknowledgements_are_ambiguous_and_never_retried(
    runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
) -> None:
    handler = _FixedMetaPauseHandler(acknowledgement_status=status, acknowledgement_body=body)
    ledger, action, kernel, grant = _prepared_execution(runtime, monkeypatch, handler)

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        result = kernel.execute(action.plan, grant, {"meta_ads": adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.recovery_required is True
    assert handler.post_count == 1
    assert not any(
        row["record_kind"] == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION"
        for row in ledger.records("changes")
    )
    assert ledger.unresolved_starts()


def test_post_ack_account_drift_stays_ambiguous_without_readback_record(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _FixedMetaPauseHandler(account_status_after_post=2)
    ledger, action, kernel, grant = _prepared_execution(runtime, monkeypatch, handler)

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        result = kernel.execute(action.plan, grant, {"meta_ads": adapter})

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert handler.post_count == 1
    assert not any(
        row["record_kind"] == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION"
        for row in ledger.records("changes")
    )


def test_read_core_refuses_fixed_pause_before_another_http_request(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _FixedMetaPauseHandler(inspection_scope="ads_read")
    monkeypatch.setattr(runtime, "handle", handler)
    enroll_meta_token_profile(
        runtime.root,
        MetaAccountTokenProfileDraft.model_validate(
            {
                "profile_id": "meta-reader",
                "account_ref": "12345",
                "client_application_ref": "67890",
                "purpose": "READ",
                "currency": "USD",
                "timezone": "UTC",
                "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
            }
        ),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    expected_configuration_digest = digest(load_configuration(runtime.root))

    with shared_session._configured_meta_ads_token_read_state(
        runtime.root,
        "meta-reader",
        expected_configuration_digest=expected_configuration_digest,
    ) as core:
        requests_before = len(handler.requests)
        with pytest.raises(AdhelmError) as refused:
            core._post_fixed_meta_campaign_pause("987654321")

    assert refused.value.code == "META_TOKEN_EDIT_PROFILE_REQUIRED"
    assert len(handler.requests) == requests_before
    assert handler.post_count == 0


def test_adapter_rejects_another_valid_capability_before_post(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _FixedMetaPauseHandler()
    ledger, action, _kernel, _grant = _prepared_execution(runtime, monkeypatch, handler)

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        other_post = edit_session._issue_fixed_meta_campaign_pause_post(
            adapter._session,
            campaign_id="987654322",
            plan_digest=action.plan.plan_digest,
            subplan_digest=action.plan.subplans[0].subplan_digest,
        )
        object.__setattr__(adapter, "_post", other_post)
        with pytest.raises(AdhelmError) as refused:
            adapter.capabilities(action.plan.subplans[0])

    assert refused.value.code == "META_CAMPAIGN_PAUSE_ADAPTER_INVALID"
    assert handler.post_count == 0


@pytest.mark.parametrize("permit", [object.__new__(EgressPermit), object()])
def test_invalid_permit_refuses_before_fixed_post(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, permit: object
) -> None:
    handler = _FixedMetaPauseHandler()
    ledger, action, _kernel, _grant = _prepared_execution(runtime, monkeypatch, handler)
    monkeypatch.setattr(
        pause_adapter.MetaCampaignPauseGuardedAdapter,
        "_kernel_started",
        staticmethod(lambda _action, _plan, _ledger: True),
    )

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        with pytest.raises(AdhelmError) as refused:
            adapter.apply_guarded(
                action.plan.subplans[0],
                idempotency_key="attempt:invalid-permit",
                permit=permit,
            )

    assert refused.value.code == "KERNEL_EGRESS_PERMIT_REQUIRED"
    assert handler.post_count == 0


def test_final_baseline_cookie_mutation_stops_post_after_permit_consumption(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first campaign GET is the kernel preflight. The second is the
    # adapter's final baseline immediately before its fixed POST handoff.
    handler = _FixedMetaPauseHandler(set_cookie_on_campaign_get=2)
    ledger, action, kernel, grant = _prepared_execution(runtime, monkeypatch, handler)

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        result = kernel.execute(action.plan, grant, {"meta_ads": adapter})

    assert handler.campaign_get_count == 2
    assert handler.post_count == 0
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.recovery_required is True
