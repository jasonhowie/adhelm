"""Configured handoff from a protected Meta bundle to the fixed inspector."""

from __future__ import annotations

import hmac
import re
from pathlib import Path

from adhelm.errors import AdhelmError, ExitCode
from adhelm.meta_token_credentials import (
    _prepare_meta_token_inspection,
    _resolve_meta_token_inspection_credentials,
)
from adhelm.meta_token_inspection import MetaTokenObservation, inspect_meta_token

_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)


def inspect_configured_meta_token(
    directory: Path,
    profile_id: str,
    *,
    expected_configuration_digest: str | None = None,
) -> MetaTokenObservation:
    """Inspect one profile with both bundle reads bound to one configuration.

    This method deliberately returns only the reviewed redacted observation. It
    does not expose a bearer, account-access fact, provider read client, plan,
    approval, or execution capability.
    """
    expected = _expected_configuration(expected_configuration_digest)
    context = _prepare_meta_token_inspection(directory, profile_id)
    if expected is not None and not hmac.compare_digest(context._configuration_digest, expected):
        raise _configuration_changed()
    return inspect_meta_token(
        credential_supplier=lambda: _resolve_meta_token_inspection_credentials(directory, context),
        expected_client_application_ref=context._client_application_ref,
        required_scope=context._required_scope,
    )


def _expected_configuration(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _configuration_changed()
    return value


def _configuration_changed() -> AdhelmError:
    return AdhelmError(
        "CONFIGURATION_CHANGED",
        "Local configuration changed before Meta token inspection.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect local Meta token status before a deliberate follow-up.",
    )
