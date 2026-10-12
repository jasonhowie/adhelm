"""The 0.1.0 release-policy gate: no remote mutation is released.

`docs/release-0.1.0.md` ("Writes refused") and ADR 0013 decision 7 ship the
write code and refuse it at runtime. This module is the one place that refusal
is decided. A command that can change a provider account is declared with
:func:`remote_mutation_leaf`; the declaration both registers the command for
`discover` and refuses it before the command reads configuration or input,
touches the operating-system credential store, appends `APPLY_STARTED`, or
opens an egress path.

Nothing here reads configuration, an environment variable, a flag, or a file,
so none of them can lift the refusal. Releasing writes is a source change to
this module, made with the approval-host and policy work ADR 0013 defers.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, Final, TypeVar, cast

from adhelm.errors import AdhelmError, ExitCode

WRITES_NOT_RELEASED: Final = "WRITES_NOT_RELEASED"

#: The one status every surface uses for a write command or plan execution.
UNRELEASED_STATUS: Final = "UNRELEASED"

_COMMAND_MARKER: Final = "__release_policy_command__"
_Function = TypeVar("_Function", bound=Callable[..., Any])
_unreleased_commands: set[str] = set()


def require_writes_released() -> None:
    """Refuse the remote mutation this build does not release."""

    raise AdhelmError(
        WRITES_NOT_RELEASED,
        "This build does not make remote changes to advertising or analytics accounts.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Remote writes are unreleased under the 0.1.0 release policy "
            "(docs/release-0.1.0.md). Apply the change manually in the provider's own "
            "interface; read, report, audit, and planning commands (no remote effect) "
            "remain available."
        ),
    )


def remote_mutation_leaf(command: str) -> Callable[[_Function], _Function]:
    """Declare a CLI command that can mutate a provider account.

    ``command`` is the full command path as `discover` lists it. The returned
    function runs the gate first and only then the command, so a command added
    under this decorator cannot reach a credential, the ledger, or egress while
    the gate refuses. The wrapped function carries the path so a test can find
    every declared leaf from the registered command tree.
    """

    def declare(function: _Function) -> _Function:
        @functools.wraps(function)
        def gated(*args: Any, **kwargs: Any) -> Any:
            from adhelm.cli import guarded

            guarded(command, require_writes_released)
            return function(*args, **kwargs)

        setattr(gated, _COMMAND_MARKER, command)
        _unreleased_commands.add(command)
        return cast(_Function, gated)

    return declare


def declared_command(function: Callable[..., Any]) -> str | None:
    """Return the command path a callback was declared under, or ``None``."""

    command = getattr(function, _COMMAND_MARKER, None)
    return command if isinstance(command, str) else None


def unreleased_commands() -> tuple[str, ...]:
    """Return every declared remote-mutation command, sorted."""

    return tuple(sorted(_unreleased_commands))
