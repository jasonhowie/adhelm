from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import adhelm.inventory as inventory
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, Profile
from adhelm.providers.meta_ads import MetaAdsCampaign, MetaAdsIdentity

ResponseHandler = Callable[[httpx.Request], httpx.Response]
_REAL_HTTP_CLIENT = httpx.Client


def _profile(provider: str, account_ref: str, *, google: bool = False) -> Profile:
    return Profile(
        profile_id=f"{provider}-read",
        provider=provider,  # type: ignore[arg-type]
        account_ref=account_ref,
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="primary-fixture",
        secondary_credential_ref="developer-fixture" if google else None,
        login_customer_ref="0987654321" if google else None,
    )


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch, handler: ResponseHandler, calls: list[tuple[str, bool]]
) -> None:
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _REAL_HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )

    def credential_fixture(
        profile: Profile, *, ci_enabled: bool = False, secondary: bool = False
    ) -> str:
        calls.append((profile.profile_id, secondary))
        return "developer-fixture" if secondary else "primary-fixture"

    monkeypatch.setattr(inventory, "credential", credential_fixture)
    monkeypatch.setattr(inventory, "export_key", lambda: b"i" * 32)


def _google_identity() -> dict[str, object]:
    return {
        "results": [{"customer": {"id": "1234567890", "currencyCode": "USD", "timeZone": "UTC"}}]
    }


def _google_handler(status: str = "PAUSED") -> ResponseHandler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v25/customers/1234567890/googleAds:search"
        assert request.headers["authorization"] == "Bearer primary-fixture"
        assert request.headers["developer-token"] == "developer-fixture"
        assert request.headers["login-customer-id"] == "0987654321"
        query = json.loads(request.content)["query"]
        if "customer.currency_code" in query:
            return httpx.Response(200, json=_google_identity())
        if "campaign_budget.amount_micros" in query:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "campaign": {
                                "id": "11",
                                "status": status,
                                "advertisingChannelType": "SEARCH",
                                "startDate": "2026-01-01",
                                "endDate": "",
                                "biddingStrategyType": "MANUAL_CPC",
                            },
                            "campaignBudget": {"amountMicros": "1000000"},
                        }
                    ]
                },
            )
        if "campaign_criterion.type = 'AD_SCHEDULE'" in query:
            return httpx.Response(200, json={"results": []})
        if "ad_group.cpc_bid_micros" in query:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "campaign": {"id": "11"},
                            "adGroup": {
                                "id": "21",
                                "status": status,
                                "type": "SEARCH_STANDARD",
                                "cpcBidMicros": "1000000",
                            },
                        }
                    ]
                },
            )
        assert "ad_group_ad.ad.type" in query
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "campaign": {"id": "11"},
                        "adGroup": {"id": "21"},
                        "adGroupAd": {
                            "ad": {"id": "31", "type": "RESPONSIVE_SEARCH_AD"},
                            "status": status,
                        },
                    }
                ]
            },
        )

    return handler


def test_google_inventory_normalizes_complete_reads_and_records_unknown_remote_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path, Configuration(profiles=[_profile("google_ads", "1234567890", google=True)])
    )
    credential_calls: list[tuple[str, bool]] = []
    _patch_runtime(monkeypatch, _google_handler(), credential_calls)
    first = inventory.run_inventory(tmp_path, "google_ads", "google_ads-read")

    assert first["resource_counts"] == {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1}
    assert first["account_ref"].startswith("opaque:")
    assert "1234567890" not in json.dumps(first)
    assert first["change_record_digests"] == []
    assert any(secondary for _, secondary in credential_calls)
    # The ad snapshot keeps `CREATIVE_NOT_EXPOSED` and adds the read-boundary
    # and presence facts the shared closed state model has no field for.  The
    # fixture ad returned no responsive-search creative, no final URL and no
    # policy summary, so each of those is recorded as an explicit absence
    # rather than omitted.
    assert first["resources"][-1]["data_quality_statuses"] == [
        "CREATIVE_NOT_EXPOSED",
        # The connector state-shape version travels with every Google Ads
        # snapshot so a later reader can tell a retention change from a change
        # at Google.  It lives here, never in `normalized_state`, so recording
        # it creates no drift of its own.
        "GOOGLE_ADS_STATE_SHAPE_2026-09-09.1",
        "GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED",
        "GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED",
        "GOOGLE_ADS_FINAL_URL_ABSENT",
        "GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED",
        "GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED",
    ]

    _patch_runtime(monkeypatch, _google_handler("ENABLED"), credential_calls)
    second = inventory.run_inventory(tmp_path, "google_ads", "google_ads-read")
    assert len(second["change_record_digests"]) == 3
    changes = [
        record
        for record in Ledger(tmp_path).records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
    ]
    assert len(changes) == 3
    for change in changes:
        assert change["payload"]["origin"] == "UNKNOWN"
        assert change["payload"]["actor_ref"] is None
        assert change["payload"]["field_deltas"][0]["path"] == "normalized_state.status"
        assert change["payload"]["observation_interval_start"]
        assert change["payload"]["observation_interval_end"]


