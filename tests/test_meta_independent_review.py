# ruff: noqa: E501
"""Independent review of P01-MA: Meta Ads read completeness, token status, audit.

Every case here is hermetic: `httpx.MockTransport`, an in-memory credential
store, and a local ledger only.  No live Meta request is made.

Each test states the behaviour the shipped contract already claims, so a
failure here is a defect in the product or a false claim in a document, never a
new requirement invented by the reviewer.  The claim each case holds the build
to is quoted in its docstring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_inspection as inspection
from adhelm.audit import AuditPolicy
from adhelm.audits import (
    ProviderAuditContext,
    ProviderAuditEvidence,
    ResourceSnapshotState,
    RetainedInventory,
    RetainedSnapshotRecord,
)
from adhelm.audits.meta_ads import RULES, evaluate
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.meta_token_credentials import (
    enroll_meta_token_profile,
    install_meta_token_credentials,
    prepare_meta_token_rotation,
)
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import (
    Configuration,
    MetaAccountTokenProfile,
    MetaNativeIdentifier,
    Money,
    OutputPreferences,
)
from adhelm.providers.meta_ads import (
    META_ADS_API_HOST,
    META_ADS_API_VERSION,
    MetaAdsClient,
    MetaAdsInventoryQuery,
)
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.transport import SafeHttpClient
from adhelm.workflows import persist_report

REPO = Path(__file__).resolve().parent.parent

NATIVE_ACCOUNT = "123"
ACCOUNT = "meta_ads.account.act_123"
CAMPAIGN = "meta_ads.campaign.11"
AD_SET = "meta_ads.adset.12"
AD = "meta_ads.ad.13"


# --------------------------------------------------------------- adapter fakes


def _account_payload() -> dict[str, Any]:
    return {"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"}


def _adapter(handler: httpx.MockTransport) -> MetaAdsClient:
    return MetaAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {META_ADS_API_HOST}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _identity(client: MetaAdsClient) -> Any:
    return client.discover_identity("act_123", expected_account_client_ref="123")


def _next_url(edge: str, cursor: str) -> str:
    return f"https://{META_ADS_API_HOST}/{META_ADS_API_VERSION}/act_123/{edge}?after={cursor}"


# ================================================================= FINDING 1 ==
# The typed Meta rate-limit and access classifier is never called by product
# code, so no real read can ever produce the diagnostics both documents claim.


def test_a_meta_rate_limit_status_reaches_the_caller_as_the_documented_meta_diagnostic() -> None:
    """`docs/research/meta-ads-read-contract.md` L114-117 claims the adapter
    classifies `429` to `META_ADS_RATE_LIMITED` (exit 3), and
    `docs/capability-matrix.md` L87 sells "typed Graph rate-limit
    classification" as an implemented subset of the Meta read.

    `classify_meta_ads_transport_error` is defined in
    `src/adhelm/providers/meta_ads.py:349` and imported by nothing outside
    `tests/`.  `MetaAdsClient._get` (`src/adhelm/providers/meta_ads.py:1276`)
    calls the shared transport with no handler, so the shared
    `PROVIDER_REQUEST_FAILED` reaches the caller unchanged: the classifier is
    an unwired stub and its four Graph rate-limit codes are unreachable.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": 4}}, headers={"retry-after": "30"})

    client = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("act_123", expected_account_client_ref="123")
    assert raised.value.code == "META_ADS_RATE_LIMITED"
    assert raised.value.exit_code == 3
    assert "regain" in (raised.value.next_action or "")


