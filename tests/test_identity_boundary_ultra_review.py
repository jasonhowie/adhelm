"""Cross-adapter regressions for typed client-ID egress boundaries."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.ga4 import Ga4DataClient, Ga4Identity, Ga4RealtimeQuery, Ga4ReportQuery
from adhelm.providers.linkedin_ads import (
    LinkedInAdsClient,
    LinkedInAdsIdentity,
    LinkedInAdsInventoryQuery,
    LinkedInAdsReportQuery,
)
from adhelm.providers.meta_ads import (
    MetaAdsClient,
    MetaAdsIdentity,
    MetaAdsInventoryQuery,
    MetaAdsReportQuery,
)
from adhelm.providers.reddit_ads import (
    RedditAdsClient,
    RedditAdsIdentity,
    RedditAdsInventoryQuery,
    RedditAdsReportQuery,
)
from adhelm.providers.tiktok_ads import (
    TikTokAdsClient,
    TikTokAdsIdentity,
    TikTokAdsInventoryQuery,
    TikTokAdsReportQuery,
)
from adhelm.transport import SafeHttpClient


def _meta_client(handler: httpx.MockTransport) -> MetaAdsClient:
    return MetaAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"graph.facebook.com"}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="ultra-review",
    )


@pytest.mark.parametrize("operation", ["inventory", "report"])
@pytest.mark.parametrize("account_client_ref", ["act_111/../act_222", "act_222"])
def test_meta_revalidates_direct_identity_before_account_path_egress(
    operation: str, account_client_ref: str
) -> None:
    """A direct typed identity must not change the configured account route."""

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    identity = MetaAdsIdentity(
        account_ref="meta_ads.account.act_111",
        # The first value escapes via path normalization; the second is a
        # syntactically valid different account with forged provenance.
        account_client_ref=account_client_ref,
        timezone="UTC",
        currency="USD",
        status="1",
    )
    client = _meta_client(httpx.MockTransport(handler))

    if operation == "inventory":
        with pytest.raises(AdhelmError):
            client.list_campaigns(
                MetaAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
            )
    else:
        with pytest.raises(AdhelmError):
            client.run_campaign_report(
                MetaAdsReportQuery(
                    account_ref=identity.account_ref,
                    report_id="report:meta-boundary",
                    query_contract_hash="a" * 64,
                    window_start=date(2026, 9, 1),
                    window_end=date(2026, 9, 1),
                    source_timezone="UTC",
                    action_attribution_windows=("7d_click",),
                ),
                identity=identity,
            )

    assert calls == 0


def _linkedin_client(handler: httpx.MockTransport) -> LinkedInAdsClient:
    return LinkedInAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"api.linkedin.com"}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="ultra-review",
    )


@pytest.mark.parametrize("operation", ["inventory", "report"])
@pytest.mark.parametrize("account_client_ref", ["123/../../adAccounts/999", "999"])
def test_linkedin_revalidates_direct_identity_before_account_egress(
    operation: str, account_client_ref: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    identity = LinkedInAdsIdentity(
        account_ref="linkedin_ads.account.123",
        account_client_ref=account_client_ref,
        currency="USD",
    )
    client = _linkedin_client(httpx.MockTransport(handler))
    if operation == "inventory":
        with pytest.raises(AdhelmError):
            client.list_campaigns(
                LinkedInAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
            )
    else:
        with pytest.raises(AdhelmError):
            client.run_campaign_report(
                LinkedInAdsReportQuery(
                    account_ref=identity.account_ref,
                    report_id="report:linkedin-boundary",
                    query_contract_hash="b" * 64,
                    window_start=date(2026, 9, 1),
                    window_end=date(2026, 9, 1),
                ),
                identity=identity,
            )

    assert calls == 0


def _tiktok_client(handler: httpx.MockTransport) -> TikTokAdsClient:
    return TikTokAdsClient(
        SafeHttpClient(
            httpx.Client(transport=handler), {"business-api.tiktok.com"}, read_attempts=1
        ),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="ultra-review",
    )


@pytest.mark.parametrize("operation", ["inventory", "campaign_report", "ad_report"])
def test_tiktok_revalidates_direct_identity_before_advertiser_egress(operation: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    identity = TikTokAdsIdentity(
        account_ref="tiktok_ads.advertiser.123",
        advertiser_client_ref="999",
        timezone="UTC",
        currency="USD",
        status="ACTIVE",
    )
    client = _tiktok_client(httpx.MockTransport(handler))
    if operation == "inventory":
        with pytest.raises(AdhelmError):
            client.list_campaigns(
                TikTokAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
            )
    else:
        query = TikTokAdsReportQuery(
            level="AD" if operation == "ad_report" else "CAMPAIGN",
            account_ref=identity.account_ref,
            report_id="report:tiktok-boundary",
            query_contract_hash="c" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 1),
            source_timezone="UTC",
        )
        with pytest.raises(AdhelmError):
            if operation == "ad_report":
                client.run_ad_report(query, identity=identity)
            else:
                client.run_campaign_report(query, identity=identity)

    assert calls == 0


def _ga4_client(handler: httpx.MockTransport) -> Ga4DataClient:
    return Ga4DataClient(
        SafeHttpClient(
            httpx.Client(transport=handler),
            {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
            read_attempts=1,
        ),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="ultra-review",
    )


@pytest.mark.parametrize("operation", ["standard", "realtime"])
def test_ga4_rejects_forged_account_provenance_before_egress(operation: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    identity = Ga4Identity(
        property_ref="ga4.property.123",
        property_client_ref="123",
        web_stream_ref="ga4.stream.456",
        web_stream_client_ref="456",
        timezone="UTC",
        currency="USD",
    )
    client = _ga4_client(httpx.MockTransport(handler))
    if operation == "standard":
        with pytest.raises(AdhelmError):
            client.run_report(
                Ga4ReportQuery(
                    property_id="123",
                    account_ref="ga4.property.claimed",
                    report_id="report:ga4-boundary",
                    query_contract_hash="d" * 64,
                    window_start=date(2026, 9, 1),
                    window_end=date(2026, 9, 1),
                    metrics=["sessions"],
                    source_timezone="UTC",
                ),
                identity=identity,
            )
    else:
        with pytest.raises(AdhelmError):
            client.run_realtime_report(
                Ga4RealtimeQuery(
                    property_id="123",
                    account_ref="ga4.property.claimed",
                    report_id="report:ga4-boundary",
                    query_contract_hash="d" * 64,
                    allowed_event_names=["approved_event"],
                    source_timezone="UTC",
                ),
                identity=identity,
            )

    assert calls == 0


def _reddit_client(
    handler: httpx.MockTransport,
    *,
    reference_for: object | None = None,
) -> RedditAdsClient:
    options = {} if reference_for is None else {"reference_for": reference_for}
    return RedditAdsClient(
        SafeHttpClient(httpx.Client(transport=handler), {"ads-api.reddit.com"}, read_attempts=1),
        lambda: "fixture-token-not-for-output",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="ultra-review",
        **options,
    )


@pytest.mark.parametrize("operation", ["inventory", "report"])
def test_reddit_rejects_valid_but_mismatched_identity_account(operation: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    identity = RedditAdsIdentity(
        account_ref="reddit_ads.account.a2_claimed",
        account_client_ref="a2_other",
        currency="USD",
    )
    client = _reddit_client(httpx.MockTransport(handler))
    if operation == "inventory":
        with pytest.raises(AdhelmError):
            client.list_campaigns(
                RedditAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
            )
    else:
        with pytest.raises(AdhelmError):
            client.run_campaign_report(
                RedditAdsReportQuery(
                    account_ref=identity.account_ref,
                    report_id="report:reddit-boundary",
                    query_contract_hash="e" * 64,
                    window_start=date(2026, 9, 1),
                    window_end=date(2026, 9, 1),
                ),
                identity=identity,
            )

    assert calls == 0


def test_reddit_identity_boundary_accepts_the_client_custom_reference_function() -> None:
    def reference_for(kind: str, value: str) -> str:
        return f"opaque.{kind}.{value}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/ad_accounts/a2_custom/campaigns"
        return httpx.Response(200, json={"data": [], "pagination": {}})

    client = _reddit_client(httpx.MockTransport(handler), reference_for=reference_for)
    identity = RedditAdsIdentity(
        account_ref="opaque.account.a2_custom",
        account_client_ref="a2_custom",
        currency="USD",
    )
    assert (
        client.list_campaigns(
            RedditAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
        == []
    )
