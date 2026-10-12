"""The provider-agnostic execute and attempt-recovery routes, with fakes only.

Every approval here is a real signed receipt from an ephemeral test key, a real
acceptance, a real ``SafetyKernel`` execution, and a real local ledger.  What is
fake is the *installed* part: the private bridge seam is replaced, and the
configured execution adapter is an in-memory object registered for the test.
Nothing contacts a provider, an operating-system credential store, a native
helper, or a network socket, and none of this is evidence of installed native
authority or live provider conformance.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_approval_protocol import _private_key, _public_x963, _signed_receipt
from test_execution_plan_records import approval_configuration

from adhelm import credentials as credentials_module
from adhelm import generic_execution_workflow as route

# P03 registers the one installed ``("google_ads", PAUSE)`` planner and adapter
# factory at import of ``adhelm.google_ads_pause_workflow``, which ``adhelm.cli``
# imports through the Google PAUSE CLI.  P04, P04-OA and P05 register the other
# five pairs at import of ``adhelm.plan_cli``, which ``adhelm.cli`` imports for the
# whole ``plan`` group.  Importing both here makes the installed registries
# deterministic for this module rather than dependent on which other module a
# worker happened to import first.
from adhelm import google_ads_pause_workflow as _installed_google_pause_registration  # noqa: F401
from adhelm import plan_cli as _installed_plan_registrations  # noqa: F401
from adhelm.approval_acceptance import (
    AcceptedNativeApproval,
    PinnedApprovalTrust,
    accept_attempt_recovery_receipt,
    accept_composite_execution_plan_receipt,
)
from adhelm.approval_protocol import (
    ApprovalChallenge,
    build_attempt_recovery_challenge,
    build_composite_execution_plan_challenge,
)
from adhelm.config import save_configuration
from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_plan_records import ExecutionPlanRecord
from adhelm.execution_planning_session import (
    BoundedExecutionRead,
    CompiledExecutionCandidate,
    plan_execution,
)
from adhelm.generic_execution_workflow import (
    CONFIGURED_EXECUTION_ADAPTERS,
    AttemptRecoveryRequest,
    ExecutionAdapterCapabilities,
    ExecutionPlanExecutionRequest,
    configured_execution_adapter,
    executable_execution_operations,
    execute_retained_execution_plan_with_installed_approval,
    first_execution_refusal_code,
    installed_execution_refusal_gates,
    record_retained_attempt_recovery_with_installed_approval,
    register_execution_adapter,
    unresolved_attempt_catalog,
)
from adhelm.ledger import Ledger
from adhelm.models import Configuration
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    CompositePlan,
    DirectGrant,
    DirectGrantSource,
    EffectStatus,
    EgressTarget,
    Money,
    OperationKind,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    StateRef,
    SubPlan,
    Verification,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_WORKSPACE = "default"
_PROVIDER = "google_ads"
_BEFORE = "a" * 64
_AFTER = "b" * 64
_EVIDENCE = "evidence:sha256:" + "d" * 64
_LINES = ("Provider google ads operation PAUSE",)


# --- fixtures ---------------------------------------------------------------


def _now() -> datetime:
    """The real clock, truncated to the millisecond the receipt wire requires."""

    value = datetime.now(UTC)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _account() -> AccountIdentity:
    return AccountIdentity(
        account_ref=f"{_PROVIDER}.alias",
        stable_account_id=f"{_PROVIDER}.stable.1",
        auth_profile_ref=f"{_PROVIDER}.edit-profile",
        principal_ref=f"{_PROVIDER}.principal",
        client_application_ref=f"{_PROVIDER}.client",
        scopes=("ads.write",),
    )


def _candidate(**overrides: object) -> CompiledExecutionCandidate:
    values: dict[str, Any] = {
        "provider": _PROVIDER,
        "operation": OperationKind.PAUSE,
        "subplan_id": "google-ads-pause",
        "account": _account(),
        "provider_api_version": "v25",
        "capability_version": "cap-v1",
        "after": StateRef(fingerprint=_AFTER, revision="r2"),
        "max_spend": Money(minor_units=0, currency="USD"),
        "egress": (
            EgressTarget(host="googleads.googleapis.com", purpose="fixed v25 campaign pause"),
        ),
        "recovery_actions": (OperationKind.PAUSE,),
    }
    values.update(overrides)
    return CompiledExecutionCandidate(**values)


def _retain(
    directory: Path, *, plan_id: str = "generic-plan-1", **overrides: object
) -> tuple[Ledger, ExecutionPlanRecord, str]:
    """Retain one provider-neutral plan against the real local clock."""

    save_configuration(directory, Configuration(), replace=True)
    now = _now()
    read = BoundedExecutionRead(
        observed_state=StateRef(fingerprint=_BEFORE, revision="r1"),
        read_started_at=now - timedelta(seconds=4),
        read_finished_at=now - timedelta(seconds=2),
        connector_version="0.1.0.dev0",
    )
    ledger = Ledger(directory, profile_ref=_WORKSPACE)
    retained = plan_execution(
        ledger,
        _candidate(**overrides),
        read=read,
        plan_id=plan_id,
        profile_ref=_WORKSPACE,
        expires_at=now + timedelta(hours=1),
        now=now,
    )
    return ledger, retained.record, retained.record_digest


class _FakeExecutionAdapter:
    """A guarded in-memory adapter for the retained plan's exact subplan."""

    provider = _PROVIDER

    def __init__(self, plan_digest: str, *, apply_error: BaseException | None = None) -> None:
        self._plan_digest = plan_digest
        self._apply_error = apply_error
        self.state = _BEFORE
        self.revision = "r1"
        self.identity_reads = 0
        self.state_reads = 0
        self.apply_calls = 0
        self.closed = False

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider_api_version=plan.provider_api_version,
            capability_version=plan.capability_version,
            supported_operations=(plan.operation,),
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        self.identity_reads += 1
        assert account_ref == f"{_PROVIDER}.alias"
        return RemoteIdentity(
            stable_account_id=f"{_PROVIDER}.stable.1",
            auth_profile_ref=f"{_PROVIDER}.edit-profile",
            principal_ref=f"{_PROVIDER}.principal",
            client_application_ref=f"{_PROVIDER}.client",
            scopes=("ads.write",),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        del plan
        self.state_reads += 1
        return RemoteState(fingerprint=self.state, revision=self.revision)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError("PERMIT_REQUIRED", "This mutation requires the guarded seam.")

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        self.apply_calls += 1
        # The permit must be claimed: a confirmed acknowledgement that never
        # went through the guarded seam is downgraded to AMBIGUOUS_REMOTE_STATE
        # with GUARDED_EGRESS_NOT_CLAIMED.
        permit.consume(
            plan_digest=self._plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        if self._apply_error is not None:
            raise self._apply_error
        self.state, self.revision = _AFTER, "r2"
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=(_EVIDENCE,))

    def verify(self, plan: SubPlan) -> Verification:
        del plan
        return Verification(verified=True, evidence_refs=(_EVIDENCE,))

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        del plan
        return Reconciliation(
            state=RemoteState(fingerprint=self.state, revision=self.revision),
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )


class _FakeAdapterFactory:
    """The registry entry a provider execution ticket would register."""

    def __init__(self, adapter: _FakeExecutionAdapter, *, child_egress: bool = False) -> None:
        self.adapter = adapter
        self.child_egress = child_egress
        self.opened = 0
        self.declarations = 0

    def declared_capabilities(self) -> ExecutionAdapterCapabilities:
        self.declarations += 1
        return ExecutionAdapterCapabilities(child_egress=self.child_egress)

    @contextmanager
    def _scope(self) -> Iterator[_FakeExecutionAdapter]:
        self.opened += 1
        try:
            yield self.adapter
        finally:
            self.adapter.closed = True

    def open_configured_adapter(
        self,
        directory: Path,
        record: ExecutionPlanRecord,
        ledger: Ledger,
        *,
        clock: Callable[[], datetime],
    ) -> Any:
        assert isinstance(directory, Path)
        assert type(record) is ExecutionPlanRecord
        assert isinstance(ledger, Ledger)
        assert callable(clock)
        return self._scope()


def _trust(key: ec.EllipticCurvePrivateKey) -> PinnedApprovalTrust:
    return PinnedApprovalTrust(approval_configuration(), _public_x963(key), _WORKSPACE)


class _FakeCompositeBridge:
    """A stand-in for the installed bridge that runs the real acceptance.

    It builds the challenge, signs a receipt with an ephemeral test key, and
    consumes it through the public acceptance route -- exactly the private
    sequence an installed bridge owns.  ``receipt`` is retained so a test can
    replay the same one.
    """

    def __init__(
        self,
        *,
        challenge_plan: CompositePlan | None = None,
        acceptance_plan: CompositePlan | None = None,
        receipt_id: str = "approval-receipt:" + "A" * 43,
        nonce: bytes = bytes(range(32)),
        grant_override: DirectGrant | None = None,
    ) -> None:
        self.key = _private_key()
        self.calls = 0
        self.receipt: bytes | None = None
        self._challenge_plan = challenge_plan
        self._acceptance_plan = acceptance_plan
        self._receipt_id = receipt_id
        self._nonce = nonce
        self._grant_override = grant_override

    def _challenge(self, plan: CompositePlan, lines: tuple[str, ...]) -> ApprovalChallenge:
        now = _now()
        return build_composite_execution_plan_challenge(
            plan,
            grantor_ref="customer-grantor-1",
            review_lines=lines,
            expires_at=now + timedelta(minutes=4),
            configuration=approval_configuration(),
            nonce=self._nonce,
            issued_at=now,
            now=now,
        )

    def request_composite_execution_plan_approval(
        self,
        kernel: Any,
        ledger: Ledger,
        *,
        record: ExecutionPlanRecord,
        source_plan_record_digest: str,
        grantor_ref: str,
    ) -> AcceptedNativeApproval:
        self.calls += 1
        assert grantor_ref == route.INSTALLED_EXECUTION_LOGICAL_GRANTOR_REF
        assert len(source_plan_record_digest) == 64
        challenge = self._challenge(
            self._challenge_plan or record.plan,
            _LINES if self._challenge_plan is not None else record.review_lines,
        )
        if self.receipt is None:
            self.receipt = _signed_receipt(
                challenge,
                key=self.key,
                issued_at=_now(),
                receipt_id=self._receipt_id,
                configuration=approval_configuration(),
            )
        accepted = accept_composite_execution_plan_receipt(
            kernel,
            ledger,
            receipt=self.receipt,
            expected_challenge=challenge,
            trust=_trust(self.key),
            plan=self._acceptance_plan or record.plan,
        )
        if self._grant_override is not None:
            return AcceptedNativeApproval(
                receipt_claim_record_digest=accepted.receipt_claim_record_digest,
                receipt_digest=accepted.receipt_digest,
                kind=accepted.kind,
                direct_grant=self._grant_override,
                policy=None,
            )
        return accepted

    def request_attempt_recovery_approval(
        self,
        kernel: Any,
        ledger: Ledger,
        *,
        record: ExecutionPlanRecord,
        source_plan_record_digest: str,
        attempts: tuple[tuple[SubPlan, str], ...],
        grantor_ref: str,
    ) -> AcceptedNativeApproval:
        self.calls += 1
        assert grantor_ref == route.INSTALLED_RECOVERY_LOGICAL_GRANTOR_REF
        assert source_plan_record_digest == record.plan_digest
        now = _now()
        challenge = build_attempt_recovery_challenge(
            record.plan,
            attempts,
            grantor_ref="customer-grantor-1",
            review_lines=("One unresolved attempt has no journaled result",),
            expires_at=now + timedelta(minutes=4),
            configuration=approval_configuration(),
            nonce=bytes(range(32, 64)),
            issued_at=now,
            now=now,
        )
        return accept_attempt_recovery_receipt(
            kernel,
            ledger,
            receipt=_signed_receipt(
                challenge,
                key=self.key,
                issued_at=now,
                receipt_id="approval-receipt:" + "B" * 43,
                configuration=approval_configuration(),
            ),
            expected_challenge=challenge,
            trust=_trust(self.key),
            plan=record.plan,
            attempts=attempts,
        )


class _RefusingBridge:
    """Fails the test if the approval bridge is reached at all."""

    def request_composite_execution_plan_approval(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the approval bridge was reached before the adapter check")

    def request_attempt_recovery_approval(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the approval bridge was reached before the adapter check")


class _RecordingCredentialStore:
    """Records every credential-store touch and refuses it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("a credential store was reached")


def _install(monkeypatch: pytest.MonkeyPatch, bridge: object) -> None:
    monkeypatch.setattr(route, "_resolve_composite_approval_bridge", lambda: bridge)


def _no_credentials(monkeypatch: pytest.MonkeyPatch) -> _RecordingCredentialStore:
    store = _RecordingCredentialStore()
    monkeypatch.setattr(credentials_module, "_backend", store)
    monkeypatch.setattr(credentials_module, "credential", store)
    return store


def _registry(
    factory: _FakeAdapterFactory, *, operation: OperationKind = OperationKind.PAUSE
) -> dict[tuple[str, OperationKind], Any]:
    """The private seam replaces the whole registry, keyed by the exact pair."""

    return {(_PROVIDER, operation): factory}


def _claims(ledger: Ledger) -> list[dict[str, Any]]:
    return [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    ]


# --- the registry -----------------------------------------------------------


def test_exactly_the_reviewed_execution_adapters_ship() -> None:
    """The installed registry holds the reviewed pairs, and only those.

    An entry here makes ``execute plan`` reach a provider for that pair, so the
    set changing is exactly what must not happen silently.  Each pair was
    registered with its own contract, guarded adapter, and fixture evidence:
    P03 ``("google_ads", PAUSE)``
    (``tests/test_google_ads_pause_execution.py``), P04
    ``("google_ads", CREATE)`` (``tests/test_google_ads_create_execution.py``),
    P05 ``("google_ads", ACTIVATE)``
    (``tests/test_google_ads_activate_execution.py``) plus
    ``("google_ads", UPDATE)``
    (``tests/test_google_ads_maintain_execution.py``; ``ARCHIVE`` is delete-class
    and stays unregistered until rule 11's dependency proof has a cited read),
    P04-OA ``("openai_ads", CREATE)`` with a journaled child-egress adapter
    (``tests/test_openai_create_execution.py``), and P04-MA
    ``("meta_ads", CREATE)`` with the second journaled child-egress adapter
    (``tests/test_meta_create_execution.py``); every other pair is still
    unexecutable, which the ARCHIVE and DELETE cases below pin.  Meta Ads
    PAUSE deliberately stays absent here: it keeps its own compiled V2 approval
    bridge and does not run on this generic route.
    """

    from adhelm import plan_cli  # noqa: F401  (registers P04, P04-OA, P04-MA and P05)

    assert set(CONFIGURED_EXECUTION_ADAPTERS) == {
        (_PROVIDER, OperationKind.CREATE),
        (_PROVIDER, OperationKind.PAUSE),
        (_PROVIDER, OperationKind.ACTIVATE),
        (_PROVIDER, OperationKind.UPDATE),
        ("meta_ads", OperationKind.CREATE),
        ("openai_ads", OperationKind.CREATE),
    }
    assert executable_execution_operations() == (
        {"provider": _PROVIDER, "operation": "ACTIVATE"},
        {"provider": _PROVIDER, "operation": "CREATE"},
        {"provider": _PROVIDER, "operation": "PAUSE"},
        {"provider": _PROVIDER, "operation": "UPDATE"},
        {"provider": "meta_ads", "operation": "CREATE"},
        {"provider": "openai_ads", "operation": "CREATE"},
    )
    assert configured_execution_adapter(_PROVIDER, OperationKind.CREATE) is not None
    assert configured_execution_adapter(_PROVIDER, OperationKind.PAUSE) is not None
    assert configured_execution_adapter(_PROVIDER, OperationKind.ACTIVATE) is not None
    assert configured_execution_adapter("openai_ads", OperationKind.CREATE) is not None
    # Rule 11 makes DELETE the pair that must stay unregistered: it needs a
    # dependency proof no execution adapter can supply.
    assert configured_execution_adapter(_PROVIDER, OperationKind.DELETE) is None
    assert configured_execution_adapter("meta_ads", OperationKind.CREATE) is not None
    assert configured_execution_adapter(_PROVIDER, OperationKind.ARCHIVE) is None
    assert configured_execution_adapter("meta_ads", OperationKind.PAUSE) is None


def test_registration_is_deliberate_single_and_never_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry: dict[tuple[str, OperationKind], Any] = {}
    monkeypatch.setattr(route, "_CONFIGURED_EXECUTION_ADAPTERS", registry)
    monkeypatch.setattr(route, "CONFIGURED_EXECUTION_ADAPTERS", registry)
    factory = _FakeAdapterFactory(_FakeExecutionAdapter("0" * 64))

    register_execution_adapter(_PROVIDER, OperationKind.PAUSE, factory)
    assert executable_execution_operations() == ({"provider": _PROVIDER, "operation": "PAUSE"},)
    with pytest.raises(AdhelmError) as duplicate:
        register_execution_adapter(_PROVIDER, OperationKind.PAUSE, factory)
    assert duplicate.value.code == "EXECUTION_ADAPTER_ALREADY_REGISTERED"
    for provider, operation in (
        ("not_a_provider", OperationKind.PAUSE),
        (_PROVIDER, OperationKind.RECOVER),
    ):
        with pytest.raises(AdhelmError) as refused:
            register_execution_adapter(provider, operation, factory)
        assert refused.value.code == "INVALID_EXECUTION_ADAPTER_REGISTRATION"


# --- the execute route ------------------------------------------------------


def test_a_retained_plan_is_approved_executed_and_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retained plan -> receipt -> digest-bound grant -> kernel -> readback."""

    ledger, record, record_digest = _retain(tmp_path)
    adapter = _FakeExecutionAdapter(record.plan_digest)
    factory = _FakeAdapterFactory(adapter)
    bridge = _FakeCompositeBridge()
    _install(monkeypatch, bridge)

    result = execute_retained_execution_plan_with_installed_approval(
        tmp_path,
        ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
        _adapter_registry=_registry(factory),
    )

    assert result.plan_digest == record.plan_digest
    assert result.consumed is True
    assert result.recovery_required is False
    assert len(result.results) == 1
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert result.results[0].provider == _PROVIDER
    assert result.results[0].subplan_digest == record.subplan_digests[0]
    assert result.results[0].diagnostic_code is None
    # One approval, one claim, one guarded write, and the adapter closed.
    assert bridge.calls == 1
    assert len(_claims(ledger)) == 1
    assert adapter.apply_calls == 1
    assert adapter.closed is True
    assert factory.opened == 1
    # The grant the acceptance recorded binds this exact plan and subplan.
    granted = [
        item["payload"]
        for item in ledger.records("changes")
        if item["record_kind"] == "CHANGE_EVENT"
        and item["payload"].get("event_type") == "APPROVAL_GRANTED"
    ]
    assert len(granted) == 1
    assert granted[0]["plan_digest"] == record.plan_digest
    assert not ledger.unresolved_starts()
    assert unresolved_attempt_catalog(ledger)["attempts"] == []


def test_an_unregistered_pair_is_refused_before_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never burn a one-use receipt on a plan nothing can execute."""

    # DELETE is deliberately an unregistered pair: rule 11 means no execution
    # adapter ships for it, and P03/P04/P04-OA/P05 registered the other five.
    ledger, _record, record_digest = _retain(tmp_path, operation=OperationKind.DELETE)
    _install(monkeypatch, _RefusingBridge())
    store = _no_credentials(monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path, ExecutionPlanExecutionRequest(plan_record_digest=record_digest)
        )

    assert refused.value.code == "EXECUTION_ADAPTER_NOT_CONFIGURED"
    assert refused.value.exit_code is ExitCode.PRECONDITION
    assert "google_ads DELETE" in refused.value.message
    assert store.calls == 0
    assert _claims(ledger) == []


def test_an_unconfigured_build_refuses_at_the_bridge_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped bridge refuses both provider-neutral kinds, in its own order."""

    ledger, record, record_digest = _retain(tmp_path)
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))
    store = _no_credentials(monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(factory),
        )

    assert refused.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert store.calls == 0
    assert factory.opened == 0
    assert _claims(ledger) == []
    assert not ledger.unresolved_starts()


def test_a_receipt_for_another_plan_is_refused_and_consumes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, record, record_digest = _retain(tmp_path)
    other = record.plan.model_copy(update={"plan_id": "another-plan"})
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))
    _install(monkeypatch, _FakeCompositeBridge(challenge_plan=other))

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(factory),
        )

    assert refused.value.code == "APPROVAL_SOURCE_PLAN_MISMATCH"
    assert _claims(ledger) == []
    assert factory.opened == 0


