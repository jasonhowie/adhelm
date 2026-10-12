"""Configured Meta PAUSE from retained planning through the synthetic V2 pipe.

The pipe derives a receipt from the complete framed request using test-only
keys.  The Meta HTTP boundary is an ``httpx.MockTransport`` with fixed,
nonsecret responses.  Neither stands in for a configured native host or a
real provider call.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_meta_campaign_pause_approval_bridge_transport_v2 import _Cancellation
from test_meta_pause_pending_pipe_integration_v2 import _SyntheticNativePipe
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime

from adhelm._installed_meta_campaign_pause_approval_bridge_v2 import (
    _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2,
    _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2,
    _prepare_installed_meta_campaign_pause_approval_bridge_v2,
)
from adhelm._meta_campaign_pause_approval_bridge_transport_v2 import (
    _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2,
)
from adhelm._native_approval_bridge_transport import _META_CAMPAIGN_PAUSE_HELPER_PATH
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.providers.meta_campaign_pause_adapter import _configured_meta_campaign_pause_adapter
from adhelm.safety import EffectStatus, SafetyKernel
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _FixedMetaPauseHandler:
    """Closed fake Graph subset with stable inspection expiry facts.

    It checks that the sole fixed POST happens only after the kernel's durable
    start.  Its raw marker must never reach local history.
    """

    def __init__(self, runtime: _Runtime, *, scenario: str) -> None:
        self._runtime = runtime
        self._scenario = scenario
        now = datetime.now(UTC).replace(microsecond=0)
        self._expires_at = int((now + timedelta(hours=1)).timestamp())
        self._data_access_expires_at = int((now + timedelta(hours=2)).timestamp())
        self._ledger: Ledger | None = None
        self._plan_digest: str | None = None
        self._subplan_digest: str | None = None
        self.configured_status = "ACTIVE"
        self.post_count = 0
        self.post_saw_durable_start = False
        self.requests: list[tuple[str, str]] = []

    def bind_execution(self, *, ledger: Ledger, plan_digest: str, subplan_digest: str) -> None:
        self._ledger = ledger
        self._plan_digest = plan_digest
        self._subplan_digest = subplan_digest

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "graph.facebook.com"
        self.requests.append((request.method, request.url.path))
        if request.method == "POST":
            return self._post(request)
        assert request.method == "GET"
        return self._get(request)

    def _get(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self._runtime.paths.append(path)
        if path == "/v26.0/debug_token":
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
        assert "input_token" not in request.url.params
        if path == "/v26.0/act_12345":
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "12345",
                    "currency": "USD",
                    "timezone_name": "UTC",
                    "account_status": 1,
                },
            )
        assert path == "/v26.0/987654321"
        assert request.url.params["fields"] == "id,account_id,configured_status,effective_status"
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "987654321",
                "account_id": "12345",
                "configured_status": self.configured_status,
                "effective_status": "WITH_ISSUES",
            },
        )

    def _post(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v26.0/987654321/"
        assert request.url.query == b""
        assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
        assert request.content == b"status=PAUSED"
        self.post_count += 1
        assert self.post_count == 1, "the fixed mutation transport may not retry"
        assert self._ledger is not None
        assert self._plan_digest is not None
        assert self._subplan_digest is not None
        starts = [
            row
            for row in self._ledger.records("changes")
            if row.get("payload", {}).get("event_type") == "APPLY_STARTED"
            and row["payload"].get("plan_digest") == self._plan_digest
            and row["payload"].get("subplan_digest") == self._subplan_digest
        ]
        assert len(starts) == 1
        self.post_saw_durable_start = True
        # Both non-confirmed cases deliberately leave a target-state GET.  A
        # later matching PAUSED state cannot attribute the write to this attempt.
        self.configured_status = "PAUSED"
        if self._scenario == "success":
            return httpx.Response(200, request=request, json={"success": True})
        if self._scenario == "unknown_ack":
            return httpx.Response(
                200,
                request=request,
                json={"success": True, "raw_provider_marker": "do-not-persist"},
            )
        assert self._scenario == "lost_reply"
        raise httpx.ReadError("raw-provider-marker: lost reply", request=request)


def _accepted_pending(
    *,
    ledger: Ledger,
    kernel: SafetyKernel,
    record_digest: str,
    clock: list[datetime],
) -> tuple[object, object, _SyntheticNativePipe, object]:
    """Perform the existing synthetic native pending exchange once."""

    configuration = ApprovalHostConfiguration(
        host_ref="host:synthetic-meta-pause-adapter-pipe",
        signing_key_ref="key:synthetic-meta-pause-adapter-pipe",
    )
    metadata_digest = hashlib.sha256(b"synthetic-meta-pause-adapter-pipe-metadata").hexdigest()
    reviewed = _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2(
        configuration, metadata_digest
    )
    pending = _prepare_installed_meta_campaign_pause_approval_bridge_v2(
        kernel,
        ledger,
        source_plan_record_digest=record_digest,
        reviewed_metadata=reviewed,
    )
    operations = _SyntheticNativePipe(
        configuration=configuration,
        metadata_digest=metadata_digest,
        clock=clock,
        lost_reply=False,
        during_exchange=lambda: None,
    )
    pipe = _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(operations)
    accepted = _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
        kernel, ledger, pending=pending, adapter=pipe, cancellation=_Cancellation()
    )
    return accepted, pending, operations, pipe


@pytest.mark.parametrize(
    ("scenario", "expected_status", "unresolved"),
    [
        ("success", EffectStatus.APPLIED_VERIFIED, False),
        ("unknown_ack", EffectStatus.AMBIGUOUS_REMOTE_STATE, True),
        ("lost_reply", EffectStatus.AMBIGUOUS_REMOTE_STATE, True),
    ],
)
def test_retained_meta_pause_pipe_approval_executes_one_fixed_guarded_post(
    runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_status: EffectStatus,
    unresolved: bool,
) -> None:
    handler = _FixedMetaPauseHandler(runtime, scenario=scenario)
    monkeypatch.setattr(runtime, "handle", handler)
    prepared = runtime.plan()
    record_digest = prepared["data"]["record_digest"]
    ledger = Ledger(runtime.root)
    action, _provenance = load_meta_campaign_pause_plan(ledger, record_digest)
    clock = [datetime.now(UTC)]
    kernel = SafetyKernel(runtime.root, ledger, clock=lambda: clock[0])
    handler.bind_execution(
        ledger=ledger,
        plan_digest=action.plan.plan_digest,
        subplan_digest=action.plan.subplans[0].subplan_digest,
    )

    accepted, pending, operations, pipe = _accepted_pending(
        ledger=ledger,
        kernel=kernel,
        record_digest=record_digest,
        clock=clock,
    )
    direct_grant = accepted.direct_grant
    assert direct_grant is not None
    assert direct_grant.plan_digest == action.plan.plan_digest

    with _configured_meta_campaign_pause_adapter(runtime.root, action, ledger) as adapter:
        result = kernel.execute(action.plan, direct_grant, {"meta_ads": adapter})
        assert result.results[0].status is expected_status
        assert result.recovery_required is unresolved
        if unresolved:
            reconciled = kernel.reconcile_run(action.plan, direct_grant, {"meta_ads": adapter})
            assert reconciled.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
            assert reconciled.recovery_required is True
            with pytest.raises(AdhelmError) as replayed_execution:
                kernel.execute(action.plan, direct_grant, {"meta_ads": adapter})
            assert replayed_execution.value.code == "UNRESOLVED_APPLY"

    assert handler.post_count == 1
    assert handler.post_saw_durable_start is True
    assert [item for item in handler.requests if item[0] == "POST"] == [
        ("POST", "/v26.0/987654321/")
    ]
    assert operations.synthetic_replies == 1
    assert len(operations.launch_specs) == 1
    assert operations.launch_specs[0].executable == _META_CAMPAIGN_PAUSE_HELPER_PATH

    with pytest.raises(AdhelmError) as repeated_pending:
        _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            kernel, ledger, pending=pending, adapter=pipe, cancellation=_Cancellation()
        )
    assert (
        repeated_pending.value.code
        == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"
    )
    assert operations.synthetic_replies == 1
    assert handler.post_count == 1

    changes = list(ledger.records("changes"))
    starts = [row for row in changes if row["payload"].get("event_type") == "APPLY_STARTED"]
    assert len(starts) == 1
    assert starts[0]["payload"]["plan_digest"] == action.plan.plan_digest
    assert bool(ledger.unresolved_starts()) is unresolved
    history = b"".join(path.read_bytes() for path in runtime.root.rglob("*.jsonl"))
    for secret_or_raw_marker in (
        _SUBJECT.encode(),
        _INSPECTOR.encode(),
        b"raw_provider_marker",
        b"raw-provider-marker",
        b"canonical_receipt_b64url",
        b"active_public_key_x963_b64url",
    ):
        assert secret_or_raw_marker not in history