def test_meta_inventory_reads_all_three_fixed_collections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("meta_ads", "123")]))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(
                200,
                json={"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"},
            )
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "11",
                            "effective_status": "PAUSED",
                            "objective": "OUTCOME_TRAFFIC",
                            "daily_budget": "500",
                            "lifetime_budget": None,
                            "spend_cap": "0",
                            "start_time": "2026-01-01T00:00:00+00:00",
                            "stop_time": None,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/adsets"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "12",
                            "campaign_id": "11",
                            "effective_status": "PAUSED",
                            "billing_event": "IMPRESSIONS",
                            "bid_amount": "5",
                            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                            "daily_budget": "500",
                            "lifetime_budget": None,
                            "start_time": "2026-01-01T00:00:00+00:00",
                            "end_time": None,
                        }
                    ]
                },
            )
        assert request.url.path.endswith("/ads")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "13",
                        "campaign_id": "11",
                        "adset_id": "12",
                        "effective_status": "PAUSED",
                    }
                ]
            },
        )

    credential_calls: list[tuple[str, bool]] = []
    _patch_runtime(monkeypatch, handler, credential_calls)
    result = inventory.run_inventory(tmp_path, "meta_ads", "meta_ads-read")
    assert result["resource_counts"] == {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1}
    assert result["diagnostics"] == ["CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER"]
    snapshots = [
        record
        for record in Ledger(tmp_path).records("changes")
        if record["record_kind"] == "RESOURCE_SNAPSHOT"
    ]
    assert len(snapshots) == 4
    assert snapshots[0]["payload"]["parent_resource_refs"] == []
    assert snapshots[1]["payload"]["parent_resource_refs"] == [
        snapshots[0]["payload"]["resource_ref"]
    ]
    assert snapshots[2]["payload"]["parent_resource_refs"] == ["meta_ads.campaign.11"]
    for snapshot in snapshots:
        state = snapshot["payload"]["normalized_state"]
        if any(
            state.get(field) is not None
            for field in ("budget_minor", "spend_cap_minor", "bid_amount_minor")
        ):
            assert state["currency"] == "USD"


def test_ledger_rejects_minor_unit_state_without_currency(tmp_path: Path) -> None:
    state = {"status": "PAUSED", "budget_minor": 500}
    payload = {
        "snapshot_id": "snapshot:currency-required",
        "provider_api_version": "fixture-v1",
        "connector_version": "fixture-v1",
        "resource_type": "CAMPAIGN",
        "resource_ref": "fixture.campaign.1",
        "parent_resource_refs": ["fixture.account.1"],
        "remote_fingerprint": digest(state),
        "normalized_state": state,
        "normalized_state_digest": digest(state),
        "effective_status": "PAUSED",
    }

    with pytest.raises(AdhelmError) as raised:
        Ledger(tmp_path).append("changes", "RESOURCE_SNAPSHOT", payload)
    assert raised.value.code == "INVALID_LEDGER_PAYLOAD"


def test_meta_snapshot_preserves_lifetime_and_simultaneous_budgets() -> None:
    identity = MetaAdsIdentity(
        account_ref="meta_ads.account.act_123",
        account_client_ref="123",
        timezone="UTC",
        currency="USD",
        status="1",
    )
    lifetime_campaign = MetaAdsCampaign(
        campaign_ref="meta_ads.campaign.11",
        campaign_client_ref="11",
        status="PAUSED",
        lifetime_budget=Money(minor_units=900, currency="USD"),
    )
    snapshots = inventory._meta_snapshots(identity, [lifetime_campaign], [], [])
    state = snapshots[1]["normalized_state"]
    assert state["budget_minor"] == 900
    assert state["budget_type"] == "LIFETIME"
    assert state["currency"] == "USD"

    ambiguous_campaign = lifetime_campaign.model_copy(
        update={"daily_budget": Money(minor_units=500, currency="USD")}
    )
    both = inventory._meta_snapshots(identity, [ambiguous_campaign], [], [])
    state = both[1]["normalized_state"]
    assert state["daily_budget_minor"] == 500
    assert state["lifetime_budget_minor"] == 900
    assert "budget_minor" not in state and "budget_type" not in state