def test_a_grant_for_another_plan_never_reaches_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, record, record_digest = _retain(tmp_path)
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))
    _install(
        monkeypatch,
        _FakeCompositeBridge(
            grant_override=DirectGrant(
                authorization_id="approval-receipt:" + "A" * 43,
                source=DirectGrantSource.USER_CONTROLLED_HOST,
                grantor_ref="customer-grantor-1",
                host_ref="host:adhelm-macos-approval-v1",
                user_interaction_ref="evidence:sha256:" + "e" * 64,
                plan_digest="c" * 64,
                subplan_digests=("c" * 64,),
                expires_at=_now() + timedelta(minutes=4),
            )
        ),
    )

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(factory),
        )

    assert refused.value.code == "COMPOSITE_EXECUTION_APPROVAL_REQUIRED"
    assert refused.value.exit_code is ExitCode.AUTHORIZATION
    assert factory.opened == 0
    assert not ledger.unresolved_starts()


def test_an_expired_grant_never_opens_a_credential_bearing_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, record, record_digest = _retain(tmp_path)
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))
    store = _no_credentials(monkeypatch)
    _install(
        monkeypatch,
        _FakeCompositeBridge(
            grant_override=DirectGrant(
                authorization_id="approval-receipt:" + "A" * 43,
                source=DirectGrantSource.USER_CONTROLLED_HOST,
                grantor_ref="customer-grantor-1",
                host_ref="host:adhelm-macos-approval-v1",
                user_interaction_ref="evidence:sha256:" + "e" * 64,
                plan_digest=record.plan_digest,
                subplan_digests=record.subplan_digests,
                expires_at=_now() - timedelta(seconds=1),
            )
        ),
    )

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(factory),
        )

    assert refused.value.code == "AUTHORIZATION_EXPIRED"
    assert refused.value.exit_code is ExitCode.AUTHORIZATION
    assert factory.opened == 0
    assert store.calls == 0
    assert not ledger.unresolved_starts()


