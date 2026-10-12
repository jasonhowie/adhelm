"""Transient one-use adapter permits, issued only inside the kernel write path.

This prevents accidental public adapter invocation/replay. It is not a security
boundary against a hostile Python process with access to the same internals.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import NoReturn

from adhelm.errors import AdhelmError


class EgressPermit:
    __slots__ = ()

    def __new__(cls) -> EgressPermit:
        raise TypeError("Only the safety kernel issues egress permits")

    def __repr__(self) -> str:
        return "<EgressPermit>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("Egress permits cannot be serialized or copied")

    def consume(
        self,
        *,
        plan_digest: str,
        subplan_digest: str,
        attempt_ref: str,
        child_index: int | None = None,
    ) -> None:
        """Claim this active permit once, immediately before its fixed mutation.

        ``child_index`` is ``None`` for a subplan whose whole apply is one
        request.  A multi-step subplan gets one permit per journaled child, and
        each of those permits binds its own index, so a permit issued for child
        ``n`` can never be replayed as child ``n + 1``.
        """
        state = _current.get()
        if state is None:
            raise _required()
        with state.lock:
            if (
                not state.active
                or state.consumed
                or state.permit is not self
                or state.binding != (plan_digest, subplan_digest, attempt_ref, child_index)
            ):
                raise _required()
            try:
                state.check_current()
            except Exception:
                state.active = False
                raise
            state.consumed = True


@dataclass
class _PermitState:
    permit: EgressPermit
    binding: tuple[str, str, str, int | None]
    check_current: Callable[[], None]
    active: bool = True
    consumed: bool = False
    lock: Lock = field(default_factory=Lock)


_current: ContextVar[_PermitState | None] = ContextVar("adhelm_kernel_egress_permit", default=None)


def _required() -> AdhelmError:
    return AdhelmError(
        "KERNEL_EGRESS_PERMIT_REQUIRED",
        "A current, matching, unused safety-kernel egress permit is required.",
        exit_code=4,
        next_action="Execute the exact authorized plan through the safety kernel.",
    )


def _permit_claimed(permit: EgressPermit) -> bool:
    """Report whether the adapter consumed this permit inside its scope.

    Read before the scope closes.  A guarded adapter that returns a confirmed
    acknowledgement without ever claiming its permit gives the kernel no
    evidence that egress went through the guarded seam, so the kernel treats
    that acknowledgement as ambiguous.
    """
    state = _current.get()
    if state is None or state.permit is not permit:
        return False
    with state.lock:
        return state.consumed


def _invalidate_permit(permit: EgressPermit) -> None:
    """Revoke a kernel-issued permit before any post-dispatch readback."""
    state = _current.get()
    if state is None or state.permit is not permit:
        raise _required()
    with state.lock:
        state.active = False


@contextmanager
def _kernel_permit(
    *,
    plan_digest: str,
    subplan_digest: str,
    attempt_ref: str,
    check_current: Callable[[], None],
    child_index: int | None = None,
) -> Iterator[EgressPermit]:
    """Internal call site follows the matching durable start append.

    A subplan-level permit follows ``APPLY_STARTED``.  A child permit follows
    that child's own ``CHILD_APPLY_STARTED``; the kernel never issues one before
    the matching record has been appended and fsynced.
    """
    permit = object.__new__(EgressPermit)
    state = _PermitState(
        permit, (plan_digest, subplan_digest, attempt_ref, child_index), check_current
    )
    token = _current.set(state)
    try:
        yield permit
    finally:
        # Invalidation is the load-bearing step and always runs: it also kills
        # copies of this context retained by adapter code.
        with state.lock:
            state.active = False
        try:
            _current.reset(token)
        except ValueError:
            # The kernel force-closed a scope whose permit was entered in
            # another context, so this token does not belong here.  The permit
            # is already revoked above; clear the variable in this context too
            # rather than raising over the caller's own outcome.
            _current.set(None)
