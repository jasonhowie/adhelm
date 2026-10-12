"""Independent review of P01-TT: TikTok token lifecycle, read declarations, audit rules.

Every case here is offline.  The token transport is driven with an
``httpx.MockTransport`` so no socket is opened, the rule table is evaluated over
retained models only, and the documentation checks read files from the tree.

These tests state the behaviour this reviewer expects.  They are written to fail
against the branch under review; the product code is deliberately not changed
here.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from test_tiktok_audit import (
    ACCOUNT,
    AD,
    AD_GROUP,
    CAMPAIGN,
    _account,
    _context,
    _fired,
    _inventory,
    _record,
)

from adhelm import models as models_module
from adhelm import tiktok_token_profiles as profiles_module
from adhelm.audits import ProviderAuditEvidence
from adhelm.audits.tiktok_ads import _DIMENSION_SURFACES, RULES, evaluate
from adhelm.oauth_enrollment import OAuthTokenDispatchState, OAuthTransportError
from adhelm.providers.tiktok_ads import (
    TIKTOK_ADS_READ_SURFACES,
    TIKTOK_ADS_STATE_SHAPE_STATUS,
)
from adhelm.tiktok_token_workflow import FixedTikTokTokenTransport, _parse_exchange

REPOSITORY = Path(__file__).resolve().parents[1]
DOCUMENTED_BODY = (("app_id", "app"), ("auth_code", "code"), ("secret", "secret"))


# ------------------------------------------------------- fixed token transport


def _recording_client(dispatched: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        dispatched.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"code": 0, "data": {"access_token": "t"}}).encode(),
        )

    return httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    )


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        # A host carrying userinfo resolves to the part after the `@`: the whole
        # pinned name becomes a username and the request leaves for the attacker.
        "business-api.tiktok.com@evil.example",
        "business-api.tiktok.com.evil.example",
    ],
)
def test_the_fixed_token_transport_refuses_a_host_outside_the_closed_map(host: str) -> None:
    """The one POST that carries the app secret and the auth code must be pinned.

    `FixedOAuthTokenTransport` can only be built from an `OAuthProviderContract`
    whose endpoint passed `_validate_https_endpoint`, and
    `FixedGoogleOAuthTransport` posts to a module constant.  The TikTok analogue
    interpolates whatever `host` string it is handed, so the pinning lives only
    at the one call site rather than in the transport.  A transport that can be
    pointed anywhere is not a fixed transport: constructing it with a host
    outside `TIKTOK_TOKEN_ENVIRONMENT_HOSTS` must be a `NOT_DISPATCHED` refusal,
    and nothing may leave the machine.
    """
    dispatched: list[str] = []
    with _recording_client(dispatched) as client:
        with pytest.raises(OAuthTransportError) as failure:
            FixedTikTokTokenTransport(host, client).post_token_body(DOCUMENTED_BODY)
        assert failure.value.dispatch_state is OAuthTokenDispatchState.NOT_DISPATCHED
    assert dispatched == []


def test_opening_the_production_transport_refuses_an_unpinned_host() -> None:
    """`open()` is the production constructor, so it must refuse the same hosts.

    It currently builds a live client for any non-empty string, which means the
    only thing standing between the pasted one-hour authorization code and an
    arbitrary origin is the caller remembering to resolve the host through
    `tiktok_environment_host` first.
    """
    with pytest.raises(OAuthTransportError):
        FixedTikTokTokenTransport.open("attacker.example").close()


# --------------------------------------------------------- exchange honesty


def _envelope(data: dict[str, object]) -> bytes:
    return json.dumps(
        {"code": 0, "message": "OK", "request_id": "request-1", "data": data}
    ).encode()


def test_two_contradicting_declared_lifetimes_are_refused_rather_than_silently_resolved() -> None:
    """Neither lifetime field name is proven, so a disagreement is not resolvable.

    `docs/research/tiktok-oauth-contract.md` records the exchange payload's field
    names as `UNVERIFIED`: the SDK proves the envelope, not `data`.  A response
    that declares two different bounded lifetimes therefore cannot be reduced to
    one `PROVIDER_DECLARED` expiry by preferring whichever key the parser happens
    to look at first -- that records an expiry the same response contradicts.
    The reviewed shape is the one this build accepts, and this is not it.
    """
    with pytest.raises(OAuthTransportError) as failure:
        _parse_exchange(
            _envelope({"access_token": "tok", "expires_in": 100, "token_expires_in": 999_999}),
            app_ref="app",
            environment="PRODUCTION",
        )
    assert failure.value.code == "TIKTOK_TOKEN_RESPONSE_UNSUPPORTED"


# ------------------------------------------------------------ audit honesty


def _unreviewed_parent_records() -> tuple[object, ...]:
    """A campaign whose operation status is outside the reviewed pair."""
    return (
        _account(),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={"status": "PAUSED_BY_SYSTEM", "currency": "USD", "budget_minor": 5_000},
            quality=(
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                "TIKTOK_OPERATION_STATUS_NOT_IN_REVIEWED_ENUM",
                "TIKTOK_OBJECTIVE_RETURNED",
                "TIKTOK_BUDGET_MODE_DAY",
                "TIKTOK_BUDGET_AMOUNT_PRESENT",
            ),
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state={
                "status": "ENABLE",
                "currency": "USD",
                "budget_minor": 1_000,
                "bid_strategy": "BID_TYPE_NO_BID",
            },
            quality=(
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                "TIKTOK_OPERATION_STATUS_REVIEWED",
                "TIKTOK_BUDGET_MODE_DAY",
                "TIKTOK_BUDGET_AMOUNT_PRESENT",
                "TIKTOK_BID_TYPE_RETURNED",
                "TIKTOK_BID_PRICE_ABSENT",
            ),
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_GROUP),
            state={"status": "ENABLE", "currency": "USD"},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_OPERATION_STATUS_REVIEWED"),
        ),
    )


def test_an_unreviewed_parent_status_is_not_read_as_a_serving_contradiction() -> None:
    """A status the table refuses to interpret cannot also mean "not serving".

    `TT_STRUCTURE_STATUS_NOT_REVIEWED` says out loud that "this audit does not
    guess what an unreviewed status means", and `_structure_findings` excludes
    the advertiser account from both sides of the contradiction comparison for
    exactly that reason.  The same exclusion must apply to an unreviewed
    `operation_status` on a campaign, ad group or ad: today it is silently read
    as non-serving, so an `ENABLE` child under it raises a MEDIUM
    `TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT` and a declined `PAUSE` row
    built on a value the same run reported as uninterpretable.
    """
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_unreviewed_parent_records())  # type: ignore[arg-type]
            )
        )
    )
    fired = _fired(sections)
    assert "TT_STRUCTURE_STATUS_NOT_REVIEWED" in fired
    assert "TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT" not in fired


# The concept word each declared-unread surface must reach a reader by, either
# through `_DIMENSION_SURFACES` or through a rule's fixed prose.
_UNREAD_SURFACE_CONCEPTS = {
    "AD_CREATIVE_ASSOCIATION": "creative",
    "AD_CREATIVE_VIDEO_PRESENCE": "video",
    "AD_CREATIVE_COPY_TEXT": "ad copy",
    "AD_GROUP_OPTIMIZATION_GOAL": "optimization",
    "REPORT_ATTRIBUTION": "attribution",
    "REPORT_FRESHNESS": "fresh",
}


def test_every_declared_unread_surface_reaches_a_reader() -> None:
    """ "Unread is not healthy" only holds if the reader is told the surface exists.

    `TIKTOK_ADS_READ_SURFACES` is the single declaration of what the fixed reads
    return, and the rule table's second honesty bound promises that everything
    declared `NOT_IN_REVIEWED_FIXED_READ` is "reported as unread here".  A
    surface that neither backs a dimension through `_DIMENSION_SURFACES` nor is
    named by any rule's fixed prose is silent: nothing in a result says the
    product never looked at it.
    """
    mapped = {surface for surfaces in _DIMENSION_SURFACES.values() for surface in surfaces}
    prose = " ".join(
        f"{rule.observation} {rule.suggested_action}" for rule in RULES.values()
    ).lower()
    silent = sorted(
        surface
        for surface, concept in _UNREAD_SURFACE_CONCEPTS.items()
        if TIKTOK_ADS_READ_SURFACES[surface] == "NOT_IN_REVIEWED_FIXED_READ"
        and surface not in mapped
        and concept not in prose
    )
    assert silent == []


# ------------------------------------------------- one declaration per fact


def test_the_reserved_tiktok_credential_prefix_has_one_declaration() -> None:
    """`is_reserved_credential_reference` and the slot deriver must share a constant.

    `adhelm.linkedin_token_profiles` imports its prefix from `adhelm.models`, and
    `docs/implementation/tiktok-token-lifecycle.md` says the TikTok one "should
    then be re-pointed at the `adhelm.models` constant" once the reserved-namespace
    hunk lands.  It landed; the re-point did not.  Two independent literals for
    one reserved namespace can drift, and the drift silently un-reserves the
    derived slot selectors rather than failing anything.
    """
    assert (
        profiles_module.TIKTOK_TOKEN_CREDENTIAL_REFERENCE_PREFIX
        is models_module.TIKTOK_TOKEN_CREDENTIAL_REFERENCE_PREFIX
    )


# --------------------------------------------------------- documented facts


def test_the_research_contract_names_only_symbols_that_exist() -> None:
    """A research contract that names a symbol nobody wrote is not reviewable.

    `docs/research/tiktok-oauth-contract.md` places `FixedTikTokTokenTransport`
    in `adhelm.tiktok_token_credentials` (it lives in `adhelm.tiktok_token_workflow`)
    and states that the exchange "records `TIKTOK_ADVERTISER_IDS_NOT_RETURNED`"
    when no advertiser list is returned.  No such name exists anywhere in the
    tree; the recorded fact is `advertiser_list_status="NOT_RETURNED"`.
    """
    contract = (REPOSITORY / "docs/research/tiktok-oauth-contract.md").read_text(encoding="utf-8")
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (REPOSITORY / "src").rglob("*.py")
    )
    invented = [
        name
        for name in ("TIKTOK_ADVERTISER_IDS_NOT_RETURNED",)
        if name in contract and name not in sources
    ]
    assert invented == []
    assert "adhelm.tiktok_token_credentials.FixedTikTokTokenTransport" not in contract


_STALE_UNWIRED_CLAIMS = {
    "docs/capability-matrix.md": (
        "the `auth tiktok-token` group is unwired until the `cli.py` hunk in the "
        "token-lifecycle doc is applied"
    ),
    "docs/implementation/tiktok-token-lifecycle.md": (
        "Until these three hunks land, the commands are **unwired**"
    ),
    "scripts/validate_development.py": (
        "until the coordinator applies it, the command is\n    absent from discovery and this "
        "check fails"
    ),
}


def test_the_shipped_documents_do_not_describe_the_wired_commands_as_unwired() -> None:
    """`docs/capability-matrix.md` is AGENTS.md required reading; it must be current.

    The coordinator's wiring commit applied both integration hunks: `adhelm.cli`
    registers the `auth tiktok-token` group and `_COMMANDS` lists all four leaves,
    so `discover` finds them.  Three shipped files still tell a reader the
    opposite, and the token-lifecycle document still carries an "Integration
    hunks the coordinator must apply" section for work that is already applied.
    """
    from adhelm.cli import _COMMANDS

    assert "auth tiktok-token enroll" in _COMMANDS
    stale = sorted(
        name
        for name, claim in _STALE_UNWIRED_CLAIMS.items()
        if claim in (REPOSITORY / name).read_text(encoding="utf-8")
    )
    assert stale == []


def test_the_lifecycle_document_no_longer_asks_for_applied_hunks() -> None:
    """An applied hunk recorded as outstanding work misdirects the next reader."""
    text = (REPOSITORY / "docs/implementation/tiktok-token-lifecycle.md").read_text(
        encoding="utf-8"
    )
    assert "## Integration hunks the coordinator must apply" not in text