def test_a_replayed_receipt_is_refused_by_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second execution reuses the first receipt; one claim survives."""

    ledger, record, record_digest = _retain(tmp_path)
    bridge = _FakeCompositeBridge()
    _install(monkeypatch, bridge)
    execute_retained_execution_plan_with_installed_approval(
        tmp_path,
        ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
        _adapter_registry=_registry(_FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))),
    )
    second = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))

    with pytest.raises(AdhelmError) as replayed:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(second),
        )

    assert replayed.value.code != "OK"
    assert len(_claims(ledger)) == 1
    assert second.opened == 0


def test_an_unretained_digest_and_a_malformed_request_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _retain(tmp_path)
    _install(monkeypatch, _RefusingBridge())

    with pytest.raises(AdhelmError) as missing:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path, ExecutionPlanExecutionRequest(plan_record_digest="f" * 64)
        )
    assert missing.value.code == "EXECUTION_PLAN_NOT_RETAINED"
    with pytest.raises(AdhelmError) as invalid:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            AttemptRecoveryRequest(started_record_digest="f" * 64),  # type: ignore[arg-type]
        )
    assert invalid.value.code == "INVALID_EXECUTION_REQUEST"


def test_an_unresolved_attempt_refuses_a_later_execution_before_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, record, record_digest = _retain(tmp_path)
    _install(monkeypatch, _FakeCompositeBridge())
    with pytest.raises(KeyboardInterrupt):
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(
                _FakeAdapterFactory(
                    _FakeExecutionAdapter(
                        record.plan_digest, apply_error=KeyboardInterrupt("process death")
                    )
                ),
                operation=record.operation,
            ),
        )
    assert ledger.unresolved_starts()

    _, second_record, second_digest = _retain(tmp_path, plan_id="generic-plan-2")
    _install(monkeypatch, _RefusingBridge())
    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=second_digest),
            _adapter_registry=_registry(
                _FakeAdapterFactory(_FakeExecutionAdapter(second_record.plan_digest))
            ),
        )
    assert refused.value.code == "RECOVERY_REQUIRED"


# --- the recovery route -----------------------------------------------------


def _interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> tuple[Ledger, ExecutionPlanRecord, str]:
    """Drive one guarded attempt that dies inside the kernel's write path."""

    ledger, record, record_digest = _retain(tmp_path, **overrides)
    _install(monkeypatch, _FakeCompositeBridge())
    with pytest.raises(KeyboardInterrupt):
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(
                _FakeAdapterFactory(
                    _FakeExecutionAdapter(
                        record.plan_digest, apply_error=KeyboardInterrupt("process death")
                    )
                ),
                operation=record.operation,
            ),
        )
    started = [item["record_digest"] for item in ledger.unresolved_starts()]
    assert len(started) == 1
    return ledger, record, str(started[0])


