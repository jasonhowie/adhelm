"""End-to-end fake composition for retained Meta PAUSE execution through V2.

The test replaces only the compiled-route static binding and fixed pipe
adapter with module-issued/fake equivalents.  It never imports a configured
wheel or starts a system helper, and Graph requests use the existing planning
fixture's ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from test_meta_pause_adapter_pipe_integration import _FixedMetaPauseHandler
from test_meta_pause_pending_pipe_integration_v2 import _SyntheticNativePipe
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime

import adhelm._compiled_meta_campaign_pause_approval_bridge_v2 as compiled
import adhelm.meta_ads_token_edit_session as edit_session
import adhelm.meta_pause_execution_workflow as workflow
from adhelm._compiled_meta_campaign_pause_approval_bridge_v2 import (
    _issue_compiled_meta_campaign_pause_approval_bridge_binding_v2_from_generated_metadata,
)
from adhelm._meta_campaign_pause_approval_bridge_transport_v2 import (
    _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2,
)
from adhelm._native_approval_bridge_transport import _META_CAMPAIGN_PAUSE_HELPER_PATH
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.safety import EffectStatus
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_METADATA_FIELD_ORDER = (
    "schema_version",
    "host_ref",
    "signing_key_ref",
    "mach_service_name",
    "client_bundle_identifier",
    "client_team_identifier",
    "host_bundle_identifier",
    "host_team_identifier",
    "signing_key_application_tag_hex",
    "signing_key_access_group",
    "key_storage",
    "provenance_service",
    "provenance_account",
    "maximum_challenge_bytes",
    "maximum_challenge_lifetime_seconds",
    "maximum_issued_at_skew_seconds",
    "maximum_subplans",
    "maximum_policy_items",
)


def _metadata() -> dict[str, object]:
    """Return a full synthetic static binding for this fake-only route test."""

    value: dict[str, object] = {
        "schema_version": 1,
        "host_ref": "host:m52-execution-integration",
        "signing_key_ref": "key:m52-execution-integration",
        "mach_service_name": "com.aptsonic.adhelm.approvalhost",
        "client_bundle_identifier": "com.aptsonic.adhelm.client",
        "client_team_identifier": "ABCDE12345",
        "host_bundle_identifier": "com.aptsonic.adhelm.host",
        "host_team_identifier": "ABCDE12345",
        "signing_key_application_tag_hex": "00616263ff",
        "signing_key_access_group": "ABCDE12345.com.aptsonic.adhelm.host",
        "key_storage": "KEYCHAIN_P256",
        "provenance_service": "com.aptsonic.adhelm.provenance",
        "provenance_account": "approval-host-v1",
        "maximum_challenge_bytes": 65_536,
        "maximum_challenge_lifetime_seconds": 300,
        "maximum_issued_at_skew_seconds": 60,
        "maximum_subplans": 6,
        "maximum_policy_items": 64,
    }
    assert tuple(value) == _METADATA_FIELD_ORDER
    return value


def _configuration(metadata: dict[str, object]) -> ApprovalHostConfiguration:
    return ApprovalHostConfiguration(
        host_ref=str(metadata["host_ref"]),
        signing_key_ref=str(metadata["signing_key_ref"]),
        maximum_challenge_bytes=int(metadata["maximum_challenge_bytes"]),
        maximum_challenge_lifetime_seconds=int(metadata["maximum_challenge_lifetime_seconds"]),
        maximum_issued_at_skew_seconds=int(metadata["maximum_issued_at_skew_seconds"]),
        maximum_subplans=int(metadata["maximum_subplans"]),
        maximum_policy_items=int(metadata["maximum_policy_items"]),
    )


def _install_synthetic_compiled_route(
    monkeypatch: pytest.MonkeyPatch, *, clock: list[datetime]
) -> _SyntheticNativePipe:
    """Install a module-issued V2 binding and a synthetic framed pipe only."""

    metadata = _metadata()
    configuration = _configuration(metadata)
    metadata_digest = hashlib.sha256(canonical_bytes(metadata)).hexdigest()
    binding = (
        _issue_compiled_meta_campaign_pause_approval_bridge_binding_v2_from_generated_metadata(
            metadata_fields=tuple((name, metadata[name]) for name in _METADATA_FIELD_ORDER),
            expected_metadata_digest=metadata_digest,
            expected_protocol_configuration_digest=configuration.configuration_digest,
        )
    )
    operations = _SyntheticNativePipe(
        configuration=configuration,
        metadata_digest=metadata_digest,
        clock=clock,
        lost_reply=False,
        during_exchange=lambda: None,
    )
    pipe = _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(operations)
    monkeypatch.setattr(
        compiled,
        "_resolve_compiled_meta_campaign_pause_approval_bridge_binding_v2",
        lambda: binding,
    )
    monkeypatch.setattr(
        compiled,
        "_fixed_meta_campaign_pause_approval_bridge_pipe_adapter_v2",
        lambda: pipe,
    )
    return operations


def _durable_approval_rows(ledger: Ledger, plan_digest: str) -> list[dict[str, object]]:
    return [
        row
        for row in ledger.records("changes")
        if row.get("payload", {}).get("event_type") == "APPROVAL_GRANTED"
        and row["payload"].get("plan_digest") == plan_digest
    ]


@pytest.mark.parametrize(
    ("scenario", "expected_status", "recovery_required"),
    [
        ("success", EffectStatus.APPLIED_VERIFIED, False),
        ("unknown_ack", EffectStatus.AMBIGUOUS_REMOTE_STATE, True),
        ("lost_reply", EffectStatus.AMBIGUOUS_REMOTE_STATE, True),
    ],
)
def test_retained_meta_pause_execution_composes_v2_approval_and_guarded_adapter(
    runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_status: EffectStatus,
    recovery_required: bool,
) -> None:
    """A V2 grant reaches one guarded POST only after durable local acceptance."""

    handler = _FixedMetaPauseHandler(runtime, scenario=scenario)
    monkeypatch.setattr(runtime, "handle", handler)
    prepared = runtime.plan()
    record_digest = prepared["data"]["record_digest"]
    ledger = Ledger(runtime.root)
    action, _provenance = load_meta_campaign_pause_plan(ledger, record_digest)
    handler.bind_execution(
        ledger=ledger,
        plan_digest=action.plan.plan_digest,
        subplan_digest=action.plan.subplans[0].subplan_digest,
    )
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(workflow, "_now", lambda: clock[0])
    operations = _install_synthetic_compiled_route(monkeypatch, clock=clock)

    # Planning occurred before this wrapper is installed.  Any execution-time
    # session open therefore proves a durable native direct grant already
    # exists; it also catches a future workflow that opens a protected bundle
    # merely to preflight the retained plan.
    original_session = edit_session.configured_meta_ads_token_edit_session
    session_open_after_approval: list[bool] = []

    @contextmanager
    def session_after_durable_approval(*args: object, **kwargs: object) -> Iterator[object]:
        assert _durable_approval_rows(ledger, action.plan.plan_digest)
        assert operations.synthetic_replies == 1
        session_open_after_approval.append(True)
        with original_session(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(
        edit_session,
        "configured_meta_ads_token_edit_session",
        session_after_durable_approval,
    )

    result = workflow.execute_retained_meta_campaign_pause_with_installed_approval(
        runtime.root,
        workflow.MetaCampaignPauseExecutionRequest(plan_record_digest=record_digest),
    )

    assert len(result.results) == 1
    assert result.results[0].status is expected_status
    assert result.recovery_required is recovery_required
    assert session_open_after_approval == [True]
    assert _durable_approval_rows(ledger, action.plan.plan_digest)
    assert operations.synthetic_replies == 1
    assert len(operations.launch_specs) == 1
    assert operations.launch_specs[0].executable == _META_CAMPAIGN_PAUSE_HELPER_PATH
    assert handler.post_count == 1
    assert handler.post_saw_durable_start is True

    if recovery_required:
        assert ledger.unresolved_starts()
        with pytest.raises(AdhelmError) as replay:
            workflow.execute_retained_meta_campaign_pause_with_installed_approval(
                runtime.root,
                workflow.MetaCampaignPauseExecutionRequest(plan_record_digest=record_digest),
            )
        assert replay.value.code == "RECOVERY_REQUIRED"
        assert operations.synthetic_replies == 1
        assert handler.post_count == 1
    else:
        assert ledger.unresolved_starts() == []

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
