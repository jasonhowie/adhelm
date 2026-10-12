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

    ``provider_error_code`` is likewise a classification fact: it is set only
    when a caller supplied a closed allowlist of provider error-code constants
    and exactly one of them appeared in a bounded refusal body.  Only the
    allowlisted constant is stored, never any body text.
    ``provider_code`` and ``provider_request_id`` are bounded, screened fields
    from the reviewed diagnostic parser. Free-form provider prose is never stored.
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
        provider_error_code: str | None = None,
        provider_code: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.next_action = next_action
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.provider_error_code = provider_error_code
        self.provider_code = provider_code
        self.provider_request_id = provider_request_id


def copy_provider_diagnostics(source: AdhelmError, target: AdhelmError) -> AdhelmError:
    """Keep bounded evidence without replacing provider-specific access guidance."""
    target.provider_error_code = source.provider_error_code
    target.provider_code = source.provider_code
    target.provider_request_id = source.provider_request_id
    if (
        target.code == "PROVIDER_REQUEST_FAILED"
        and source.provider_code
        and source.provider_code.startswith("queryError.")
    ):
        target.next_action = (
            "The installed Google Ads query is incompatible with the provider. "
            "Upgrade AdHelm or report the provider code and request identifier."
        )
    return target