def test_recovering_one_attempt_records_an_unattributed_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, record, started = _interrupted(tmp_path, monkeypatch)
    adapter = _FakeExecutionAdapter(record.plan_digest)
    factory = _FakeAdapterFactory(adapter)
    _install(monkeypatch, _FakeCompositeBridge())

    outcome = record_retained_attempt_recovery_with_installed_approval(
        tmp_path,
        AttemptRecoveryRequest(started_record_digest=started),
        _adapter_registry=_registry(factory),
    )

    assert outcome.started_record_digest == started
    assert outcome.plan_digest == record.plan_digest
    assert outcome.subplan_digest == record.subplan_digests[0]
    assert outcome.provider == _PROVIDER
    assert outcome.operation == "PAUSE"
    assert outcome.actor_ref == "UNKNOWN"
    assert outcome.attempt_effect_status == "AMBIGUOUS_REMOTE_STATE"
    # The adapter's fresh read is what the record describes.
    assert outcome.observed_relation == "AT_BASELINE"
    assert outcome.disposition == "RECOVERED_AT_BASELINE"
    assert adapter.identity_reads >= 1
    assert adapter.state_reads >= 1
    assert adapter.apply_calls == 0
    assert adapter.closed is True
    # The barrier is released and nothing was attributed or settled.
    assert not ledger.unresolved_starts()
    observed = [
        item
        for item in ledger.records("changes")
        if item["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
    ]
    assert len(observed) == 1
    assert observed[0]["record_digest"] == outcome.record_digest
    assert observed[0]["payload"]["recovery_source"] == "OPERATOR_ATTESTED_OBSERVATION"
    assert unresolved_attempt_catalog(ledger)["attempts"] == []


def test_recovery_refuses_an_unknown_start_before_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _retain(tmp_path)
    _install(monkeypatch, _RefusingBridge())
    store = _no_credentials(monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        record_retained_attempt_recovery_with_installed_approval(
            tmp_path, AttemptRecoveryRequest(started_record_digest="0" * 64)
        )

    assert refused.value.code == "ATTEMPT_RECOVERY_START_NOT_FOUND"
    assert store.calls == 0


def test_recovery_refuses_an_unregistered_pair_before_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DELETE is deliberately an unregistered pair: rule 11 means no execution
    # adapter ships for it, and P03/P04/P04-OA/P05 registered the other five.
    ledger, _record, started = _interrupted(tmp_path, monkeypatch, operation=OperationKind.DELETE)
    _install(monkeypatch, _RefusingBridge())

    with pytest.raises(AdhelmError) as refused:
        record_retained_attempt_recovery_with_installed_approval(
            tmp_path, AttemptRecoveryRequest(started_record_digest=started)
        )

    assert refused.value.code == "EXECUTION_ADAPTER_NOT_CONFIGURED"
    assert ledger.unresolved_starts()


def test_an_unconfigured_build_refuses_a_recovery_at_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, record, started = _interrupted(tmp_path, monkeypatch)
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))
    # Restore the bridge this checkout actually ships: it refuses both
    # provider-neutral kinds before a helper launch or a credential.
    monkeypatch.undo()
    # `undo` also removes the release-policy substitution this module applies; this
    # test is about the bridge refusal that sits behind the gate, so lift it again.
    monkeypatch.setattr("adhelm.release_policy.require_writes_released", lambda: None)

    with pytest.raises(AdhelmError) as refused:
        record_retained_attempt_recovery_with_installed_approval(
            tmp_path,
            AttemptRecoveryRequest(started_record_digest=started),
            _adapter_registry=_registry(factory),
        )

    assert refused.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert factory.opened == 0
    assert ledger.unresolved_starts()


