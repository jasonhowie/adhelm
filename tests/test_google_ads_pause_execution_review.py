"""Independent adversarial review of the Google Ads PAUSE execution route (P03).

Written by a reviewer who did not implement the change.  Each test encodes a
behaviour the reviewed contract claims, so a failure here is a claim the code
does not keep.  Every credential, transport, and approval here is a fake; no
provider, credential store, or network is reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_google_ads_pause_execution import _execute, _plan, _prepare, _readbacks

import adhelm.ledger as ledger_module
from adhelm.errors import AdhelmError
from adhelm.google_ads_pause_readback_records import (
    GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND,
    validate_google_ads_pause_readback_history,
)
from adhelm.ledger import Ledger

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def test_history_verify_enforces_the_readback_ordering_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readback invariant must survive a scan, not only an append.

    ``validate_google_ads_pause_readback_history`` is wired into
    ``Ledger.append`` (``ledger.py:954``) but not into the stream verification
    block (``ledger.py:2706-2713``), where every other ``changes`` history
    validator runs -- including its own direct twin,
    ``validate_meta_pause_readback_history`` at ``ledger.py:2706``.

    So the ordering rule the module documents -- "every retained readback must
    ... follow exactly one durable ``APPLY_STARTED`` for the same attempt.  A
    second readback for one attempt is refused" -- is enforced only at write
    time.  ``history verify`` and ``checkpoint()`` re-derive every other
    invariant from the retained bytes; for this kind they do not, so a
    ``changes`` stream that violates it still reports ``VERIFIED``, and every
    selection path built on a verified checkpoint inherits that weaker
    guarantee.  Rule 22 makes canonical history verifiable from its own bytes
    rather than from trust that a past writer checked it.

    The duplicate readback below is the module's own named violation, built
    from the genuine record the happy path retained.
    """

    prepared = _prepare(tmp_path, monkeypatch)
    _execute(prepared, _plan(prepared), monkeypatch)
    retained = _readbacks(tmp_path)
    assert len(retained) == 1
    payload: dict[str, Any] = dict(retained[0]["payload"])

    # Appending the duplicate is only possible with the write-time hook
    # neutralised; that is precisely the check this test says the scan should
    # not depend on.
    monkeypatch.setattr(ledger_module, "validate_google_ads_pause_readback_history", lambda _: None)
    ledger = Ledger(tmp_path)
    ledger.append(
        "changes",
        GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND,
        payload,
        provider=retained[0]["provider"],
        account_ref=retained[0]["account_ref"],
        run_id=retained[0]["run_id"],
        source="LOCAL_CLI",
        observed_at=retained[0]["observed_at"],
        occurred_at=retained[0]["occurred_at"],
    )
    monkeypatch.undo()

    reread = Ledger(tmp_path)
    # The retained bytes really do violate the invariant: the module's own
    # validator refuses them.
    with pytest.raises(AdhelmError):
        validate_google_ads_pause_readback_history(reread.records("changes"))
    # Therefore the stream must not verify.
    with pytest.raises(AdhelmError):
        reread.verify()