@pytest.mark.parametrize("status", [401, 403])
def test_a_meta_denied_status_reaches_the_caller_with_the_documented_exit_code(
    status: int,
) -> None:
    """The same document claims `401` and `403` become `PROVIDER_ACCESS_REQUIRED`
    with **exit code 4** and the Meta-specific reauthorization guidance that
    names `adhelm auth meta-token inspect` and refuses to assert an app-review
    outcome.  Because the classifier is unwired, the caller instead receives
    the shared transport's generic refusal at the default exit code 3 with the
    generic next action "Reauthorize locally with the required scope."
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": 190}})

    client = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("act_123", expected_account_client_ref="123")
    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert raised.value.exit_code == 4
    assert "not documented locally" in (raised.value.next_action or "")


# ================================================================= FINDING 2 ==
# A page after the first carries the documented `paging.previous` key, which
# the fixed read rejects, so no multi-page Meta account can be read at all.


def test_a_second_page_that_carries_the_documented_previous_link_is_still_read() -> None:
    """Meta documents cursor paging as `paging.cursors{before,after}` plus
    `next` **and** `previous`; every page after the first carries `previous`.

    `MetaAdsClient._page` allows only `{"cursors", "next"}`
    (`src/adhelm/providers/meta_ads.py:1233`), so page two of any real account
    fails with `PROVIDER_RESPONSE_INVALID` before a single record is returned.
    `docs/capability-matrix.md` L87 nonetheless sells "paginated
    campaign/ad-set/ad inventory" and the read contract claims the reads are
    "consumed to completion".  A one-page fixture cannot show this.
    """
    cursor = "cursorpageone"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account_payload())
        if request.url.params.get("after") is None:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "11", "effective_status": "ACTIVE"}],
                    "paging": {
                        "cursors": {"before": "beforeone", "after": cursor},
                        "next": _next_url("campaigns", cursor),
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id": "12", "effective_status": "ACTIVE"}],
                "paging": {
                    "cursors": {"before": "beforetwo"},
                    # Documented, and returned by Graph on every page after
                    # the first.
                    "previous": f"https://{META_ADS_API_HOST}/{META_ADS_API_VERSION}/act_123/campaigns?before=beforetwo",
                },
            },
        )

    client = _adapter(httpx.MockTransport(handler))
    identity = _identity(client)
    campaigns = client.list_campaigns(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert [item.campaign_client_ref for item in campaigns] == ["11", "12"]


def test_a_documented_empty_page_that_continues_is_followed_rather_than_refused() -> None:
    """Graph documents verbatim that "it is possible that a page may be empty
    but contain a `next` paging link".  `_page`
    (`src/adhelm/providers/meta_ads.py:1248`) treats an empty `data` with a
    valid `after` cursor as an invalid cursor and fails the whole read, so a
    real account with a privacy-filtered page returns nothing at all.
    """
    cursor = "cursorpageone"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account_payload())
        if request.url.params.get("after") is None:
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "paging": {
                        "cursors": {"before": "beforeone", "after": cursor},
                        "next": _next_url("campaigns", cursor),
                    },
                },
            )
        return httpx.Response(200, json={"data": [{"id": "11", "effective_status": "ACTIVE"}]})

    client = _adapter(httpx.MockTransport(handler))
    identity = _identity(client)
    campaigns = client.list_campaigns(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert [item.campaign_client_ref for item in campaigns] == ["11"]


# ================================================================= FINDING 3 ==
# Newly requested customer free text now passes through the fail-closed privacy
# screener, and the refusal names nothing and offers no next action.


def test_one_destination_url_carrying_a_contact_address_does_not_void_the_whole_ad_read() -> None:
    """This change added `creative{id,image_hash,link_url,object_url}` to
    `_AD_FIELDS` and `targeting` to `_ADSET_FIELDS`
    (`src/adhelm/providers/meta_ads.py:43,51`).  `_json`
    (`src/adhelm/providers/meta_ads.py:429`) runs `validate_data` over the whole
    raw Graph response, and `adhelm.privacy._EMAIL` matches any `x@y.z` run.

    So one ad whose landing page carries a contact address in its query --
    ordinary, legitimate advertiser content -- makes the *entire* fixed ad
    inventory read impossible.  Before this change the ad read requested only
    `id,campaign_id,adset_id,effective_status` and no advertiser free text
    could reach the screener at all.  The refusal is
    `PROVIDER_RESPONSE_INVALID` "Meta Ads response contains prohibited data.",
    which names no resource, offers no next action, and blames the provider for
    a local screening decision.  Neither
    `docs/research/meta-ads-read-contract.md` nor `docs/capability-matrix.md`
    discloses the new failure mode.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account_payload())
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "13",
                        "campaign_id": "11",
                        "adset_id": "12",
                        "effective_status": "ACTIVE",
                        "creative": {
                            "id": "990",
                            "link_url": "https://shop.example.com/lp?contact=sales@example.com",
                        },
                    }
                ]
            },
        )

    client = _adapter(httpx.MockTransport(handler))
    identity = _identity(client)
    ads = client.list_ads(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert [item.creative_link_present for item in ads] == [True]


# ================================================================= FINDING 4 ==
# `docs/capability-matrix.md` claims values are never *requested*.  They are.


def test_the_capability_matrix_does_not_claim_presence_only_values_are_never_requested() -> None:
    """`docs/capability-matrix.md` L87 ends the Meta row with "presence only --
    no targeting value, image hash, destination URL, or ad copy is ever
    **requested** or retained".

    The fixed read requests all three: `_ADSET_FIELDS` asks for the whole
    `targeting` object and `_AD_FIELDS` asks for `image_hash`, `link_url`, and
    `object_url` (`src/adhelm/providers/meta_ads.py:43,51`).  The adapter's own
    comments say so plainly -- the values "are read for presence only and
    their values ... are never retained".  Only *retention* is presence-only;
    the request and the response are not, and a row that says otherwise
    overstates the privacy boundary that was actually built.
    """
    from adhelm.providers import meta_ads as adapter

    row = ""
    for line in (REPO / "docs" / "capability-matrix.md").read_text().splitlines():
        if line.startswith("| Meta Marketing API Graph v26 "):
            row = line
    assert row, "the Meta read row is missing from the capability matrix"
    requests_values = (
        "targeting" in adapter._ADSET_FIELDS
        and "image_hash" in adapter._AD_FIELDS
        and "link_url" in adapter._AD_FIELDS
    )
    assert requests_values, "the fixed read no longer requests these fields"
    assert "is ever requested or retained" not in row


# ================================================================= FINDING 5 ==
# The P01-GA state-shape finding was not carried over to Meta.


def _snapshot_record(
    *,
    seed: int,
    resource_type: str,
    resource_ref: str,
    parents: tuple[str, ...],
    state: dict[str, Any],
    status: str,
    quality: tuple[str, ...],
    observed_at: datetime,
) -> RetainedSnapshotRecord:
    return RetainedSnapshotRecord(
        record_digest=format(seed, "064x"),
        observed_at=observed_at,
        snapshot=ResourceSnapshotState(
            provider_api_version="v26.0",
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_ref=resource_ref,
            parent_resource_refs=parents,
            normalized_state=state,
            normalized_state_digest=digest(state),
            effective_status=status,
            data_quality_statuses=quality,
        ),
    )


_CAMPAIGN_QUALITY = (
    "META_OBJECTIVE_RETURNED",
    "META_BUYING_TYPE_RETURNED",
    "META_BID_STRATEGY_RETURNED",
    "META_BUDGET_MODE_DAILY",
    "META_SCHEDULE_END_PRESENT",
)
_AD_SET_QUALITY = (
    "META_TARGETING_DECLARED",
    "META_TARGETING_GEO_PRESENT",
    "META_TARGETING_AGE_PRESENT",
    "META_TARGETING_INTEREST_PRESENT",
    "META_BUDGET_MODE_DAILY",
    "META_SCHEDULE_END_PRESENT",
    "META_BID_STRATEGY_RETURNED",
)
_AD_QUALITY = (
    "META_CREATIVE_ASSOCIATED",
    "META_CREATIVE_IMAGE_HASH_PRESENT",
    "META_CREATIVE_LINK_PRESENT",
    "META_AD_REVIEW_NOT_FLAGGED",
)

_PRE_UPGRADE_CAMPAIGN_STATE: dict[str, Any] = {
    "status": "ACTIVE",
    "currency": "USD",
    "objective": "OUTCOME_TRAFFIC",
    "budget_minor": 5_000,
    "budget_type": "DAILY",
    "schedule_start": "2026-08-01T00:00:00+00:00",
    "schedule_end": "2026-09-30T00:00:00+00:00",
}
# `src/adhelm/inventory.py:853` added exactly this one key to the retained Meta
# campaign state in this change.
_POST_UPGRADE_CAMPAIGN_STATE: dict[str, Any] = {
    **_PRE_UPGRADE_CAMPAIGN_STATE,
    "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
}


def _records(
    *, campaign_state: dict[str, Any], observed_at: datetime, seed: int
) -> tuple[RetainedSnapshotRecord, ...]:
    ad_set_state = {
        "status": "ACTIVE",
        "currency": "USD",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "budget_minor": 2_000,
        "budget_type": "DAILY",
        "schedule_end": "2026-09-30T00:00:00+00:00",
    }
    return (
        _snapshot_record(
            seed=seed,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            parents=(),
            state={"status": "1", "currency": "USD"},
            status="1",
            quality=(),
            observed_at=observed_at,
        ),
        _snapshot_record(
            seed=seed + 1,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state=campaign_state,
            status="ACTIVE",
            quality=_CAMPAIGN_QUALITY,
            observed_at=observed_at,
        ),
        _snapshot_record(
            seed=seed + 2,
            resource_type="AD_GROUP",
            resource_ref=AD_SET,
            parents=(CAMPAIGN,),
            state=ad_set_state,
            status="ACTIVE",
            quality=_AD_SET_QUALITY,
            observed_at=observed_at,
        ),
        _snapshot_record(
            seed=seed + 3,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_SET),
            state={"status": "ACTIVE"},
            status="ACTIVE",
            quality=_AD_QUALITY,
            observed_at=observed_at,
        ),
    )