# --- the catalog ------------------------------------------------------------


def test_the_catalog_reports_an_unresolved_subplan_start_with_its_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, record, started = _interrupted(tmp_path, monkeypatch)

    catalog = unresolved_attempt_catalog(ledger)

    assert catalog["total_count"] == 1
    assert catalog["next_cursor"] is None
    assert catalog["blocking_effect"] == "BLOCKS_EVERY_LATER_GUARDED_WRITE"
    assert catalog["attribution"] == "NO_ACTOR_ATTRIBUTED"
    assert catalog["recovery_authorizes_retry"] is False
    row = catalog["attempts"][0]
    assert row["started_record_digest"] == started
    assert row["level"] == "SUBPLAN"
    assert row["child_index"] is None
    assert row["provider"] == _PROVIDER
    assert row["operation"] == "PAUSE"
    assert row["plan_digest"] == record.plan_digest
    assert row["refusal_code"] == "UNRESOLVED_APPLY"
    # The provider-neutral plan is retained, so `recover attempt` can act here.
    assert row["recoverable"] is True


def test_the_catalog_reports_a_journaled_child_start_as_unrecoverable(
    tmp_path: Path,
) -> None:
    """A child start of a plan no generic record retains cannot be recovered here."""

    from test_kernel_child_egress import _HierarchyAdapter, _kernel, _plan
    from test_safety import FakeApprovalHost, direct_grant

    kernel, ledger = _kernel(tmp_path)
    compiled = _plan(children=3)
    grant = direct_grant(compiled)
    kernel.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = _HierarchyAdapter(compiled, crash_at=2)
    kernel.execute(compiled, grant, {adapter.provider: adapter})

    catalog = unresolved_attempt_catalog(ledger)

    levels = {row["level"] for row in catalog["attempts"]}
    assert levels == {"SUBPLAN", "CHILD"}
    child = next(row for row in catalog["attempts"] if row["level"] == "CHILD")
    assert child["child_index"] == 2
    assert child["refusal_code"] == "UNRESOLVED_CHILD_APPLY"
    assert child["recoverable"] is False
    assert all(row["recoverable"] is False for row in catalog["attempts"])


