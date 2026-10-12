"""Independent adversarial review of the generic retained execution plan (S3).

Written by a reviewer who did not implement the change.  Each test encodes a
behaviour the reviewed contract claims, so a failure here is a claim the code
does not keep.  Nothing here reaches a provider, a credential store, or a
network.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from test_execution_plan_records import _EXPIRES, _T0, candidate, read

import adhelm.cli as cli_module
from adhelm.errors import AdhelmError
from adhelm.execution_planning_session import (
    CONFIGURED_EXECUTION_PLANNERS,
    plan_execution,
)
from adhelm.ledger import Ledger

# --- Finding 1 -------------------------------------------------------------


def test_a_plan_cannot_be_compiled_from_a_read_that_has_not_happened(tmp_path: Path) -> None:
    """A retained plan may not claim a bounded read from after its own retention.

    ``ExecutionPlanSourceContinuity`` is documented as "the exact bounded read
    this plan was compiled against", and the record is append-only local
    history under AGENTS rule 22.  The interval is checked only for internal
    ordering and against ``expires_at``; nothing compares it with the retention
    instant, so a caller may retain a plan whose read finishes half an hour
    after the record's own ``recorded_at``.

    The GA4 CREATE family this generic session replaces already refuses exactly
    this: ``ga4_create_workflow.py`` lines 172-174 raise ``CLOCK_INVALID`` when
    the planning clock precedes ``observation.read_finished_at``.  The generic
    seam should not be weaker than the family it generalizes.
    """

    ledger = Ledger(tmp_path, profile_ref="default")
    now = _T0 + timedelta(minutes=1)
    future_read = read(
        read_started_at=now + timedelta(minutes=29),
        read_finished_at=now + timedelta(minutes=30),
    )

    with pytest.raises(AdhelmError):
        plan_execution(
            ledger,
            candidate(),
            read=future_read,
            plan_id="plan-1",
            profile_ref="default",
            expires_at=_EXPIRES,
            now=now,
        )


# --- Finding 2 -------------------------------------------------------------


def _registered_commands() -> list[str]:
    def walk(app: object, prefix: str = "") -> list[str]:
        found: list[str] = []
        for command in getattr(app, "registered_commands", []):
            name = command.name or command.callback.__name__.replace("_", "-")
            found.append(f"{prefix} {name}".strip())
        for group in getattr(app, "registered_groups", []):
            found += walk(group.typer_instance, f"{prefix} {group.name or ''}".strip())
        return found

    return sorted(walk(cli_module.app))


def test_every_registered_cli_command_is_discoverable() -> None:
    """``adhelm discover`` is how the agent this CLI is built for finds commands.

    AGENTS.md rule 27 makes the CLI agent-facing first: the marketer directs an
    agent, and that agent discovers and invokes the typed CLI.  ``_COMMANDS``
    is that catalogue, and it covers all 96 pre-existing commands exactly.  The
    three commands this ticket adds are registered on the ``plan`` group but
    absent from it, so an agent cannot find them and nothing fails.  No parity
    regression existed to catch it, which is why it slipped; this is that
    regression.
    """

    missing = [name for name in _registered_commands() if name not in set(cli_module._COMMANDS)]
    assert missing == []


# --- Contributed pin: the registry the module documents as empty -----------


def test_only_reviewed_execution_planners_ship() -> None:
    """An entry here compiles and durably retains a plan for that pair.

    The registry becoming non-empty is exactly the change that must not happen
    silently, so this pins the exact set rather than emptiness.  P03 registered
    ``("google_ads", PAUSE)``, P04 registered ``("google_ads", CREATE)``, P05
    registered ``("google_ads", ACTIVATE)`` and the maintenance kind ``UPDATE``
    (``ARCHIVE`` is delete-class and stays unregistered until rule 11's
    dependency proof has a cited read), P04-OA registered
    ``("openai_ads", CREATE)`` and P04-MA registered ``("meta_ads", CREATE)``,
    each with its own contract, planning session, and fixture evidence;
    anything else appearing here fails this test.  ``test_safety.py`` pins the
    sibling policy with
    ``READ_ONLY_OPERATIONS == frozenset()``.
    """

    from adhelm import (
        google_ads_pause_workflow,  # noqa: F401  (its import registers P03)
        plan_cli,  # noqa: F401  (its import registers P04, P04-OA, P04-MA and P05)
    )
    from adhelm.google_ads_activate_execution import GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER
    from adhelm.google_ads_create_execution import GOOGLE_ADS_CREATE_EXECUTION_PLANNER
    from adhelm.google_ads_maintain_execution import GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER
    from adhelm.google_ads_pause_execution import GOOGLE_ADS_PAUSE_EXECUTION_PLANNER
    from adhelm.meta_create_execution import META_CREATE_EXECUTION_PLANNER
    from adhelm.openai_create_execution import OPENAI_CREATE_EXECUTION_PLANNER
    from adhelm.safety import OperationKind

    assert dict(CONFIGURED_EXECUTION_PLANNERS) == {
        ("google_ads", OperationKind.CREATE): GOOGLE_ADS_CREATE_EXECUTION_PLANNER,
        ("google_ads", OperationKind.PAUSE): GOOGLE_ADS_PAUSE_EXECUTION_PLANNER,
        ("google_ads", OperationKind.ACTIVATE): GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER,
        ("google_ads", OperationKind.UPDATE): GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER,
        ("meta_ads", OperationKind.CREATE): META_CREATE_EXECUTION_PLANNER,
        ("openai_ads", OperationKind.CREATE): OPENAI_CREATE_EXECUTION_PLANNER,
    }
