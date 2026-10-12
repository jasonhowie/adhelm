"""Configured planning through the real pending/pipe boundary with fake native I/O."""

from __future__ import annotations

import base64
import hashlib
import selectors
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from test_meta_campaign_pause_approval_bridge_transport_v2 import (
    _Cancellation,
    _events_for_streams,
    _FakeChild,
    _FakePosixOperations,
    _frame,
)
from test_meta_pause_kernel import _GuardedMetaAdapter
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime
from test_meta_pause_private_handoff_v2_integration import _synthetic_candidate

from adhelm._installed_meta_campaign_pause_approval_bridge_v2 import (
    _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2,
    _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2,
    _prepare_installed_meta_campaign_pause_approval_bridge_v2,
)
from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    decode_meta_campaign_pause_approval_bridge_request_v2,
)
from adhelm._meta_campaign_pause_approval_bridge_transport_v2 import (
    _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2,
)
from adhelm._native_approval_bridge_transport import _META_CAMPAIGN_PAUSE_HELPER_PATH
from adhelm.approval_protocol import ApprovalHostConfiguration, decode_approval_challenge
from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.safety import ApplyOutcome, EffectStatus, SafetyKernel
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _ResponseChild(_FakeChild):
    def __init__(self, on_stdin_closed: Callable[[], None]) -> None:
        super().__init__()
        self._on_stdin_closed = on_stdin_closed

    def close_stream(self, stream: str) -> None:
        was_closed = stream in self.closed
        super().close_stream(stream)
        if stream == "stdin" and not was_closed:
            self._on_stdin_closed()


class _SyntheticNativePipe(_FakePosixOperations):
    """Derive a synthetic response only from the fully written request frame.

    This models framing and a lost reply; it does not witness real native
    authentication, protected nonce consumption, or installed identity.
    """

    def __init__(
        self,
        *,
        configuration: ApprovalHostConfiguration,
        metadata_digest: str,
        clock: list[datetime],
        lost_reply: bool,
        during_exchange: Callable[[], None],
    ) -> None:
        self._configuration = configuration
        self._metadata_digest = metadata_digest
        self._clock = clock
        self._lost_reply = lost_reply
        self._during_exchange = during_exchange
        self._request_frame = bytearray()
        self.synthetic_replies = 0
        self.candidate_bytes: bytes | None = None
        super().__init__(
            stdout=b"",
            child=_ResponseChild(self._respond),
            events=[[(10, selectors.EVENT_WRITE)]],
        )

    def write(self, file_descriptor: int, data: bytes) -> int:
        written = super().write(file_descriptor, data)
        self._request_frame.extend(data)
        if len(self._request_frame) >= 4:
            expected = 4 + int.from_bytes(self._request_frame[:4], "big")
            if len(self._request_frame) < expected:
                assert self.selector is not None
                self.selector._events.append([(10, selectors.EVENT_WRITE)])
        return written

    def _respond(self) -> None:
        assert self.synthetic_replies == 0
        assert len(self._request_frame) == 4 + int.from_bytes(self._request_frame[:4], "big")
        request = decode_meta_campaign_pause_approval_bridge_request_v2(
            bytes(self._request_frame[4:]),
            configuration=self._configuration,
            expected_metadata_digest=self._metadata_digest,
        )
        request_data = parse_json(request.canonical_bytes)
        assert type(request_data) is dict
        challenge_text = request_data["canonical_challenge_b64url"]
        assert type(challenge_text) is str
        challenge = decode_approval_challenge(
            base64.urlsafe_b64decode(challenge_text + "=" * (-len(challenge_text) % 4)),
            configuration=self._configuration,
            now=self._clock[0],
        )
        started_at = self._clock[0]
        observed_at = started_at + timedelta(microseconds=(-started_at.microsecond) % 1000)
        candidate = _synthetic_candidate(
            request,
            challenge,
            self._configuration,
            self._metadata_digest,
            started_at,
            observed_at,
        )
        # Only this synthetic native stand-in serializes its own candidate.
        # Neither the test nor production exchange inspects the held pending.
        self.candidate_bytes = object.__getattribute__(
            candidate, "_NativeMetaCampaignPauseApprovalBridgeCandidateV2__canonical_bytes"
        )
        self.synthetic_replies += 1
        self._clock[0] = observed_at
        self._during_exchange()
        response = b"" if self._lost_reply else _frame(self.candidate_bytes)
        self._stdout.extend(response)
        assert self.selector is not None
        self.selector._events.extend(_events_for_streams(response, b"")[1:])