def test_the_catalog_paginates_and_refuses_an_invalid_selection(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path, profile_ref=_WORKSPACE)

    empty = unresolved_attempt_catalog(ledger, cursor=0, limit=1)
    assert empty == {
        "attempts": [],
        "total_count": 0,
        "next_cursor": None,
        "history_verification": "HASH_CHAIN_VERIFIED_LOCAL_SCAN",
        "blocking_effect": "BLOCKS_EVERY_LATER_GUARDED_WRITE",
        "attribution": "NO_ACTOR_ATTRIBUTED",
        "recovery_command": "recover attempt",
        "recovery_authorizes_retry": False,
    }
    for cursor, limit in ((-1, 50), (0, 0), (0, 501), (1, 50)):
        with pytest.raises(AdhelmError) as refused:
            unresolved_attempt_catalog(ledger, cursor=cursor, limit=limit)
        assert refused.value.code == "INVALID_ATTEMPT_RECOVERY_SELECTION"


# --- review repairs ---------------------------------------------------------


def test_the_successor_is_executable_and_the_predecessor_is_named_in_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Supersession refuses the predecessor and leaves the successor current."""

    from adhelm.saved_execution_plans import execution_plan_catalog

    ledger, record, first = _retain(tmp_path, plan_id="plan-1")
    now = _now()
    successor = plan_execution(
        ledger,
        # A distinct plan identifier is enough to make this a different plan;
        # the predicted `after` stays the one the fake adapter reaches.
        _candidate(subplan_id="google-ads-pause-2"),
        read=BoundedExecutionRead(
            observed_state=StateRef(fingerprint=_BEFORE, revision="r1"),
            read_started_at=now - timedelta(seconds=4),
            read_finished_at=now - timedelta(seconds=2),
            connector_version="0.1.0.dev0",
        ),
        plan_id="plan-2",
        profile_ref=_WORKSPACE,
        expires_at=now + timedelta(hours=1),
        now=now,
        supersedes_record_digest=first,
    )
    catalog = execution_plan_catalog(ledger)
    rows = {row["record_digest"]: row for row in catalog["plans"]}
    assert rows[first]["superseded_by"] == successor.record_digest
    assert rows[first]["supersedes_record_digest"] is None
    assert rows[successor.record_digest]["superseded_by"] is None
    assert rows[successor.record_digest]["supersedes_record_digest"] == first

    adapter = _FakeExecutionAdapter(successor.record.plan_digest)
    _install(monkeypatch, _FakeCompositeBridge())
    result = execute_retained_execution_plan_with_installed_approval(
        tmp_path,
        ExecutionPlanExecutionRequest(plan_record_digest=successor.record_digest),
        _adapter_registry=_registry(_FakeAdapterFactory(adapter)),
    )

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert result.plan_digest == successor.record.plan_digest
    del record


def test_a_child_egress_plan_is_executable_only_by_a_declaring_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declaration is checked before the bridge and against reality after it."""

    _ledger, record, record_digest = _retain(
        tmp_path, plan_id="plan-multi", expected_child_egress_count=2
    )
    bridge = _FakeCompositeBridge()
    _install(monkeypatch, bridge)
    # A factory that declares the capability but opens an adapter without it is
    # refused too, before the kernel rather than inside its mutation path.
    lying = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest), child_egress=True)

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(lying),
        )

    assert refused.value.code == "EXECUTION_ADAPTER_INCOMPATIBLE"
    assert "does not implement the child egress it declared" in refused.value.message
    assert lying.opened == 1
    assert bridge.calls == 1