def _review_report() -> NormalizedReport:
    return NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:meta-review",
            provider="meta_ads",
            source_account_ref=ACCOUNT,
            query_contract_hash="b" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="META_ACTION_ATTRIBUTION_WINDOWS",
            attribution_window="7d_click,1d_view",
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            data_quality_statuses=["COMPLETE"],
            provider_api_version="v26.0",
            connector_version="test",
        ),
        performance_rows=[],
    )


def test_a_connector_upgrade_that_adds_a_retained_field_is_not_reported_as_a_change_at_meta() -> (
    None
):
    """This is the P01-GA finding repaired in `29d72c1`, unrepaired for Meta.

    That commit established the rule: "adding `normalized_state.budget_type`
    to a retained campaign ... rewrites every campaign digest, so the first
    audit after the upgrade reported an out-of-band Google change for the whole
    account", and required `_state_deltas` to compare **only** the fields both
    retained reads returned, reporting a key-set difference as a local
    state-shape change with **no** drift observation.

    `src/adhelm/inventory.py:853` adds `normalized_state.bid_strategy` to every
    retained Meta campaign in this change, and
    `src/adhelm/audits/meta_ads.py:1465` still calls the shared
    `snapshot_state_deltas`, which reads a key present on one side only as a
    differing value.  There is no `META_ADS_STATE_SHAPE_VERSION` recorded
    outside `normalized_state` and no `MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED`
    rule, so the first audit after this upgrade asserts an out-of-band change
    at Meta, inside a recorded observation interval, for every campaign in the
    account -- a fabricated remote observation, and a violation of AGENTS.md
    rule 24.
    """
    earlier = _records(
        campaign_state=_PRE_UPGRADE_CAMPAIGN_STATE,
        observed_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        seed=100,
    )
    later = _records(
        campaign_state=_POST_UPGRADE_CAMPAIGN_STATE,
        observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        seed=200,
    )
    sections = evaluate(
        ProviderAuditContext(
            provider="meta_ads",
            report=_review_report(),
            source_record_digest="a" * 64,
            policy=AuditPolicy(
                maximum_observation_age_seconds=604_800, require_conversion_evidence=False
            ),
            as_of=datetime(2026, 9, 1, 18, tzinfo=UTC),
            evidence=ProviderAuditEvidence(
                inventory=RetainedInventory(
                    provider="meta_ads",
                    account_ref=ACCOUNT,
                    observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
                    completeness="COMPLETE_BOUNDED_READ",
                    records=later,
                ),
                earlier_inventory=RetainedInventory(
                    provider="meta_ads",
                    account_ref=ACCOUNT,
                    observed_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
                    completeness="COMPLETE_BOUNDED_READ",
                    records=earlier,
                ),
            ),
        )
    )
    shape_only = ("normalized_state.bid_strategy",)
    assert not [
        observation
        for observation in sections.drift_observations
        if observation.changed_paths == shape_only
    ], "a field this connector began retaining is reported as a change at Meta"
    assert "MA_DRIFT_STATE_CHANGED" not in {item.rule_id for item in sections.findings}


