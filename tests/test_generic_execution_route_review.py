"""Independent adversarial review of the generic execution route (S4).

Written by a reviewer who did not implement the change.  Each test encodes a
behaviour the reviewed contract claims, so a failure here is a claim the code
does not keep.  Every key is ephemeral, every adapter is in memory, and no
provider, credential store, or network is reached.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from test_generic_execution_route import (
    _BEFORE,
    _WORKSPACE,
    _candidate,
    _claims,
    _FakeAdapterFactory,
    _FakeCompositeBridge,
    _FakeExecutionAdapter,
    _install,
    _now,
    _registry,
    _retain,
)

from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_planning_session import BoundedExecutionRead, plan_execution
from adhelm.generic_execution_workflow import (
    ExecutionPlanExecutionRequest,
    execute_retained_execution_plan_with_installed_approval,
)
from adhelm.safety import StateRef

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

# --- Finding 1 -------------------------------------------------------------


def test_a_superseded_retained_plan_is_not_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan local history records as replaced is no longer the current plan.

    S3 gives a retained plan an explicit predecessor chain: a successor names
    its predecessor's record digest, ``validate_execution_plan_history`` keeps
    that chain linear and unforkable, and ``plan execution catalog`` surfaces
    ``supersedes_record_digest`` on every row.  ``_require_current_retained_plan``
    -- the function whose name is its contract -- checks the workspace, the
    expiry and the unresolved-attempt barrier, but not whether a successor
    exists, so the predecessor stays fully executable.

    That applies an intent this workspace's own history says was replaced, from
    a strictly older bounded read.  It is not an authority bypass, because the
    approval binds the predecessor's own digests and the kernel still re-reads
    and rejects drift; it is a currency question the route answers by ignoring
    the one field that expresses currency.
    """

    ledger, record, record_digest = _retain(tmp_path, plan_id="plan-1")
    now = _now()
    successor = plan_execution(
        ledger,
        _candidate(after=StateRef(fingerprint="c" * 64, revision="r3")),
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
        supersedes_record_digest=record_digest,
    ).record_digest
    bridge = _FakeCompositeBridge()
    _install(monkeypatch, bridge)
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(factory),
        )

    # Repaired: the predecessor is refused before the bridge, and the successor
    # the caller should execute instead is named.
    assert refused.value.code == "EXECUTION_PLAN_SUPERSEDED"
    assert refused.value.exit_code is ExitCode.PRECONDITION
    assert successor in refused.value.message
    assert bridge.calls == 0
    assert factory.opened == 0
    assert _claims(ledger) == []


# --- Finding 2 -------------------------------------------------------------


def test_a_plan_no_installed_adapter_can_execute_never_consumes_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's stated reason for checking the adapter before the bridge.

    ``execute_retained_execution_plan_with_installed_approval`` documents step 2
    as refusing "**before** the bridge, so a receipt is never consumed for a
    plan nothing can execute", and the registry-miss path keeps that.  A
    registered adapter that cannot execute the retained plan does not: a
    subplan with ``expected_child_egress_count`` above one needs the journaled
    child-egress interface, the kernel refuses it in ``_preflight_one`` with
    ``ADAPTER_CHILD_EGRESS_REQUIRED``, and by then the bridge has already built
    and consumed the one-use receipt and written its durable claim.

    The plan itself survives -- the kernel refuses before any
    ``APPLY_STARTED`` -- but the native approval is spent, so retrying needs a
    fresh human review of a plan that was never executable.  Either the route
    learns the factory's child-egress capability before the bridge, or the
    stated invariant should not be stated; the mechanism is the implementer's
    choice.
    """

    ledger, record, record_digest = _retain(
        tmp_path, plan_id="plan-multi", expected_child_egress_count=3
    )
    assert record.expected_child_egress_count == 3
    bridge = _FakeCompositeBridge()
    _install(monkeypatch, bridge)
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=_registry(factory),
        )

    # Repaired: the factory declares its child-egress capability before the
    # bridge, so the fact the kernel would have reported as
    # ADAPTER_CHILD_EGRESS_REQUIRED is now a typed pre-bridge refusal that names
    # it, and no receipt is built or consumed.
    assert refused.value.code == "EXECUTION_ADAPTER_INCOMPATIBLE"
    assert refused.value.exit_code is ExitCode.PRECONDITION
    assert "ADAPTER_CHILD_EGRESS_REQUIRED" in refused.value.message
    assert bridge.calls == 0
    assert factory.opened == 0
    assert not ledger.unresolved_starts()
    assert _claims(ledger) == []
