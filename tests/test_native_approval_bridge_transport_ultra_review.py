"""Independent fake-only adversarial review of M33 pipe handoff mechanics."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import adhelm._native_approval_bridge_transport as transport
from adhelm._native_approval_bridge_protocol import (
    NativeApprovalBridgeCandidateV1,
    NativeApprovalBridgeRequestV1,
    decode_native_approval_bridge_request_v1,
)
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode

_ROOT = Path(__file__).resolve().parents[1]
_VECTORS = (
    _ROOT
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/NativeApprovalBridge"
    / "native-approval-bridge-protocol-v1.json"
)


def _case() -> dict[str, object]:
    document = parse_json(_VECTORS.read_bytes())
    assert type(document) is dict
    assert canonical_bytes(document) == _VECTORS.read_bytes()
    cases = document["source_cases"]
    assert type(cases) is list
    result = [value for value in cases if type(value) is dict]
    assert len(result) == 2
    return result[0]


def _metadata(case: dict[str, object]) -> dict[str, object]:
    value = case["metadata_fixture"]
    assert type(value) is dict
    return value


def _configuration(case: dict[str, object]) -> ApprovalHostConfiguration:
    metadata = _metadata(case)
    fields = (
        "host_ref",
        "signing_key_ref",
        "maximum_challenge_bytes",
        "maximum_challenge_lifetime_seconds",
        "maximum_issued_at_skew_seconds",
        "maximum_subplans",
        "maximum_policy_items",
    )
    assert all(field in metadata for field in fields)
    values = {field: metadata[field] for field in fields}
    assert type(values["host_ref"]) is str
    assert type(values["signing_key_ref"]) is str
    for field in fields[2:]:
        assert type(values[field]) is int
    return ApprovalHostConfiguration(**values)  # type: ignore[arg-type]


def _metadata_digest(case: dict[str, object]) -> str:
    return hashlib.sha256(canonical_bytes(_metadata(case))).hexdigest()


def _time(value: object) -> datetime:
    assert type(value) is str
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _request(case: dict[str, object]) -> NativeApprovalBridgeRequestV1:
    raw = case["request_canonical_json"]
    assert type(raw) is str
    return decode_native_approval_bridge_request_v1(
        raw.encode("utf-8"),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
    )


class _Cancellation:
    def __init__(self) -> None:
        self.value = False
        self.calls = 0

    def cancelled(self) -> bool:
        self.calls += 1
        return self.value


class _Clock:
    def __init__(self, samples: list[datetime]) -> None:
        self._samples = samples
        self.calls = 0

    def utc_now(self) -> datetime:
        value = self._samples[self.calls]
        self.calls += 1
        return value


class _ReturnedResponse:
    def __init__(
        self, response: bytes, *, cancel_after_return: _Cancellation | None = None
    ) -> None:
        self.response = response
        self.cancel_after_return = cancel_after_return
        self.calls = 0
        self.frames: list[bytes] = []

    def exchange(self, request_frame: bytes, *, cancellation: _Cancellation) -> bytes:
        assert cancellation is self.cancel_after_return or self.cancel_after_return is None
        self.calls += 1
        self.frames.append(request_frame)
        if self.cancel_after_return is not None:
            self.cancel_after_return.value = True
        return self.response


def _exchange(
    *,
    adapter: _ReturnedResponse,
    cancellation: _Cancellation,
    samples: list[datetime],
) -> NativeApprovalBridgeCandidateV1:
    case = _case()
    return transport.exchange_native_approval_bridge_once_v1(
        _request(case),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
        adapter=adapter,
        validation_clock=_Clock(samples),
        cancellation=cancellation,
    )


def _candidate_bytes(case: dict[str, object]) -> bytes:
    raw = case["candidate_canonical_json"]
    assert type(raw) is str
    return raw.encode("utf-8")


def _assert_ambiguous(raised: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_EXCHANGE_UNVERIFIED"
    assert raised.value.exit_code == ExitCode.AMBIGUOUS
    assert "private-sentinel" not in raised.value.message.lower()
    assert "retry this exchange" in raised.value.next_action.lower()


def test_post_response_cancellation_burns_handoff_without_returning_candidate() -> None:
    case = _case()
    cancellation = _Cancellation()
    adapter = _ReturnedResponse(
        _candidate_bytes(case),
        cancel_after_return=cancellation,
    )

    with pytest.raises(AdhelmError) as raised:
        _exchange(
            adapter=adapter,
            cancellation=cancellation,
            samples=[_time(case["exchange_started_at"]), _time(case["current_now"])],
        )

    _assert_ambiguous(raised)
    assert adapter.calls == 1
    assert len(adapter.frames) == 1
    assert cancellation.calls >= 1


def test_handoff_clock_rollback_after_response_is_ambiguous() -> None:
    case = _case()
    start = _time(case["exchange_started_at"])
    current = _time(case["current_now"])
    adapter = _ReturnedResponse(_candidate_bytes(case))

    with pytest.raises(AdhelmError) as raised:
        _exchange(
            adapter=adapter,
            cancellation=_Cancellation(),
            samples=[start, current, current - timedelta(microseconds=1)],
        )

    _assert_ambiguous(raised)
    assert adapter.calls == 1


def test_handoff_expiry_after_response_is_ambiguous() -> None:
    case = _case()
    candidate = case["candidate"]
    assert type(candidate) is dict
    expires_at = candidate["expires_at"]
    adapter = _ReturnedResponse(_candidate_bytes(case))

    with pytest.raises(AdhelmError) as raised:
        _exchange(
            adapter=adapter,
            cancellation=_Cancellation(),
            samples=[
                _time(case["exchange_started_at"]),
                _time(case["current_now"]),
                _time(expires_at),
            ],
        )

    _assert_ambiguous(raised)
    assert adapter.calls == 1


def test_partial_write_overreport_cannot_skip_unsubmitted_request_bytes() -> None:
    class _OverreportingOperations:
        def write(self, _file_descriptor: int, _data: bytes) -> int:
            return transport._PIPE_CHUNK_BYTES + 1

    with pytest.raises(ValueError):
        transport._write_pending(
            _OverreportingOperations(),
            7,
            memoryview(b"x" * (transport._PIPE_CHUNK_BYTES + 1)),
        )