def test_the_meta_rule_table_can_name_a_local_retained_state_shape_change() -> None:
    """`29d72c1` added `GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED` precisely because a
    dropped `None` and a connector that started or stopped retaining a field
    are indistinguishable from the records alone.  The Meta table, which
    changed its retained campaign shape in this same change, has no such rule,
    so the difference has nowhere honest to go.
    """
    assert "MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED" in RULES


def test_every_meta_snapshot_records_the_connector_state_shape_version() -> None:
    """`29d72c1` also required every Google Ads snapshot to record
    `GOOGLE_ADS_STATE_SHAPE_VERSION` in `data_quality_statuses`, "outside
    `normalized_state` so it creates no drift of its own", so that two reads
    can be told apart after an upgrade.  Meta records no such version, so even
    a reader who suspects a shape change cannot confirm one.
    """
    from adhelm.providers import meta_ads as adapter

    assert hasattr(adapter, "META_ADS_STATE_SHAPE_VERSION")


# ================================================================= FINDING 6 ==
# The default `--account` derivation can never reach a real Meta read, and the
# resulting diagnostic tells the operator to repeat work they already did.


def _ledger_snapshot(
    resource_type: str, resource_ref: str, parents: list[str], state: dict[str, Any]
) -> dict[str, Any]:
    state_digest = digest(state)
    return {
        "snapshot_id": "snapshot:" + digest({"resource": resource_ref, "state": state_digest}),
        "provider_api_version": "v26.0",
        "connector_version": "test",
        "resource_type": resource_type,
        "resource_ref": resource_ref,
        "parent_resource_refs": parents,
        "remote_fingerprint": state_digest,
        "normalized_state": state,
        "normalized_state_digest": state_digest,
        "effective_status": str(state["status"]),
        "source_updated_at": None,
        "data_quality_statuses": [],
    }


def _retained_meta_account(directory: Path) -> str:
    """One completed Meta inventory read plus one report, exactly as this build writes them.

    `run_inventory` keys every Meta record by `profile.account_ref`
    (`src/adhelm/inventory.py:270`), which `MetaAccountTokenProfile` constrains to
    the bare numeric ad-account id.  `persist_report` keys the report by
    `report.envelope.source_account_ref` (`src/adhelm/workflows.py:444`), which
    `MetaAdsClient.discover_identity` sets to `meta_ads.account.act_<id>`
    (`src/adhelm/providers/meta_ads.py:870`).
    """
    ledger = Ledger(directory)
    snapshots = [
        _ledger_snapshot("ACCOUNT", ACCOUNT, [], {"status": "1", "currency": "USD"}),
        _ledger_snapshot("CAMPAIGN", CAMPAIGN, [ACCOUNT], {"status": "ACTIVE", "currency": "USD"}),
        _ledger_snapshot("AD_GROUP", AD_SET, [CAMPAIGN], {"status": "ACTIVE", "currency": "USD"}),
        _ledger_snapshot("AD", AD, [CAMPAIGN, AD_SET], {"status": "ACTIVE"}),
    ]
    appended = [
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="meta_ads", account_ref=NATIVE_ACCOUNT
        )
        for item in snapshots
    ]
    ledger.append(
        "changes",
        "INVENTORY_READ_COMPLETED",
        {
            "schema_version": 1,
            "provider": "meta_ads",
            "account_ref": NATIVE_ACCOUNT,
            "snapshot_record_digests": [record["record_digest"] for record in appended],
            "resource_counts": {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1},
            "read_started_at": "2026-09-01T11:00:00Z",
            "read_finished_at": "2026-09-01T12:00:00Z",
        },
        provider="meta_ads",
        account_ref=NATIVE_ACCOUNT,
    )
    checkpoint = ledger.checkpoint()
    report = NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:meta",
            provider="meta_ads",
            # Exactly what `MetaAdsClient._report` writes.
            source_account_ref=ACCOUNT,
            query_contract_hash="c" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="META_ACTION_ATTRIBUTION_WINDOWS",
            attribution_window="7d_click,1d_view",
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            data_fresh_through=None,
            data_quality_statuses=["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"],
            provider_api_version="v26.0",
            connector_version="test",
            source_ledger_checkpoints=[
                LedgerCheckpoint(
                    stream_id=value["stream_id"],
                    high_watermark_sequence=value["high_watermark"],
                    head_digest=value["head_digest"],
                )
                for value in checkpoint["streams"].values()
            ],
        ),
        performance_rows=[
            PerformanceRow(
                report_id="report:meta",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="META_ADS_CAMPAIGN",
                resource_ref=CAMPAIGN,
                campaign_ref=CAMPAIGN,
                dimension_type="DATE",
                dimension_value="2026-08-25",
                impressions=100,
                clicks=4,
                reach=80,
                spend=Money(minor_units=125, currency="USD"),
                data_quality_statuses=["COMPLETE"],
            )
        ],
    )
    return str(persist_report(ledger, report)["record_digest"])