def test_an_invalid_capability_declaration_is_refused_before_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Lying(_FakeAdapterFactory):
        def declared_capabilities(self) -> object:  # type: ignore[override]
            return {"child_egress": True}

    ledger, record, record_digest = _retain(tmp_path)
    _install(monkeypatch, _RefusingBridge())
    factory = _Lying(_FakeExecutionAdapter(record.plan_digest))

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(factory),
        )

    assert refused.value.code == "EXECUTION_ADAPTER_INCOMPATIBLE"
    assert refused.value.exit_code is ExitCode.PRECONDITION
    assert factory.opened == 0
    assert _claims(ledger) == []


def test_the_registry_seam_resolves_by_the_pair_the_plan_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A factory registered under another pair is not reachable by this plan."""

    ledger, record, record_digest = _retain(tmp_path)
    _install(monkeypatch, _RefusingBridge())
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))

    for registry in (
        _registry(factory, operation=OperationKind.CREATE),
        {("meta_ads", OperationKind.PAUSE): factory},
        {},
    ):
        with pytest.raises(AdhelmError) as refused:
            execute_retained_execution_plan_with_installed_approval(
                tmp_path,
                ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
                _adapter_registry=registry,
            )
        assert refused.value.code == "EXECUTION_ADAPTER_NOT_CONFIGURED"
    assert factory.opened == 0
    assert _claims(ledger) == []


def test_the_advertised_first_refusal_code_is_the_one_this_build_produces(
    tmp_path: Path,
) -> None:
    """`discover` must not name a gate the caller never reaches."""

    gates = installed_execution_refusal_gates()

    assert [gate["gate"] for gate in gates] == [
        "RELEASE_POLICY_WRITES_RELEASED",
        "COMPILED_APPROVAL_BRIDGE_INSTALLED",
        "APPROVAL_KIND_RENDERED_BY_INSTALLED_HOST",
        "CONFIGURED_EXECUTION_ADAPTER",
    ]
    # This build never releases writes, so the first gate is never satisfied.
    assert gates[0]["satisfied"] is False
    # Deciding the second gate means resolving the binding, which a static read
    # must not do, so it is reported as undecided rather than guessed.
    assert gates[1]["satisfied"] is None
    assert gates[2]["satisfied"] is False
    # Adapters are registered, so the last gate is satisfied; the caller still
    # never reaches it, because the release gate and both bridge gates precede it.
    assert gates[3]["satisfied"] is True
    assert first_execution_refusal_code() == "WRITES_NOT_RELEASED"
    # The release gate lives in the command, so the CLI is what produces that
    # code; the workflow below it keeps its own later refusals.
    _retain(tmp_path)
    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path, ExecutionPlanExecutionRequest(plan_record_digest="0" * 64)
        )
    assert refused.value.code == "EXECUTION_PLAN_NOT_RETAINED"


def _child_attempt(tmp_path: Path) -> tuple[Ledger, str, str]:
    """Leave one journaled child request of a real multi-child plan unresolved."""

    from test_kernel_child_egress import NOW, _HierarchyAdapter, _subplan
    from test_safety import FakeApprovalHost, direct_grant

    from adhelm.safety import CompositePlan, SafetyKernel

    save_configuration(tmp_path, Configuration(), replace=True)
    ledger = Ledger(tmp_path, profile_ref=_WORKSPACE)
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: NOW)
    compiled = CompositePlan(
        plan_id="child-plan",
        profile_ref=_WORKSPACE,
        subplans=(_subplan(children=3),),
        expires_at=NOW + timedelta(hours=1),
    )
    grant = direct_grant(compiled)
    kernel.record_direct_grant(compiled, grant, FakeApprovalHost())
    kernel.execute(compiled, grant, {"meta_ads": _HierarchyAdapter(compiled, crash_at=2)})
    catalog = unresolved_attempt_catalog(ledger)
    child = next(row for row in catalog["attempts"] if row["level"] == "CHILD")
    parent = next(row for row in catalog["attempts"] if row["level"] == "SUBPLAN")
    return ledger, str(child["started_record_digest"]), str(parent["started_record_digest"])


def test_recovering_a_journaled_child_names_the_attempt_it_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child digest is the wrong level, not an unknown record."""

    ledger, child, parent = _child_attempt(tmp_path)
    _install(monkeypatch, _RefusingBridge())
    store = _no_credentials(monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        record_retained_attempt_recovery_with_installed_approval(
            tmp_path, AttemptRecoveryRequest(started_record_digest=child)
        )

    assert refused.value.code == "ATTEMPT_RECOVERY_CHILD_START_NOT_RECOVERABLE"
    assert parent in refused.value.message
    assert parent in str(refused.value.next_action)
    assert child != parent
    assert store.calls == 0
    assert ledger.unresolved_child_starts()


def test_an_unknown_digest_is_still_the_generic_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _child, _parent = _child_attempt(tmp_path)
    _install(monkeypatch, _RefusingBridge())

    with pytest.raises(AdhelmError) as refused:
        record_retained_attempt_recovery_with_installed_approval(
            tmp_path, AttemptRecoveryRequest(started_record_digest="e" * 64)
        )

    assert refused.value.code == "ATTEMPT_RECOVERY_START_NOT_FOUND"
    del ledger