@pytest.mark.parametrize("scenario", ["normal", "history_extension", "lost_reply"])
def test_configured_plan_consumes_pending_before_real_pipe_boundary(
    runtime: _Runtime, scenario: str
) -> None:
    prepared = runtime.plan()
    record_digest = prepared["data"]["record_digest"]
    ledger = Ledger(runtime.root)
    action, _provenance = load_meta_campaign_pause_plan(ledger, record_digest)
    clock = [datetime.now(UTC)]
    kernel = SafetyKernel(runtime.root, ledger, clock=lambda: clock[0])
    configuration = ApprovalHostConfiguration(
        host_ref="host:synthetic-meta-v2-pipe",
        signing_key_ref="key:synthetic-meta-v2-pipe",
    )
    metadata_digest = hashlib.sha256(b"synthetic-held-meta-v2-pipe-metadata").hexdigest()
    reviewed = _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2(
        configuration, metadata_digest
    )
    pending = _prepare_installed_meta_campaign_pause_approval_bridge_v2(
        kernel,
        ledger,
        source_plan_record_digest=record_digest,
        reviewed_metadata=reviewed,
    )

    def extend_history() -> None:
        if scenario == "history_extension":
            runtime.plan()

    operations = _SyntheticNativePipe(
        configuration=configuration,
        metadata_digest=metadata_digest,
        clock=clock,
        lost_reply=scenario == "lost_reply",
        during_exchange=extend_history,
    )
    adapter = _make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(operations)
    before = list(ledger.records("changes"))
    if scenario == "lost_reply":
        with pytest.raises(AdhelmError) as lost:
            _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
                kernel, ledger, pending=pending, adapter=adapter, cancellation=_Cancellation()
            )
        assert (
            lost.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED"
        )
        assert lost.value.exit_code is ExitCode.AMBIGUOUS
        assert list(ledger.records("changes")) == before
        assert ledger.unresolved_starts() == []
    else:
        accepted = _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            kernel, ledger, pending=pending, adapter=adapter, cancellation=_Cancellation()
        )
        assert accepted.direct_grant is not None
        assert accepted.direct_grant.plan_digest == prepared["data"]["plan_digest"]
        guarded = _GuardedMetaAdapter(
            action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
        )
        result = kernel.execute(action.plan, accepted.direct_grant, {"meta_ads": guarded})
        assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
        assert guarded.apply_calls == 1
        assert ledger.unresolved_starts() == []
    assert operations.synthetic_replies == 1
    assert len(operations.launch_specs) == 1
    assert operations.launch_specs[0].executable == _META_CAMPAIGN_PAUSE_HELPER_PATH
    after = list(ledger.records("changes"))
    with pytest.raises(AdhelmError) as replay:
        _exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            kernel, ledger, pending=pending, adapter=adapter, cancellation=_Cancellation()
        )
    assert replay.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"
    assert len(operations.launch_specs) == 1
    assert list(ledger.records("changes")) == after
    expected_reads = ["/v26.0/debug_token", "/v26.0/act_12345", "/v26.0/987654321"]
    assert runtime.paths == expected_reads * (2 if scenario == "history_extension" else 1)
    history = b"".join(path.read_bytes() for path in runtime.root.rglob("*.jsonl"))
    assert _SUBJECT.encode() not in history and _INSPECTOR.encode() not in history
    assert b"canonical_receipt_b64url" not in history
    assert b"active_public_key_x963_b64url" not in history
