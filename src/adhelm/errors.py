"""Sanitized errors: never interpolate raw inputs, provider responses, or secrets."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    INVALID_INPUT = 2
    PRECONDITION = 3
    AUTHORIZATION = 4
    PARTIAL = 5
    AMBIGUOUS = 6
    PENDING = 7
    INTERNAL = 10


class AdhelmError(Exception):
    """A sanitized local diagnostic.

    ``status_code`` and ``retry_after_seconds`` are optional *classification*
    facts a transport may attach so that a provider adapter can tell one
    refusal apart from another without ever seeing the response body.  They
    carry a numeric HTTP status and a bounded integer parsed from a
    ``Retry-After`` header only; no response text, header text, request value,
    or credential is ever recorded here.  Both stay ``None`` for every error
    that does not come from an observed HTTP status.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = 3,
        next_action: str = "Inspect the installed command contract.",
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.next_action = next_action
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
