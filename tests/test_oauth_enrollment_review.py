"""Independent review regressions for the reusable OAuth enrollment module.

Each test in this file encodes a defect found while reviewing P01-LI.1.  They
are expected to fail against commit f8b9108 and to pass once the product code is
corrected.  Nothing here opens a socket, a browser, or a credential store.
"""

from __future__ import annotations

import json

import pytest

from adhelm.errors import AdhelmError
from adhelm.linkedin_token_profiles import LINKEDIN_OAUTH_CONTRACT
from adhelm.oauth_enrollment import (
    OAuthAuthorizationTarget,
    consume_callback,
    exchange_authorization_code,
    start_authorization,
)

REDIRECT_URI = "http://127.0.0.1:51234/oauth/callback"


class _FixedTokenTransport:
    """A fake token seam that replays one already-encoded provider body."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def post_token_form(self, fields: tuple[tuple[str, str], ...]) -> bytes:
        return self._body


def _read_target() -> OAuthAuthorizationTarget:
    return OAuthAuthorizationTarget(
        contract=LINKEDIN_OAUTH_CONTRACT,
        client_ref="review-client-id",
        scopes=("r_ads", "r_ads_reporting"),
    )


def _exchange(body: dict[str, object]) -> object:
    target = _read_target()
    attempt = start_authorization(target, REDIRECT_URI)
    callback = consume_callback(attempt, f"code=review-code&state={attempt.state}")
    return exchange_authorization_code(
        attempt,
        callback,
        token_transport=_FixedTokenTransport(json.dumps(body).encode()),
        client_secret="review-client-secret",
    )


def test_a_granted_scope_set_wider_than_the_request_is_not_silently_recorded() -> None:
    """Finding 3: a broader echoed ``scope`` is accepted and stored verbatim.

    ``exchange_authorization_code`` only rejects a *narrower* grant.  When the
    provider echoes a permission the local flow never requested, that permission
    is recorded as if this enrollment had asked for it, which is how a
    least-privilege read enrollment ends up holding ``rw_ads`` locally.  A grant
    that does not match the requested set must fail closed or be clamped to the
    requested set; it must never be persisted as the enrolled contract.
    """
    grant = _exchange(
        {
            "access_token": "review-access-value",
            "expires_in": 5_184_000,
            "scope": "r_ads r_ads_reporting rw_ads",
        }
    )

    assert grant.granted_scopes == ("r_ads", "r_ads_reporting")  # type: ignore[attr-defined]


def test_a_definite_provider_error_body_is_not_reported_as_an_uncertain_outcome() -> None:
    """Finding 8 (NOTE): a 200 body that is a provider error object is classified
    ``UNCERTAIN`` (exit 6) even though the response definitively granted nothing.

    Exit 6 tells the operator that local or remote state may have changed and
    that a follow-up must be deliberate.  A parsed provider error object is a
    definite negative answer and belongs at exit 4 with the authorization
    contract, not in the ambiguous class.
    """
    with pytest.raises(AdhelmError) as error:
        _exchange({"error": "invalid_grant", "error_description": "expired code"})

    assert error.value.exit_code == 4
