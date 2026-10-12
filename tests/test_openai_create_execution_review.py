"""Independent adversarial review of the OpenAI Ads CREATE route (P04-OA).

Written by a reviewer who did not implement the change.  Every credential,
transport, and approval here is a fake; no provider, credential store, or
network is reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from test_openai_create_execution import _execute, _plan, _prepare

import adhelm.egress_permit as permit_module
from adhelm.providers import openai_create_adapter as adapter_module

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

    This observes the live permit state from inside the transport handler, so
    it runs at the moment each request is dispatched: every child write must
    find an active permit that is already consumed and bound to its own index,
    in order, while the planning and readback reads hold none.
    """

    prepared = _prepare(tmp_path, monkeypatch)
    observed: list[tuple[str, bool, object]] = []
    inner = prepared.openai.handler

    def recording(request: httpx.Request) -> httpx.Response:
        state = permit_module._current.get()
        claimed, index = (False, None) if state is None else (state.consumed, state.binding[3])
        observed.append((request.method, claimed, index))
        return inner(request)

    monkeypatch.setattr(
        adapter_module,
        "_openai_create_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(recording), trust_env=False),
    )

    result: Any = _execute(prepared, _plan(prepared), monkeypatch)
    assert result.results[0].status.value == "APPLIED_VERIFIED"

    writes = [(claimed, index) for method, claimed, index in observed if method == "POST"]
    reads = [(claimed, index) for method, claimed, index in observed if method != "POST"]
    assert len(writes) >= 4
    assert writes == [(True, index) for index in range(1, len(writes) + 1)]
    assert all(entry == (False, None) for entry in reads)