def test_meta_snapshot_retains_the_campaign_bid_strategy_and_its_state_shape_version() -> None:
    """`bid_strategy` is a real retained state key, not a data-quality status.

    The shared closed `NormalizedResourceState` declares the field, so the
    campaign bid strategy is retained in `normalized_state` and read back from
    there by the provider audit.  Because that changed what this connector
    retains, every Meta snapshot also records the connector state-shape version
    -- in `data_quality_statuses`, never in `normalized_state`, so recording it
    creates no drift of its own.
    """
    from adhelm.providers.meta_ads import META_ADS_STATE_SHAPE_STATUS

    identity = MetaAdsIdentity(
        account_ref="meta_ads.account.act_123",
        account_client_ref="123",
        timezone="UTC",
        currency="USD",
        status="1",
    )
    campaign = MetaAdsCampaign(
        campaign_ref="meta_ads.campaign.11",
        campaign_client_ref="11",
        status="ACTIVE",
        objective="OUTCOME_TRAFFIC",
        buying_type="AUCTION",
        bid_strategy="COST_CAP",
        daily_budget=Money(minor_units=5_000, currency="USD"),
    )
    snapshots = inventory._meta_snapshots(identity, [campaign], [], [])
    assert snapshots[1]["normalized_state"]["bid_strategy"] == "COST_CAP"
    # The buying type has no shared state field, so it stays a status.
    assert "buying_type" not in snapshots[1]["normalized_state"]
    assert "META_BUYING_TYPE_RETURNED" in snapshots[1]["data_quality_statuses"]
    for snapshot in snapshots:
        assert META_ADS_STATE_SHAPE_STATUS in snapshot["data_quality_statuses"]
        assert META_ADS_STATE_SHAPE_STATUS not in snapshot["normalized_state"]


def test_openai_inventory_retains_complete_native_hierarchy_and_explicit_state_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path, Configuration(profiles=[_profile("openai_ads", "account_fixture")])
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ad_account":
            return httpx.Response(
                200,
                json={
                    "id": "account_fixture",
                    "name": "ignored locally",
                    "status": "active",
                    "timezone": "UTC",
                    "currency_code": "USD",
                },
            )
        if request.url.path == "/v1/ad_groups":
            assert request.url.params["campaign_id"] == "campaign_fixture"
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "group_fixture",
                            "status": "paused",
                            "bidding_config": {
                                "billing_event_type": "impression",
                                "max_bid_micros": 125_000,
                            },
                        }
                    ],
                    "last_id": "group_fixture",
                    "has_more": False,
                },
            )
        if request.url.path == "/v1/ads":
            assert request.url.params["ad_group_id"] == "group_fixture"
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "ad_fixture",
                            "status": "paused",
                            "creative": {
                                "type": "chat_card",
                                "title": "not persisted",
                                "file_id": "file_fixture",
                            },
                        }
                    ],
                    "last_id": "ad_fixture",
                    "has_more": False,
                },
            )
        assert request.url.path == "/v1/campaigns"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "campaign_fixture",
                        "created_at": 1_788_260_800,
                        "status": "paused",
                        "bidding_type": "impressions",
                        "budget": {},
                        "conversion_event_setting_ids": [],
                        "description": "not persisted",
                        "end_time": None,
                        "mode": None,
                        "name": "not persisted",
                        "start_time": 1_788_260_800,
                        "targeting": {},
                        "updated_at": 1_788_260_800,
                    }
                ],
                "first_id": "campaign_fixture",
                "last_id": "campaign_fixture",
                "has_more": False,
            },
        )

    credential_calls: list[tuple[str, bool]] = []
    _patch_runtime(monkeypatch, handler, credential_calls)
    result = inventory.run_inventory(tmp_path, "openai_ads", "openai_ads-read")
    assert result["resource_counts"] == {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1}
    assert result["diagnostics"] == [
        "MINOR_UNIT_PROJECTION_REQUIRES_EXACT_USD_AMOUNT",
        "CREATIVE_BINDING_UNVERIFIED",
    ]
    assert "not persisted" not in json.dumps(result)


def test_pagination_failure_writes_no_partial_snapshot_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("meta_ads", "123")]))
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(
                200,
                json={"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"},
            )
        assert request.url.path.endswith("/campaigns")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "11",
                        "effective_status": "PAUSED",
                        "objective": "OUTCOME_TRAFFIC",
                        "daily_budget": "500",
                        "lifetime_budget": None,
                        "spend_cap": "0",
                        "start_time": None,
                        "stop_time": None,
                    }
                ],
                "paging": {
                    "cursors": {"before": "before", "after": "loop"},
                    "next": "https://graph.facebook.com/v26.0/act_123/campaigns?after=loop",
                },
            },
        )

    calls: list[tuple[str, bool]] = []
    _patch_runtime(monkeypatch, handler, calls)
    with pytest.raises(AdhelmError) as raised:
        inventory.run_inventory(tmp_path, "meta_ads", "meta_ads-read", maximum_records=2)
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert requests == 3
    assert not list((tmp_path / "ledger" / "changes").glob("*.jsonl"))
