"""Adversarial regressions for the bounded OpenAI Ads read contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import ValidationError
from test_openai_ads import _account, _ad, _ad_group, _campaign, _client

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError
from adhelm.inventory import _snapshot, _validate_snapshot_hierarchy
from adhelm.ledger import Ledger
from adhelm.providers.openai_ads import (
    OpenAIAdsAdGroup,
    OpenAIAdsCampaign,
    OpenAIAdsClient,
    OpenAIAdsHierarchyInventory,
    OpenAIAdsHierarchyInventoryQuery,
    OpenAIAdsInventoryQuery,
)


def _identity_only_client(calls: list[str]) -> OpenAIAdsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/v1/ad_account"
        return httpx.Response(200, json=_account())

    return _client(httpx.MockTransport(handler))


def _resource_snapshot(
    resource_type: Literal["ACCOUNT", "CAMPAIGN", "AD_GROUP", "AD"],
    resource_ref: str,
    parents: list[str],
    state: dict[str, Any],
) -> dict[str, Any]:
    return _snapshot(
        provider_api_version="v1",
        resource_type=resource_type,
        resource_ref=resource_ref,
        parents=parents,
        state=state,
        status="paused",
    )


def test_synthetic_canonical_parents_cannot_trigger_child_http() -> None:
    calls: list[str] = []
    client = _identity_only_client(calls)
    identity = client.discover_identity()
    campaign = OpenAIAdsCampaign(
        account_ref=identity.account_ref,
        campaign_ref="openai_ads.campaign.campaign_fixture",
        campaign_client_ref="campaign_fixture",
        status="paused",
        bidding_type="impressions",
    )
    group = OpenAIAdsAdGroup(
        account_ref=identity.account_ref,
        campaign_ref=campaign.campaign_ref,
        campaign_client_ref=campaign.campaign_client_ref,
        ad_group_ref="openai_ads.ad_group.group_fixture",
        ad_group_client_ref="group_fixture",
        status="paused",
        billing_event_type="impression",
    )
    query = OpenAIAdsInventoryQuery(account_ref=identity.account_ref)

    with pytest.raises(AdhelmError) as campaign_error:
        client.list_ad_groups(query, identity=identity, campaign=campaign)
    with pytest.raises(AdhelmError) as group_error:
        client.list_ads(query, identity=identity, ad_group=group)

    assert campaign_error.value.code == "PARENT_NOT_DISCOVERED"
    assert group_error.value.code == "PARENT_NOT_DISCOVERED"
    assert calls == ["/v1/ad_account"]


def test_hierarchy_rejects_invalid_billing_event_before_ad_egress() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v1/campaigns":
            return httpx.Response(
                200,
                json={"object": "list", "data": [_campaign("campaign_fixture")], "has_more": False},
            )
        assert request.url.path == "/v1/ad_groups"
        group = _ad_group("group_fixture")
        group["bidding_config"]["billing_event_type"] = "impressions"
        return httpx.Response(200, json={"object": "list", "data": [group], "has_more": False})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    with pytest.raises(AdhelmError) as raised:
        client.list_hierarchy(
            OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
        )

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert calls == ["/v1/ad_account", "/v1/campaigns", "/v1/ad_groups"]


@pytest.mark.parametrize("creative_type", ["image", "product_ad_template"])
def test_unreviewed_creative_shape_fails_closed(creative_type: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v1/campaigns":
            return httpx.Response(
                200,
                json={"object": "list", "data": [_campaign("campaign_fixture")], "has_more": False},
            )
        if request.url.path == "/v1/ad_groups":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_ad_group("group_fixture")],
                    "has_more": False,
                },
            )
        assert request.url.path == "/v1/ads"
        ad = _ad("ad_fixture")
        ad["creative"]["type"] = creative_type
        return httpx.Response(200, json={"object": "list", "data": [ad], "has_more": False})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    with pytest.raises(AdhelmError) as raised:
        client.list_hierarchy(
            OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
        )

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert calls == ["/v1/ad_account", "/v1/campaigns", "/v1/ad_groups", "/v1/ads"]


def test_hierarchy_global_resource_cap_rejects_before_child_reads() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v1/campaigns"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [_campaign("campaign_one"), _campaign("campaign_two")],
                "has_more": False,
            },
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    with pytest.raises(AdhelmError) as raised:
        client.list_hierarchy(
            OpenAIAdsHierarchyInventoryQuery(
                account_ref=identity.account_ref, maximum_records=1, page_size=2
            ),
            identity=identity,
        )

    assert raised.value.code == "RESULT_LIMIT_REQUIRED"
    assert calls == ["/v1/ad_account", "/v1/campaigns"]


def test_native_micros_remain_exact_above_binary_float_range() -> None:
    native = 9_007_199_254_740_993

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        if request.url.path == "/v1/campaigns":
            campaign = _campaign("campaign_fixture")
            campaign["budget"]["lifetime_spend_limit_micros"] = native
            return httpx.Response(
                200, json={"object": "list", "data": [campaign], "has_more": False}
            )
        if request.url.path == "/v1/ad_groups":
            group = _ad_group("group_fixture")
            group["bidding_config"]["max_bid_micros"] = native
            return httpx.Response(200, json={"object": "list", "data": [group], "has_more": False})
        assert request.url.path == "/v1/ads"
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    hierarchy = client.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
    )

    assert hierarchy.campaigns[0].lifetime_spend_limit_micros == str(native)
    assert hierarchy.ad_groups[0].max_bid_micros == str(native)


def test_native_micros_above_the_local_signed_int64_bound_fail_closed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account())
        assert request.url.path == "/v1/campaigns"
        campaign = _campaign("campaign_fixture")
        campaign["budget"]["lifetime_spend_limit_micros"] = 9_223_372_036_854_775_808
        return httpx.Response(200, json={"object": "list", "data": [campaign], "has_more": False})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity()
    with pytest.raises(AdhelmError) as raised:
        client.list_hierarchy(
            OpenAIAdsHierarchyInventoryQuery(account_ref=identity.account_ref), identity=identity
        )

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert calls == ["/v1/ad_account", "/v1/campaigns"]


def test_typed_native_micros_cannot_exceed_local_signed_int64_bound() -> None:
    with pytest.raises(ValidationError):
        OpenAIAdsCampaign(
            account_ref="openai_ads.account.account_fixture",
            campaign_ref="openai_ads.campaign.campaign_fixture",
            campaign_client_ref="campaign_fixture",
            status="paused",
            lifetime_spend_limit_micros="9223372036854775808",
        )


def test_typed_hierarchy_rejects_cross_account_parent_binding() -> None:
    campaign = OpenAIAdsCampaign(
        account_ref="openai_ads.account.account_one",
        campaign_ref="openai_ads.campaign.campaign_one",
        campaign_client_ref="campaign_one",
        status="paused",
        bidding_type="impressions",
    )
    group = OpenAIAdsAdGroup(
        account_ref="openai_ads.account.account_two",
        campaign_ref=campaign.campaign_ref,
        campaign_client_ref=campaign.campaign_client_ref,
        ad_group_ref="openai_ads.ad_group.group_one",
        ad_group_client_ref="group_one",
        status="paused",
        billing_event_type="impression",
    )

    with pytest.raises(ValidationError):
        OpenAIAdsHierarchyInventory(campaigns=(campaign,), ad_groups=(group,), ads=())


def test_openai_snapshot_checks_namespace_currency_and_retained_tree(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    account = _resource_snapshot(
        "ACCOUNT", "openai_ads.account.account_fixture", [], {"currency": "USD"}
    )
    campaign = _resource_snapshot(
        "CAMPAIGN",
        "openai_ads.campaign.campaign_fixture",
        ["openai_ads.account.account_fixture"],
        {
            "currency": "USD",
            "budget_type": "LIFETIME",
            "openai_ads_lifetime_spend_limit_micros": "10000000",
            "budget_minor": 1000,
            "lifetime_budget_minor": 1000,
        },
    )
    bad_group = _resource_snapshot(
        "AD_GROUP",
        "openai_ads.ad_group.group_fixture",
        ["openai_ads.campaign.campaign_fixture"],
        {"currency": "JPY", "openai_ads_max_bid_micros": "125000"},
    )
    wrong_namespace = _resource_snapshot(
        "CAMPAIGN",
        "campaign_fixture",
        ["openai_ads.account.account_fixture"],
        {"currency": "USD"},
    )

    with pytest.raises(AdhelmError) as unbound_account:
        ledger.append(
            "changes",
            "RESOURCE_SNAPSHOT",
            account,
            provider="openai_ads",
            account_ref="other_account",
        )
    assert unbound_account.value.code == "INVALID_LEDGER_PAYLOAD"
    with pytest.raises(AdhelmError) as namespace_error:
        ledger.append("changes", "RESOURCE_SNAPSHOT", wrong_namespace, provider="openai_ads")
    assert namespace_error.value.code == "INVALID_LEDGER_PAYLOAD"

    ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        account,
        provider="openai_ads",
        account_ref="account_fixture",
    )
    ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        campaign,
        provider="openai_ads",
        account_ref="account_fixture",
    )
    with pytest.raises(AdhelmError) as currency_error:
        ledger.append(
            "changes",
            "RESOURCE_SNAPSHOT",
            bad_group,
            provider="openai_ads",
            account_ref="account_fixture",
        )
    assert currency_error.value.code == "INVALID_LEDGER_PAYLOAD"

    group = _resource_snapshot(
        "AD_GROUP",
        "openai_ads.ad_group.group_fixture",
        ["openai_ads.campaign.campaign_fixture"],
        {"currency": "USD", "openai_ads_max_bid_micros": "125000"},
    )
    ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        group,
        provider="openai_ads",
        account_ref="account_fixture",
    )

    segment = tmp_path / "ledger" / "changes" / "00000000000000000001.jsonl"
    records = [parse_json(line) for line in segment.read_bytes().splitlines()]
    stored = records[-1]
    stored_state = stored["payload"]["normalized_state"]
    stored_state["currency"] = "JPY"
    state_digest = digest(stored_state)
    stored["payload"]["normalized_state_digest"] = state_digest
    stored["payload"]["remote_fingerprint"] = state_digest
    stored["record_digest"] = digest(
        {key: value for key, value in stored.items() if key != "record_digest"}
    )
    segment.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in records))

    with pytest.raises(AdhelmError) as retained_error:
        ledger.verify("changes")
    assert retained_error.value.code == "INVALID_LEDGER_PAYLOAD"


def test_openai_ad_parent_chain_is_consistent_in_inventory_and_retained_ledger(
    tmp_path: Path,
) -> None:
    account_ref = "openai_ads.account.account_fixture"
    campaign_one_ref = "openai_ads.campaign.campaign_one"
    campaign_two_ref = "openai_ads.campaign.campaign_two"
    group_ref = "openai_ads.ad_group.group_two"
    ad_ref = "openai_ads.ad.ad_fixture"
    account = _resource_snapshot("ACCOUNT", account_ref, [], {"currency": "USD"})
    campaign_one = _resource_snapshot(
        "CAMPAIGN", campaign_one_ref, [account_ref], {"currency": "USD"}
    )
    campaign_two = _resource_snapshot(
        "CAMPAIGN", campaign_two_ref, [account_ref], {"currency": "USD"}
    )
    group = _resource_snapshot("AD_GROUP", group_ref, [campaign_two_ref], {"currency": "USD"})
    valid_ad = _resource_snapshot("AD", ad_ref, [campaign_two_ref, group_ref], {"currency": "USD"})
    mismatched_ad = {**valid_ad, "parent_resource_refs": [campaign_one_ref, group_ref]}

    with pytest.raises(AdhelmError) as inventory_error:
        _validate_snapshot_hierarchy(
            [account, campaign_one, campaign_two, group, mismatched_ad], "openai_ads"
        )
    assert inventory_error.value.code == "PROVIDER_RESPONSE_INVALID"

    ledger = Ledger(tmp_path)
    for snapshot in (account, campaign_one, campaign_two, group):
        ledger.append(
            "changes",
            "RESOURCE_SNAPSHOT",
            snapshot,
            provider="openai_ads",
            account_ref="account_fixture",
        )
    with pytest.raises(AdhelmError) as append_error:
        ledger.append(
            "changes",
            "RESOURCE_SNAPSHOT",
            mismatched_ad,
            provider="openai_ads",
            account_ref="account_fixture",
        )
    assert append_error.value.code == "INVALID_LEDGER_PAYLOAD"

    ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        valid_ad,
        provider="openai_ads",
        account_ref="account_fixture",
    )
    segment = tmp_path / "ledger" / "changes" / "00000000000000000001.jsonl"
    records = [parse_json(line) for line in segment.read_bytes().splitlines()]
    record = records[-1]
    record["payload"]["parent_resource_refs"] = [campaign_one_ref, group_ref]
    record["record_digest"] = digest(
        {key: value for key, value in record.items() if key != "record_digest"}
    )
    segment.write_bytes(b"".join(canonical_bytes(item) + b"\n" for item in records))

    with pytest.raises(AdhelmError) as retained_error:
        ledger.verify("changes")
    assert retained_error.value.code == "INVALID_LEDGER_PAYLOAD"


def test_openai_resource_namespace_cannot_be_reassigned_to_another_provider(tmp_path: Path) -> None:
    foreign_namespace = _resource_snapshot(
        "CAMPAIGN",
        "openai_ads.campaign.campaign_fixture",
        ["openai_ads.account.account_fixture"],
        {"currency": "USD"},
    )

    with pytest.raises(AdhelmError) as raised:
        Ledger(tmp_path).append(
            "changes", "RESOURCE_SNAPSHOT", foreign_namespace, provider="meta_ads"
        )

    assert raised.value.code == "INVALID_LEDGER_PAYLOAD"