@pytest.fixture
def _local_export_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)


def test_a_meta_report_this_build_produced_reaches_its_own_retained_inventory(
    tmp_path: Path, _local_export_key: None
) -> None:
    """The account-key gap, exercised the way an operator meets it.

    `src/adhelm/cli.py:1213` derives evidence selection from
    `report.envelope.source_account_ref` when no `--account` is given.  For
    Meta that value is `meta_ads.account.act_<id>`, and the retained records
    are keyed by the bare `<id>`, so the default route never matches.

    `docs/implementation/meta-ads-audit-recommendations.md:361` records the
    gap, but the route still fails with `INVENTORY_NOT_RETAINED` whose next
    action is "Run `adhelm inventory` for the configured read profile first." --
    advice the operator has already followed, and which will never help.
    `docs/capability-matrix.md` L96 sells the Meta audit with no mention of the
    gap, and AGENTS.md rule 27 requires actionable diagnostics.
    """
    directory = tmp_path / "data"
    record_digest = _retained_meta_account(directory)
    policy = directory / "audit-policy.json"
    policy.write_text(
        json.dumps(
            {"maximum_observation_age_seconds": 604800, "require_conversion_evidence": False}
        )
    )
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "audit",
            record_digest,
            "--input",
            str(policy),
            "--provider-rules",
            "--no-drift",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["data"]["rules_status"] == "EVALUATED"


# ================================================================= FINDING 7 ==
# `auth meta-token rotate` leaves the retained inspection record in place, so
# `status` reports the replaced token's expiry and grant.

_TOKEN_PROFILE = "meta-review-read"
_INSPECTION_NOW = datetime(2030, 1, 1, tzinfo=UTC)


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _install_inspection_transport(
    monkeypatch: pytest.MonkeyPatch, *, expires_at: datetime, scopes: list[str]
) -> None:
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "app_id": "67890",
                    "user_id": "54321",
                    "is_valid": True,
                    "type": "USER",
                    "scopes": scopes,
                    "issued_at": int((_INSPECTION_NOW - timedelta(minutes=1)).timestamp()),
                    "expires_at": int(expires_at.timestamp()),
                    "data_access_expires_at": int((expires_at + timedelta(days=30)).timestamp()),
                }
            },
        )

    monkeypatch.setattr(
        inspection.httpx,
        "Client",
        lambda *, trust_env: real_client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    monkeypatch.setattr(inspection, "_now", lambda: _INSPECTION_NOW)
    monkeypatch.setattr(inspection, "_monotonic", lambda: 100.0)


def _meta_profile(directory: Path) -> MetaAccountTokenProfile:
    profile = next(
        item
        for item in load_configuration(directory).profiles
        if isinstance(item, MetaAccountTokenProfile) and item.profile_id == _TOKEN_PROFILE
    )
    return profile


def test_rotating_a_meta_bundle_does_not_leave_the_previous_tokens_expiry_in_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P01-GA / P01-OA finding class, in the Meta token status route.

    `auth meta-token status` reports `token_validity`, `expires_at`,
    `data_access_expires_at`, and `granted_scope_count` from the retained
    inspection record (`src/adhelm/meta_token_cli.py:432-447`).  That record is
    invalidated only by a change to the **profile declaration** digest
    (`src/adhelm/meta_token_observation_records.py:147`), and
    `auth meta-token rotate` replaces the credential bundle without touching
    the profile (`src/adhelm/meta_token_credentials.py:171`,
    `src/adhelm/meta_token_cli.py:378-410`).

    After a rotation the newly installed token has never been inspected, yet
    status still asserts `VALID_AT_LAST_INSPECTION` together with the replaced
    token's expiry and scope grant.  That is a fact about one credential
    presented as a fact about a different one -- exactly what the OpenAI
    account-key review found (`tests/test_openai_independent_review.py:414`)
    and what `29d72c1` repaired for the Google developer-token slot.  A never
    inspected token must read as not inspected.
    """
    directory = tmp_path / "data"
    save_configuration(directory, Configuration(output=OutputPreferences(max_output_bytes=4096)))
    store = _MemoryStore()
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    enroll_meta_token_profile(
        directory,
        MetaAccountTokenProfileDraft.model_validate(
            {
                "schema_version": 1,
                "profile_id": _TOKEN_PROFILE,
                "account_ref": "12345",
                "client_application_ref": "67890",
                "currency": "USD",
                "timezone": "UTC",
                "purpose": "READ",
                "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
            }
        ),
        subject_token_supplier=lambda: "synthetic-meta-subject-token-one",
        inspector_token_supplier=lambda: "synthetic-meta-inspector-token",
    )
    _install_inspection_transport(
        monkeypatch,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        scopes=["ads_read", "ads_management", "public_profile"],
    )
    inspected = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "inspect",
            "--profile",
            _TOKEN_PROFILE,
        ],
    )
    assert inspected.exit_code == 0, inspected.output

    # Replace only the bundle, exactly as `auth meta-token rotate` does.
    slot = prepare_meta_token_rotation(directory, _TOKEN_PROFILE)
    install_meta_token_credentials(
        directory,
        slot,
        subject_token_supplier=lambda: "synthetic-meta-subject-token-two",
        inspector_token_supplier=lambda: "synthetic-meta-inspector-token",
    )

    status = CliRunner().invoke(
        app,
        ["--data-dir", str(directory), "auth", "meta-token", "status", "--profile", _TOKEN_PROFILE],
    )
    assert status.exit_code == 0, status.output
    data = json.loads(status.output)["data"]
    assert data["token_validity"] == "NOT_INSPECTED_LOCALLY"
    assert data["expires_at"] is None
    assert data["granted_scope_count"] is None


# ================================================================= FINDING 8 ==
# The Meta status route has no pin that it constructs no HTTP client.


def test_the_meta_token_status_route_constructs_no_http_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`29d72c1` added `test_the_status_route_constructs_no_http_client` for the
    Google OAuth status route so that "a future change that reached Google to
    answer one of these fields would be a silent boundary change".

    The Meta status route makes the identical promise -- "Return local bundle
    state only; it does not contact Meta" (`src/adhelm/meta_token_cli.py:415`)
    -- and `tests/test_meta_token_status_disclosure.py` has no equivalent
    case, so nothing stops a later change from answering `expires_at` with a
    live `debug_token` call.  This case supplies the missing guard; it passes
    against the current build and exists to keep it true.
    """
    directory = tmp_path / "data"
    save_configuration(directory, Configuration(output=OutputPreferences(max_output_bytes=4096)))
    store = _MemoryStore()
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    enroll_meta_token_profile(
        directory,
        MetaAccountTokenProfileDraft.model_validate(
            {
                "schema_version": 1,
                "profile_id": _TOKEN_PROFILE,
                "account_ref": "12345",
                "client_application_ref": "67890",
                "currency": "USD",
                "timezone": "UTC",
                "purpose": "READ",
                "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
            }
        ),
        subject_token_supplier=lambda: "synthetic-meta-subject-token-one",
        inspector_token_supplier=lambda: "synthetic-meta-inspector-token",
    )

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("`auth meta-token status` must construct no HTTP client")

    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(directory), "auth", "meta-token", "status", "--profile", _TOKEN_PROFILE],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["token_validity"] == "NOT_INSPECTED_LOCALLY"


