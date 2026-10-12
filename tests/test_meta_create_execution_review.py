"""Independent adversarial review of the Meta Ads CREATE route (P04-MA).

Written by a reviewer who did not implement the change.  Every credential,
transport, and approval here is a fake; no provider, credential store, or
network is reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from test_meta_create_execution import (
    _REAL_HTTP_CLIENT,
    _execute,
    _plan,
    _prepare,
)

import adhelm.egress_permit as permit_module
from adhelm import _meta_ads_token_session as shared_session

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def test_every_child_consumes_its_own_permit_before_its_own_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert the child-permit protocol directly, at dispatch, not by inference.

    ``kernel-generalization.md`` requires one permit per journaled child,
    consumed immediately before that child's egress and bound to that child's
    index, so a permit issued for child ``n`` can never carry child ``n + 1``'s
    request.  The suite asserts this only indirectly -- through the ledger's
    ``CHILD_APPLY_*`` ordering and the kernel's own refusals -- which cannot
    distinguish "consumed before the POST" from "consumed after it".

    This observes the live permit state from inside the transport handler, so it
    runs at the moment each request is dispatched: every child write must find an
    active permit that is already consumed and bound to its own index, in order,
    while the planning, identity and readback reads hold none.
    """

    prepared = _prepare(tmp_path, monkeypatch)
    observed: list[tuple[str, bool, object]] = []
    inner = prepared.meta.handler

    def recording(request: httpx.Request) -> httpx.Response:
        state = permit_module._current.get()
        claimed, index = (False, None) if state is None else (state.consumed, state.binding[3])
        observed.append((request.method, claimed, index))
        return inner(request)

    monkeypatch.setattr(
        shared_session,
        "_meta_http_client",
        lambda: _REAL_HTTP_CLIENT(transport=httpx.MockTransport(recording), trust_env=False),
    )

    result: Any = _execute(prepared, _plan(prepared), monkeypatch)
    assert result.results[0].status.value == "APPLIED_VERIFIED"

    writes = [(claimed, index) for method, claimed, index in observed if method == "POST"]
    reads = [(claimed, index) for method, claimed, index in observed if method != "POST"]
    assert writes == [(True, index) for index in range(1, 6)]
    assert all(entry == (False, None) for entry in reads)
    assert reads


def test_the_fixed_create_capability_cannot_be_serialized_or_repr_leaked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private capability that escapes must carry no token, URL, or account."""

    from adhelm import meta_ads_token_edit_session as edit_session
    from adhelm.ledger import Ledger
    from adhelm.meta_create_execution import _retained_action_for_plan

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    with edit_session.configured_meta_ads_token_edit_session(
        prepared.directory,
        action.execution_binding.profile_ref,
        expected_configuration_digest=action.execution_binding.configuration_digest,
    ) as session:
        egress = edit_session._issue_fixed_meta_ads_hierarchy_create_egress(
            session,
            account_client_ref=action.account_binding.account_client_ref,
            plan_digest=action.plan.plan_digest,
            subplan_digest=action.plan.subplans[0].subplan_digest,
        )
        assert repr(egress) == "<_FixedMetaAdsHierarchyCreateEgress redacted>"
        with pytest.raises(TypeError):
            egress.__reduce__()
        with pytest.raises(TypeError):
            egress.__getstate__()
