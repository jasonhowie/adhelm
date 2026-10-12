"""Provider-neutral derivation of protected OAuth token slot selectors.

This module is deliberately free of credential-store, HTTP, browser and CLI
dependencies, so each provider's profile module can stay reviewable on its own.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Final

from adhelm.errors import AdhelmError, ExitCode

_PROFILE_ID: Final = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$")


def derive_token_reference(
    *,
    label: str,
    code_prefix: str,
    reference_prefix: str,
    slot_domains: Mapping[str, tuple[bytes, str]],
    profile_id: object,
    slot: str,
) -> str:
    """Derive one protected slot selector for a profile.

    The profile ID is configuration data; the resulting reference is an internal
    credential-store selector that is never accepted from a caller.  The ``#``
    separator cannot appear in a configured ``SafeRef``, so a derived selector
    can never collide with a customer-chosen credential reference in either the
    credential-store or the local lock namespace.
    """
    if type(profile_id) is not str or _PROFILE_ID.fullmatch(profile_id) is None:
        raise AdhelmError(
            f"{code_prefix}_PROFILE_REQUIRED",
            f"The {label} token profile ID is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=f"Select one configured {label} Ads profile.",
        )
    if slot not in slot_domains:
        raise AdhelmError(
            f"{code_prefix}_SLOT_INVALID",
            f"The requested {label} token slot is not supported.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    domain, slot_label = slot_domains[slot]
    suffix = hashlib.sha256(domain + profile_id.encode("ascii")).hexdigest()
    return f"{reference_prefix}{slot_label}#{suffix}"