# ================================================================= FINDING 9 ==
# The revocation prose never reaches the operator on the success path.


def test_the_status_route_discloses_the_revocation_boundary_to_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`META_TOKEN_REVOCATION_GUIDANCE` states the separation correctly -- local
    removal is not revocation, only Meta can revoke -- but the emitted OK
    payload carries only the bare enum
    `revocation: "PROVIDER_SIDE_ONLY_LOCAL_REMOVAL_IS_NOT_REVOCATION"`
    (`src/adhelm/meta_token_cli.py:88`).  The prose is emitted only inside the
    `META_TOKEN_EXPIRED` failure next action
    (`src/adhelm/meta_token_cli.py:438`).

    The Google precedent emits the guidance itself
    (`src/adhelm/google_oauth_cli.py:126`,
    `revocation_guidance=list(GOOGLE_OAUTH_REVOCATION_GUIDANCE)`), and
    `tests/test_meta_token_status_disclosure.py:242` only asserts the wording
    of the module constant, which proves nothing about what an operator sees.
    An agent reading this route cannot tell the marketer how to revoke.
    """
    from adhelm.meta_token_observation_records import META_TOKEN_REVOCATION_GUIDANCE

    directory = tmp_path / "data"
    save_configuration(directory, Configuration(output=OutputPreferences(max_output_bytes=4096)))
    store = _MemoryStore()
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    enroll_meta_token_profile(
        directory,
        MetaAccountTokenProfileDraft.model_validate(
            {
                "schema_version": 1,
                "profile_id": _TOKEN_PROFILE,
                "account_ref": "12345",
                "client_application_ref": "67890",
                "currency": "USD",
                "timezone": "UTC",
                "purpose": "READ",
                "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
            }
        ),
        subject_token_supplier=lambda: "synthetic-meta-subject-token-one",
        inspector_token_supplier=lambda: "synthetic-meta-inspector-token",
    )
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(directory), "auth", "meta-token", "status", "--profile", _TOKEN_PROFILE],
    )
    assert result.exit_code == 0, result.output
    assert META_TOKEN_REVOCATION_GUIDANCE in json.dumps(json.loads(result.output)["data"])


# =============================================================== CLEAN PINS ===
# Behaviour this review confirmed is correct, pinned so a later change cannot
# quietly lose it.


def test_no_targeting_or_creative_value_reaches_a_retained_meta_snapshot() -> None:
    """Presence-only retention holds under a hostile fixture.

    A geography name, an age bound, an interest name, a custom-audience
    identifier, an image hash, and a destination URL are all returned by the
    fixed read and none of them appears in the normalized snapshots the
    inventory branch builds.
    """
    from adhelm.canonical import canonical_bytes
    from adhelm.inventory import _meta_snapshots

    geo, interest, audience = "Palo Alto", "Chronic Pain Support", "23848494949"
    link, image_hash = "https://example.com/lp?utm_source=x", "a1b2c3d4e5f6a7b8c9d0"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account_payload())
        if path.endswith("/campaigns"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "11",
                            "effective_status": "ACTIVE",
                            "objective": "OUTCOME_TRAFFIC",
                            "buying_type": "AUCTION",
                            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                            "daily_budget": "5000",
                        }
                    ]
                },
            )
        if path.endswith("/adsets"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "12",
                            "campaign_id": "11",
                            "effective_status": "ACTIVE",
                            "targeting": {
                                "geo_locations": {
                                    "cities": [{"key": "2418779", "name": geo, "radius": 10}]
                                },
                                "age_min": 21,
                                "age_max": 54,
                                "flexible_spec": [
                                    {"interests": [{"id": audience, "name": interest}]}
                                ],
                                "custom_audiences": [{"id": audience, "name": "Uploaded list"}],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "13",
                        "campaign_id": "11",
                        "adset_id": "12",
                        "effective_status": "ACTIVE",
                        "creative": {"id": "990", "image_hash": image_hash, "link_url": link},
                    }
                ]
            },
        )

    client = _adapter(httpx.MockTransport(handler))
    identity = _identity(client)
    query = MetaAdsInventoryQuery(account_ref=identity.account_ref)
    snapshots = _meta_snapshots(
        identity,
        client.list_campaigns(query, identity=identity),
        client.list_adsets(query, identity=identity),
        client.list_ads(query, identity=identity),
    )
    retained = canonical_bytes(snapshots).decode()
    for value in (geo, interest, audience, link, image_hash):
        assert value not in retained


def test_a_graph_error_envelope_never_reaches_a_diagnostic() -> None:
    """`_json` reads only `error.code`; no `message`, `error_user_msg`,
    `error_user_title`, or `fbtrace_id` reaches an `AdhelmError`.
    """
    secret = "operator-only-provider-explanation"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account_payload())
        return httpx.Response(
            200,
            json={
                "error": {
                    "code": 17,
                    "message": secret,
                    "error_user_title": secret,
                    "error_user_msg": secret,
                    "fbtrace_id": secret,
                }
            },
        )

    client = _adapter(httpx.MockTransport(handler))
    identity = _identity(client)
    with pytest.raises(AdhelmError) as raised:
        client.list_campaigns(
            MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
    rendered = " ".join(
        str(part) for part in (raised.value.code, raised.value.message, raised.value.next_action)
    )
    assert secret not in rendered
    assert raised.value.code == "META_ADS_RATE_LIMITED"


def test_the_meta_native_profile_identifier_cannot_carry_the_canonical_reference() -> None:
    """The account-key gap is structural, not a fixture artefact.

    `MetaAccountTokenProfile.account_ref` is `MetaNativeIdentifier`, a bare
    decimal string, so the value the ledger is keyed by can never equal the
    `meta_ads.account.act_<id>` reference the report envelope carries.
    """
    from pydantic import TypeAdapter, ValidationError

    adapter = TypeAdapter(MetaNativeIdentifier)
    assert adapter.validate_python(NATIVE_ACCOUNT) == NATIVE_ACCOUNT
    with pytest.raises(ValidationError):
        adapter.validate_python(ACCOUNT)


# ================================================================ FINDING 10 ==
# The read-contract disclosures vanish with the account snapshot -- the
# P01-GA "disclosures" finding, repeated.


def test_the_unread_surface_disclosures_survive_an_audit_with_no_retained_inventory() -> None:
    """`29d72c1` repaired exactly this for Google Ads: "the unread-surface
    findings describe the read contract, not one retained record, so they no
    longer vanish with the account snapshot; they anchor to the audited report
    instead."  `_account_scoped_findings` in `src/adhelm/audits/google_ads.py:1463`
    now falls back to `context.report.envelope.source_account_ref` and
    `context.source_record_digest`.

    `src/adhelm/audits/meta_ads.py:1287` returns `[]` as soon as no `ACCOUNT`
    snapshot is grouped, and its docstring restates the pre-repair Google
    rationale verbatim.  So an audit of a retained Meta report with no
    inventory -- or a retained read that did not include the account snapshot
    -- reports none of `MA_PLACEMENT_NOT_READ`,
    `MA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ`,
    `MA_NATIVE_CONVERSION_NOT_READ`, `MA_BROWSER_NOT_APPLICABLE`, or
    `MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ`, every one of which is a
    fact about the fixed read contract and about no retained record at all.
    """
    sections = evaluate(
        ProviderAuditContext(
            provider="meta_ads",
            report=_review_report(),
            source_record_digest="a" * 64,
            policy=AuditPolicy(
                maximum_observation_age_seconds=604_800, require_conversion_evidence=False
            ),
            as_of=datetime(2026, 9, 1, 18, tzinfo=UTC),
            evidence=ProviderAuditEvidence(),
        )
    )
    fired = {item.rule_id for item in sections.findings}
    assert {
        "MA_PLACEMENT_NOT_READ",
        "MA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
        "MA_NATIVE_CONVERSION_NOT_READ",
        "MA_BROWSER_NOT_APPLICABLE",
        "MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ",
    } <= fired


# ================================================================ FINDING 11 ==
# The read contract affirmatively denies the retained-state change this change
# actually made.


def test_the_read_contract_does_not_deny_that_bid_strategy_is_a_retained_state_key() -> None:
    """`docs/research/meta-ads-read-contract.md:94-100` says campaign
    `buying_type` **and `bid_strategy`** "are recorded as explicit data-quality
    statuses rather than invented state keys: the shared closed
    `NormalizedResourceState` in `src/adhelm/ledger.py` has no field for them."

    That is true of `buying_type` and false of `bid_strategy`:
    `src/adhelm/ledger.py:578` declares `bid_strategy`, `src/adhelm/inventory.py:853`
    now retains it in every campaign's `normalized_state`, and
    `src/adhelm/audits/meta_ads.py:1132` reads it back from there.  The inline
    comment at `src/adhelm/inventory.py:857` explains the `quality=` line
    immediately below the state key it does not mention.

    `29d72c1` added a whole `### The retained normalized state shape` section to
    `docs/research/google-ads-read-contract.md` for exactly this class of
    change; the Meta contract has no counterpart, and the sentence it does
    have points a reader away from the field that changed.
    """
    from adhelm.inventory import _meta_snapshots
    from adhelm.ledger import NormalizedResourceState

    assert "bid_strategy" in NormalizedResourceState.model_fields

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/{META_ADS_API_VERSION}/act_123":
            return httpx.Response(200, json=_account_payload())
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "11",
                        "effective_status": "ACTIVE",
                        "objective": "OUTCOME_TRAFFIC",
                        "buying_type": "AUCTION",
                        "bid_strategy": "COST_CAP",
                        "daily_budget": "5000",
                    }
                ]
            },
        )

    client = _adapter(httpx.MockTransport(handler))
    identity = _identity(client)
    campaigns = client.list_campaigns(
        MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    snapshots = _meta_snapshots(identity, campaigns, [], [])
    campaign = next(item for item in snapshots if item["resource_type"] == "CAMPAIGN")
    # The retained shape really did change; this is the fact the contract denies.
    assert campaign["normalized_state"]["bid_strategy"] == "COST_CAP"

    contract = (REPO / "docs" / "research" / "meta-ads-read-contract.md").read_text()
    assert "Campaign `buying_type` and `bid_strategy`" not in contract


# ============================================================== RE-REVIEW 1 ==
# The account-key mapping silently drops one spelling's completed reads.


def test_two_spellings_of_one_meta_account_do_not_silently_hide_a_completed_read(
    tmp_path: Path, _local_export_key: None
) -> None:
    """`retained_account_key` joined the two spellings of a Meta account, and the
    multi-account guard in `src/adhelm/saved_audit_evidence.py:330` was relaxed
    from `len(accounts) > 1` to `len({retained_account_key(...)}) > 1` at the
    same time.

    Selection then filters on the raw string
    (`src/adhelm/saved_audit_evidence.py:346`), so when one account really does
    hold records under both spellings -- reachable today, because
    `run_inventory` keys by `profile.account_ref`, a `MetaAccountTokenProfile`
    constrains that to bare digits, and a generic `Profile` for `meta_ads`
    accepts the `act_<id>` form that `discover_identity` also normalizes --
    only one spelling's reads are selected. The rest are excluded from
    `retained_inventory_reads` and named nowhere, although this module promises
    that "every omission is named in that provenance; nothing is silently
    dropped." Before this repair the same ledger raised
    `AUDIT_ACCOUNT_SELECTION_REQUIRED`.

    Either outcome is defensible -- refuse and make the caller choose, or join
    the reads and count them all -- but silently auditing the older read while
    a newer completed one exists is not.
    """
    from adhelm.saved_audit_evidence import load_audit_evidence

    directory = tmp_path / "data"
    _retained_meta_account(directory)
    ledger = Ledger(directory)
    later = [
        _ledger_snapshot("ACCOUNT", ACCOUNT, [], {"status": "1", "currency": "USD"}),
        _ledger_snapshot("CAMPAIGN", CAMPAIGN, [ACCOUNT], {"status": "PAUSED", "currency": "USD"}),
        _ledger_snapshot("AD_GROUP", AD_SET, [CAMPAIGN], {"status": "PAUSED", "currency": "USD"}),
        _ledger_snapshot("AD", AD, [CAMPAIGN, AD_SET], {"status": "PAUSED"}),
    ]
    appended = [
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="meta_ads", account_ref=ACCOUNT
        )
        for item in later
    ]
    ledger.append(
        "changes",
        "INVENTORY_READ_COMPLETED",
        {
            "schema_version": 1,
            "provider": "meta_ads",
            "account_ref": ACCOUNT,
            "snapshot_record_digests": [record["record_digest"] for record in appended],
            "resource_counts": {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1},
            "read_started_at": "2026-09-02T11:00:00Z",
            "read_finished_at": "2026-09-02T12:00:00Z",
        },
        provider="meta_ads",
        account_ref=ACCOUNT,
    )
    evidence, provenance = load_audit_evidence(Ledger(directory), provider="meta_ads")
    assert evidence.inventory is not None
    # Two completed reads exist for this one account. The loader reports one
    # and audits the stale ACTIVE hierarchy as current.
    assert provenance["retained_inventory_reads"] == 2, provenance
    assert [
        record.snapshot.effective_status
        for record in evidence.inventory.records
        if record.snapshot.resource_type == "CAMPAIGN"
    ] == ["PAUSED"]
