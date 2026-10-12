from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import adhelm.inventory as inventory
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.providers.tiktok_ads import TIKTOK_ADS_STATE_SHAPE_STATUS

ResponseHandler = Callable[[httpx.Request], httpx.Response]
_REAL_HTTP_CLIENT = httpx.Client


def _profile(provider: str, account_ref: str, *, timezone: str = "UTC") -> Profile:
    return Profile(
        profile_id=f"{provider}-read",
        provider=provider,  # type: ignore[arg-type]
        account_ref=account_ref,
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone=timezone,
        credential_ref="fixture-read-token",
    )


def _runtime(monkeypatch: pytest.MonkeyPatch, handler: ResponseHandler) -> None:
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _REAL_HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(inventory, "credential", lambda *_args, **_kwargs: "fixture-read-token")
    monkeypatch.setattr(inventory, "export_key", lambda: b"k" * 32)


def test_tiktok_inventory_persists_verified_modes_and_native_hierarchy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("tiktok_ads", "123")]))

    def page(items: list[dict[str, object]]) -> dict[str, object]:
        return {
            "code": 0,
            "message": "OK",
            "request_id": "fixture-request",
            "data": {
                "list": items,
                "page_info": {
                    "page": 1,
                    "page_size": 500,
                    "total_number": len(items),
                    "total_page": 1,
                },
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["access-token"] == "fixture-read-token"
        if request.url.path.endswith("/advertiser/info/"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "OK",
                    "request_id": "fixture-request",
                    "data": {
                        "list": [
                            {
                                "advertiser_id": "123",
                                "currency": "USD",
                                "timezone": "UTC",
                                "status": "STATUS_ENABLE",
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("/campaign/get/"):
            return httpx.Response(
                200,
                json=page(
                    [
                        {
                            "campaign_id": "11",
                            "operation_status": "ENABLE",
                            "objective_type": "TRAFFIC",
                            "budget_mode": "BUDGET_MODE_DAY",
                            "budget": "5.00",
                        }
                    ]
                ),
            )
        if request.url.path.endswith("/adgroup/get/"):
            return httpx.Response(
                200,
                json=page(
                    [
                        {
                            "adgroup_id": "12",
                            "campaign_id": "11",
                            "operation_status": "ENABLE",
                            "budget_mode": "BUDGET_MODE_INFINITE",
                            "budget": None,
                            "bid_type": "BID_TYPE_NO_BID",
                            "bid_price": None,
                        }
                    ]
                ),
            )
        assert request.url.path.endswith("/ad/get/")
        return httpx.Response(
            200,
            json=page(
                [
                    {
                        "ad_id": "13",
                        "adgroup_id": "12",
                        "campaign_id": "11",
                        "operation_status": "ENABLE",
                    }
                ]
            ),
        )

    _runtime(monkeypatch, handler)
    result = inventory.run_inventory(tmp_path, "tiktok_ads", "tiktok_ads-read")

    assert result["resource_counts"] == {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1}
    snapshots = [
        item["payload"]
        for item in Ledger(tmp_path).records("changes")
        if item["record_kind"] == "RESOURCE_SNAPSHOT"
    ]
    campaign = next(item for item in snapshots if item["resource_type"] == "CAMPAIGN")
    adgroup = next(item for item in snapshots if item["resource_type"] == "AD_GROUP")
    assert campaign["normalized_state"].get("tiktok_ads_budget_mode") == "BUDGET_MODE_DAY"
    assert campaign["normalized_state"]["budget_type"] == "DAILY"
    assert adgroup["normalized_state"] == {
        "status": "ENABLE",
        "currency": "USD",
        "bid_strategy": "BID_TYPE_NO_BID",
        "tiktok_ads_budget_mode": "BUDGET_MODE_INFINITE",
    }
    # The pre-existing budget statuses are unchanged; the additive presence,
    # enum and state-shape facts the provider audit reads sit beside them.
    assert adgroup["data_quality_statuses"] == [
        "BUDGET_MODE_INFINITE",
        TIKTOK_ADS_STATE_SHAPE_STATUS,
        "TIKTOK_BUDGET_MODE_INFINITE",
        "TIKTOK_BUDGET_AMOUNT_ABSENT",
        "TIKTOK_BID_TYPE_RETURNED",
        "TIKTOK_BID_PRICE_ABSENT",
        "TIKTOK_OPERATION_STATUS_REVIEWED",
    ]
    assert result["account_ref"].startswith("opaque:")
    assert all(item["resource_ref"].startswith("opaque:") for item in result["resources"])


def test_linkedin_inventory_keeps_campaign_groups_without_relabeling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("linkedin_ads", "456")]))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fixture-read-token"
        if request.url.path == "/rest/adAccounts/456":
            return httpx.Response(200, json={"id": 456, "currency": "USD", "status": "ACTIVE"})
        if request.url.path.endswith("/adCampaignGroups"):
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {"account": "urn:li:sponsoredAccount:456", "id": 1, "status": "ACTIVE"}
                    ],
                    "metadata": {},
                },
            )
        if request.url.path.endswith("/adCampaigns"):
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "account": "urn:li:sponsoredAccount:456",
                            "id": 2,
                            "campaignGroup": "urn:li:sponsoredCampaignGroup:1",
                            "status": "ACTIVE",
                            "objectiveType": "WEBSITE_VISITS",
                        }
                    ],
                    "metadata": {},
                },
            )
        assert request.url.path.endswith("/creatives")
        assert request.headers["x-restli-method"] == "FINDER"
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "account": "urn:li:sponsoredAccount:456",
                        "campaign": "urn:li:sponsoredCampaign:2",
                        "id": "urn:li:sponsoredCreative:3",
                        "intendedStatus": "ACTIVE",
                    }
                ],
                "metadata": {},
            },
        )

    _runtime(monkeypatch, handler)
    result = inventory.run_inventory(tmp_path, "linkedin_ads", "linkedin_ads-read")

    assert result["resource_counts"] == {
        "ACCOUNT": 1,
        "CAMPAIGN": 1,
        "AD_GROUP": 0,
        "AD": 1,
        "CAMPAIGN_GROUP": 1,
    }
    assert result["identity"]["timezone"] is None
    snapshots = [
        item["payload"]
        for item in Ledger(tmp_path).records("changes")
        if item["record_kind"] == "RESOURCE_SNAPSHOT"
    ]
    campaign = next(item for item in snapshots if item["resource_type"] == "CAMPAIGN")
    creative = next(item for item in snapshots if item["resource_type"] == "AD")
    assert campaign["parent_resource_refs"] == ["linkedin_ads.campaign_group.1"]
    assert creative["parent_resource_refs"] == ["linkedin_ads.campaign.2"]
    assert result["account_ref"].startswith("opaque:")
    assert all(item["resource_ref"].startswith("opaque:") for item in result["resources"])
    assert all(
        all(parent.startswith("opaque:") for parent in item["parent_resource_refs"])
        for item in result["resources"]
    )


