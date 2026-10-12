"""Native money survives normalization without unsafe downward rounding."""

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_openai_ads import _account, _ad_group, _campaign, _client

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.inventory import _openai_snapshots, _snapshot
from adhelm.ledger import Ledger, NormalizedResourceState
from adhelm.providers.openai_ads import OpenAIAdsHierarchyInventoryQuery


@pytest.mark.parametrize("bid", [125_000, 200_000, 9_223_372_036_854_775_807])
def test_wire_native_micros_survive_history_without_loss(tmp_path: Path, bid: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        rows = []
        if request.url.path == "/v1/campaigns":
            rows = [_campaign("campaign_fixture")]
        elif request.url.path == "/v1/ad_groups":
            group = _ad_group("group_fixture")
            group["bidding_config"]["max_bid_micros"] = bid
            rows = [group]
        else:
            assert request.url.path == "/v1/ads"
        return httpx.Response(200, json={"object": "list", "data": rows, "has_more": False})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )
    assert hierarchy.ad_groups[0].max_bid_micros == str(bid)
    assert hierarchy.campaigns[0].lifetime_spend_limit_micros == "10000000"
    snapshots = _openai_snapshots(
        identity, list(hierarchy.campaigns), list(hierarchy.ad_groups), list(hierarchy.ads)
    )
    campaign = next(row for row in snapshots if row["resource_type"] == "CAMPAIGN")
    group = next(row for row in snapshots if row["resource_type"] == "AD_GROUP")
    assert campaign["normalized_state"]["budget_minor"] == 1000
    assert group["normalized_state"]["openai_ads_max_bid_micros"] == str(bid)
    if bid == 200_000:
        assert group["normalized_state"]["bid_amount_minor"] == 20
    else:
        assert "bid_amount_minor" not in group["normalized_state"]
        assert "SUBMINOR_AMOUNT_RETAINED_AS_NATIVE_MICROS" in group["data_quality_statuses"]
    ledger = Ledger(tmp_path)
    for snapshot in snapshots:
        ledger.append("changes", "RESOURCE_SNAPSHOT", snapshot, provider="openai_ads")
    assert ledger.verify("changes")["status"] == "VERIFIED"
    assert ledger.records("changes")[-1]["payload"]["normalized_state"][
        "openai_ads_max_bid_micros"
    ] == str(bid)


def test_native_micros_are_currency_bound_and_reject_overflow() -> None:
    with pytest.raises(ValidationError):
        NormalizedResourceState(openai_ads_max_bid_micros="1")
    with pytest.raises(ValidationError):
        NormalizedResourceState(currency="USD", openai_ads_max_bid_micros="9223372036854775808")


def test_native_namespace_is_checked_on_append_and_retained_scan(tmp_path: Path) -> None:
    account = _snapshot(
        provider_api_version="v1",
        resource_type="ACCOUNT",
        resource_ref="openai_ads.account.fixture",
        parents=[],
        status="active",
        state={"currency": "USD"},
    )
    payload = _snapshot(
        provider_api_version="v1",
        resource_type="CAMPAIGN",
        resource_ref="openai_ads.campaign.fixture",
        parents=["openai_ads.account.fixture"],
        status="paused",
        state={
            "currency": "USD",
            "budget_type": "LIFETIME",
            "openai_ads_lifetime_spend_limit_micros": "10000000",
        },
    )
    ledger = Ledger(tmp_path)
    ledger.append("changes", "RESOURCE_SNAPSHOT", account, provider="openai_ads")
    with pytest.raises(AdhelmError) as caught:
        ledger.append("changes", "RESOURCE_SNAPSHOT", payload, provider="meta_ads")
    assert caught.value.code == "INVALID_LEDGER_PAYLOAD"
    ledger.append("changes", "RESOURCE_SNAPSHOT", payload, provider="openai_ads")
    record = ledger.records("changes")[-1]
    record["provider"] = "meta_ads"
    record["record_digest"] = digest(
        {key: value for key, value in record.items() if key != "record_digest"}
    )
    with pytest.raises(AdhelmError) as scanned:
        ledger._verify_record(
            record, "changes", record["stream_sequence"], record["previous_record_digest"]
        )
    assert scanned.value.code == "INVALID_LEDGER_PAYLOAD"


@pytest.mark.parametrize(
    "state",
    [
        {"currency": "USD", "openai_ads_max_bid_micros": "125000", "bid_amount_minor": 12},
        {
            "currency": "USD",
            "openai_ads_lifetime_spend_limit_micros": "10000000",
            "budget_minor": 10,
        },
        {"currency": "JPY", "openai_ads_max_bid_micros": "10000000", "bid_amount_minor": 1000},
    ],
)
def test_native_and_shared_money_cannot_disagree_in_canonical_state(state) -> None:
    with pytest.raises(ValidationError):
        NormalizedResourceState.model_validate(state)