def test_reddit_inventory_reads_all_native_collections_before_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("reddit_ads", "a2_fixture")]))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fixture-read-token"
        if request.url.path == "/api/v3/ad_accounts/a2_fixture":
            return httpx.Response(
                200, json={"data": {"id": "a2_fixture", "currency": "USD", "time_zone_id": "UTC"}}
            )
        values = {
            "/campaigns": {
                "id": "c1",
                "ad_account_id": "a2_fixture",
                "effective_status": "PAUSED",
                "objective": "TRAFFIC",
            },
            "/ad_groups": {
                "id": "g1",
                "ad_account_id": "a2_fixture",
                "campaign_id": "c1",
                "effective_status": "PAUSED",
            },
            "/ads": {
                "id": "ad1",
                "ad_account_id": "a2_fixture",
                "campaign_id": "c1",
                "ad_group_id": "g1",
                "effective_status": "PAUSED",
            },
        }
        for suffix, item in values.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json={"data": [item], "pagination": {}})
        raise AssertionError(request.url)

    _runtime(monkeypatch, handler)
    result = inventory.run_inventory(tmp_path, "reddit_ads", "reddit_ads-read")
    assert result["resource_counts"] == {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1}
    assert len(result["snapshot_record_digests"]) == 4
    assert result["account_ref"].startswith("opaque:")
    assert all(item["resource_ref"].startswith("opaque:") for item in result["resources"])


@pytest.mark.parametrize("provider", ["google_ads", "meta_ads", "tiktok_ads", "reddit_ads"])
def test_standard_hierarchy_rejects_ad_without_observed_adgroup(provider: str) -> None:
    snapshots = [
        inventory._snapshot(
            provider_api_version="fixture",
            resource_type="ACCOUNT",
            resource_ref="fixture.account",
            parents=[],
            state={"status": "ACTIVE"},
            status="ACTIVE",
        ),
        inventory._snapshot(
            provider_api_version="fixture",
            resource_type="CAMPAIGN",
            resource_ref="fixture.campaign",
            parents=["fixture.account"],
            state={"status": "ACTIVE"},
            status="ACTIVE",
        ),
        inventory._snapshot(
            provider_api_version="fixture",
            resource_type="AD",
            resource_ref="fixture.ad",
            parents=["fixture.campaign", "fixture.adgroup"],
            state={"status": "ACTIVE"},
            status="ACTIVE",
        ),
    ]
    with pytest.raises(AdhelmError, match="incomplete resource hierarchy"):
        inventory._validate_snapshot_hierarchy(snapshots, provider)  # type: ignore[arg-type]


def test_tiktok_budget_mode_cannot_be_written_under_another_provider(tmp_path: Path) -> None:
    state = {"status": "PAUSED", "tiktok_ads_budget_mode": "BUDGET_MODE_DAY"}
    payload = inventory._snapshot(
        provider_api_version="fixture",
        resource_type="CAMPAIGN",
        resource_ref="fixture.campaign",
        parents=["fixture.account"],
        state=state,
        status="PAUSED",
    )
    with pytest.raises(AdhelmError) as raised:
        Ledger(tmp_path).append("changes", "RESOURCE_SNAPSHOT", payload, provider="meta_ads")
    assert raised.value.code == "INVALID_LEDGER_PAYLOAD"
    ledger = Ledger(tmp_path)
    ledger.append("changes", "RESOURCE_SNAPSHOT", payload, provider="tiktok_ads")
    assert ledger.verify("changes")["status"] == "VERIFIED"

    # The same provider binding runs during a retained-ledger scan, not merely
    # at append time.  Recompute the ordinary digest after this simulated
    # envelope substitution so the provider check, rather than hash mismatch,
    # is what rejects it.
    record = ledger.records("changes")[-1]
    record["provider"] = "meta_ads"
    hashed = dict(record)
    hashed.pop("record_digest")
    record["record_digest"] = digest(hashed)
    with pytest.raises(AdhelmError) as scan_error:
        ledger._verify_record(
            record, "changes", record["stream_sequence"], record["previous_record_digest"]
        )
    assert scan_error.value.code == "INVALID_LEDGER_PAYLOAD"
