"""Read-only, bounded provider inventory into normalized local snapshots.

This workflow deliberately has no generic provider query surface.  All remote
calls are delegated to the adapters' fixed inventory operations and every
selected resource collection completes before this module writes local history.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from pydantic import ValidationError

from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.credentials import credential, export_key
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.meta_ads_token_read_session import configured_meta_ads_token_read_session
from adhelm.models import (
    Configuration,
    ConfiguredProfile,
    MetaAccountTokenProfile,
    OpenAIAccountKeyProfile,
    Profile,
    validate_configuration,
    validate_configured_profile,
)
from adhelm.projections import _redact_payload
from adhelm.providers.google_ads import (
    GOOGLE_ADS_AD_SCHEDULE_ABSENT,
    GOOGLE_ADS_AD_SCHEDULE_PRESENT,
    GOOGLE_ADS_BUDGET_DELIVERY_METHOD_NOT_RETURNED,
    GOOGLE_ADS_BUDGET_DELIVERY_METHOD_PREFIX,
    GOOGLE_ADS_BUDGET_EXPLICITLY_SHARED,
    GOOGLE_ADS_BUDGET_NOT_EXPLICITLY_SHARED,
    GOOGLE_ADS_BUDGET_NOT_RETURNED,
    GOOGLE_ADS_BUDGET_PERIOD_PREFIX,
    GOOGLE_ADS_BUDGET_RETURNED,
    GOOGLE_ADS_BUDGET_SHARING_NOT_RETURNED,
    GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED,
    GOOGLE_ADS_BUDGET_TYPE_RETURNED,
    GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE,
    GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
    GOOGLE_ADS_FINAL_URL_ABSENT,
    GOOGLE_ADS_FINAL_URL_PRESENT,
    GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED,
    GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED,
    GOOGLE_ADS_NEGATIVE_GEO_TARGET_TYPE_PREFIX,
    GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
    GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED,
    GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED,
    GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
    GOOGLE_ADS_PAGINATION_TOTAL_DECLARED,
    GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED,
    GOOGLE_ADS_POLICY_APPROVAL_PREFIX,
    GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED,
    GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
    GOOGLE_ADS_POLICY_REVIEW_PREFIX,
    GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED,
    GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED,
    GOOGLE_ADS_POSITIVE_GEO_TARGET_TYPE_PREFIX,
    GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED,
    GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
    GOOGLE_ADS_RSA_DESCRIPTION_MINIMUM,
    GOOGLE_ADS_RSA_DESCRIPTIONS_ABSENT,
    GOOGLE_ADS_RSA_DESCRIPTIONS_AT_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_DESCRIPTIONS_PRESENT,
    GOOGLE_ADS_RSA_HEADLINE_MINIMUM,
    GOOGLE_ADS_RSA_HEADLINES_ABSENT,
    GOOGLE_ADS_RSA_HEADLINES_AT_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_HEADLINES_PRESENT,
    GOOGLE_ADS_STATE_SHAPE_STATUS,
    GOOGLE_ADS_TEST_ACCOUNT_DECLARED,
    GOOGLE_ADS_TEST_ACCOUNT_FLAG_NOT_RETURNED,
    GoogleAdsAd,
    GoogleAdsCampaign,
    GoogleAdsClient,
    GoogleAdsIdentity,
    GoogleAdsInventory,
    GoogleAdsInventoryQuery,
    GoogleAdsReadConfig,
)
from adhelm.providers.linkedin_ads import (
    LinkedInAdsCampaign,
    LinkedInAdsCampaignGroup,
    LinkedInAdsClient,
    LinkedInAdsCreative,
    LinkedInAdsIdentity,
    LinkedInAdsInventoryQuery,
)
from adhelm.providers.meta_ads import (
    META_ADS_STATE_SHAPE_STATUS,
    MetaAdsAd,
    MetaAdsAdSet,
    MetaAdsCampaign,
    MetaAdsClient,
    MetaAdsIdentity,
    MetaAdsInventoryQuery,
    meta_ads_ad_quality_statuses,
    meta_ads_ad_set_quality_statuses,
    meta_ads_campaign_quality_statuses,
)
from adhelm.providers.openai_ads import (
    OpenAIAdsAd,
    OpenAIAdsAdGroup,
    OpenAIAdsCampaign,
    OpenAIAdsClient,
    OpenAIAdsHierarchyInventoryQuery,
    OpenAIAdsIdentity,
    openai_ads_ad_group_quality_statuses,
    openai_ads_ad_quality_statuses,
    openai_ads_campaign_quality_statuses,
)
from adhelm.providers.reddit_ads import (
    RedditAdsAd,
    RedditAdsAdGroup,
    RedditAdsCampaign,
    RedditAdsClient,
    RedditAdsIdentity,
    RedditAdsInventoryQuery,
)
from adhelm.providers.tiktok_ads import (
    TIKTOK_ADS_PAGINATION_TOTAL_DECLARED,
    TIKTOK_ADS_STATE_SHAPE_STATUS,
    TikTokAdsAd,
    TikTokAdsAdGroup,
    TikTokAdsCampaign,
    TikTokAdsClient,
    TikTokAdsIdentity,
    TikTokAdsInventoryQuery,
    tiktok_ads_ad_group_quality_statuses,
    tiktok_ads_ad_quality_statuses,
    tiktok_ads_campaign_quality_statuses,
)
from adhelm.transport import SafeHttpClient

InventoryProvider = Literal[
    "google_ads",
    "meta_ads",
    "linkedin_ads",
    "tiktok_ads",
    "reddit_ads",
    "openai_ads",
]
IdentityOutput = dict[str, str | None]
OperationalInventoryProfile = Profile | OpenAIAccountKeyProfile | MetaAccountTokenProfile


def _configuration(root: Path) -> Configuration:
    """Detach configuration before key acquisition or any provider read."""
    try:
        return validate_configuration(load_configuration(root))
    except AdhelmError:
        raise
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=2,
            next_action="Compare configuration with `adhelm schema config`.",
        ) from exc


def run_inventory(
    root: Path,
    provider: InventoryProvider,
    profile_id: str,
    *,
    ephemeral: bool = False,
    ci_enabled: bool = False,
    maximum_records: int = 500,
) -> dict[str, Any]:
    """Read one configured provider, then append its normalized snapshots.

    The pre-read ledger vector and private export key are acquired before any
    provider egress.  A failed identity or any incomplete page causes no
    snapshot/change append, so partial remote inventory never becomes history.
    """
    if (
        not isinstance(maximum_records, int)
        or isinstance(maximum_records, bool)
        or not 1 <= maximum_records <= 500
    ):
        raise AdhelmError(
            "RESULT_LIMIT_REQUIRED",
            "Inventory maximum records must be between one and 500.",
            exit_code=2,
        )
    configuration = _configuration(root)
    profile = _read_profile(configuration.profiles, provider, profile_id)
    if isinstance(profile, MetaAccountTokenProfile) and ci_enabled:
        raise AdhelmError(
            "META_TOKEN_INTERACTIVE_STORE_REQUIRED",
            "Meta token profiles require the configured operating-system credential store.",
            exit_code=4,
        )
    ledger = Ledger(root)
    source_checkpoint = ledger.checkpoint()
    # The read boundary is recorded from here so a later reader can tell a
    # finished read from the durable prefix an interrupted append loop leaves.
    read_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    key = export_key()
    if isinstance(profile, MetaAccountTokenProfile):
        with configured_meta_ads_token_read_session(
            root, profile.profile_id, expected_configuration_digest=digest(configuration)
        ) as session:
            campaigns, adsets, ads = session.list_inventory(maximum_records)
            meta_identity = session.identity
            snapshots = _meta_snapshots(meta_identity, campaigns, adsets, ads)
            # Acquire configuration then credential locks before any ledger
            # operation. The callback uses these held locks and never reads
            # history itself. Every durable append rechecks continuity/expiry.
            with session.retention_recheck() as recheck:
                recheck()
                return _complete_inventory(
                    ledger,
                    provider,
                    profile.account_ref,
                    snapshots,
                    _identity_output(meta_identity),
                    source_checkpoint,
                    key,
                    ephemeral=ephemeral,
                    before_append=recheck,
                    read_started_at=read_started_at,
                )
    # Hold one Google primary-token snapshot across discovery and all
    # inventory pages.  A protected refresh therefore cannot change identity
    # binding partway through a sequential normalized observation.
    google_primary_token: str | None = None
    if provider == "google_ads":
        google_primary_token = credential(profile, ci_enabled=ci_enabled)

    def token_getter() -> str:
        if google_primary_token is not None:
            return google_primary_token
        return credential(profile, ci_enabled=ci_enabled)

    with httpx.Client(trust_env=False) as client:
        if provider == "google_ads":
            assert isinstance(profile, Profile)
            assert google_primary_token is not None
            snapshots, identity = _google_inventory(client, profile, token_getter, maximum_records)
        elif provider == "meta_ads":
            assert isinstance(profile, Profile)
            snapshots, identity = _meta_inventory(client, profile, token_getter, maximum_records)
        elif provider == "linkedin_ads":
            assert isinstance(profile, Profile)
            snapshots, identity = _linkedin_inventory(
                client, profile, token_getter, maximum_records
            )
        elif provider == "tiktok_ads":
            assert isinstance(profile, Profile)
            snapshots, identity = _tiktok_inventory(client, profile, token_getter, maximum_records)
        elif provider == "reddit_ads":
            assert isinstance(profile, Profile)
            snapshots, identity = _reddit_inventory(client, profile, token_getter, maximum_records)
        else:
            snapshots, identity = _openai_inventory(client, profile, token_getter, maximum_records)

    return _complete_inventory(
        ledger,
        provider,
        profile.account_ref,
        snapshots,
        identity,
        source_checkpoint,
        key,
        ephemeral=ephemeral,
        read_started_at=read_started_at,
    )


def _complete_inventory(
    ledger: Ledger,
    provider: InventoryProvider,
    account_ref: str,
    snapshots: list[dict[str, Any]],
    identity: IdentityOutput,
    source_checkpoint: dict[str, Any],
    key: bytes,
    *,
    ephemeral: bool,
    read_started_at: str,
    before_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    # A paginated inventory is one observed hierarchy.  Do not persist a
    # partial or internally inconsistent point-in-time view merely because its
    # individual provider collection reads succeeded.
    _validate_snapshot_hierarchy(snapshots, provider)

    appended: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    if not ephemeral:
        # All responses have been parsed and normalized before this first append.
        pending_changes = [
            _change_for_snapshot(ledger, item, provider, account_ref) for item in snapshots
        ]
        for snapshot, change in zip(snapshots, pending_changes, strict=True):
            record = ledger.append(
                "changes",
                "RESOURCE_SNAPSHOT",
                snapshot,
                provider=provider,
                account_ref=account_ref,
                _before_durable_append=before_append,
            )
            appended.append(record)
            if change is not None:
                event = {
                    **change,
                    "after_snapshot_ref": record["record_id"],
                    "observation_interval_end": record["observed_at"],
                }
                changed.append(
                    ledger.append(
                        "changes",
                        "CHANGE_EVENT",
                        event,
                        provider=provider,
                        account_ref=account_ref,
                        _before_durable_append=before_append,
                    )
                )

    resource_counts = _counts(snapshots)
    completion: dict[str, Any] | None = None
    if appended:
        # The read boundary is appended last, after every snapshot and change
        # record of this read is durable.  A reader that finds snapshots with no
        # marker has a prefix, not a finished read, and must not treat the
        # resources it never reached as externally deleted.
        completion = ledger.append(
            "changes",
            "INVENTORY_READ_COMPLETED",
            {
                "schema_version": 1,
                "provider": provider,
                "account_ref": account_ref,
                "snapshot_record_digests": [record["record_digest"] for record in appended],
                "resource_counts": resource_counts,
                "read_started_at": read_started_at,
                "read_finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            provider=provider,
            account_ref=account_ref,
            _before_durable_append=before_append,
        )
    safe_snapshots = [
        _redact_payload(
            {
                "resource_type": snapshot["resource_type"],
                "resource_ref": snapshot["resource_ref"],
                "parent_resource_refs": snapshot["parent_resource_refs"],
                "normalized_state": snapshot["normalized_state"],
                "effective_status": snapshot["effective_status"],
                "data_quality_statuses": snapshot["data_quality_statuses"],
            },
            key,
            False,
        )
        for snapshot in snapshots
    ]
    return {
        "status": "INVENTORY_COMPLETE",
        "provider": provider,
        "account_ref": _redact_payload(account_ref, key, False, "account_ref"),
        "identity": {
            "timezone": identity["timezone"],
            "currency": identity["currency"],
            "status": identity["status"],
        },
        "resource_counts": resource_counts,
        "resources": safe_snapshots,
        "diagnostics": _diagnostics(provider),
        "source_checkpoint_digest": source_checkpoint["checkpoint_digest"],
        "snapshot_record_digests": [record["record_digest"] for record in appended],
        "change_record_digests": [record["record_digest"] for record in changed],
        "read_completion_record_digest": (
            completion["record_digest"] if completion is not None else None
        ),
        "ephemeral": ephemeral,
    }


def _read_profile(
    profiles: list[ConfiguredProfile], provider: InventoryProvider, profile_id: str
) -> OperationalInventoryProfile:
    selected = [
        profile
        for profile in profiles
        if profile.profile_id == profile_id
        and profile.provider == provider
        and profile.purpose == "READ"
    ]
    if len(selected) != 1:
        raise AdhelmError(
            "READ_PROFILE_REQUIRED",
            "An explicit matching read profile is required.",
            next_action="Configure the selected provider with a READ profile.",
        )
    profile = validate_configured_profile(selected[0])
    if isinstance(profile, MetaAccountTokenProfile):
        return profile
    if provider != "openai_ads" and not isinstance(profile, Profile):
        raise AdhelmError(
            "SCOPED_PROFILE_REQUIRED", "This provider requires a scoped OAuth profile."
        )
    return profile


def _google_inventory(
    client: httpx.Client,
    profile: Profile,
    token_getter: Any,
    maximum_records: int,
) -> tuple[list[dict[str, Any]], IdentityOutput]:
    config = GoogleAdsReadConfig(
        account_ref=profile.account_ref,
        customer_client_ref=profile.account_ref,
        login_customer_client_ref=profile.login_customer_ref,
    )
    adapter = GoogleAdsClient(SafeHttpClient(client, {"googleads.googleapis.com"}), token_getter)
    identity = adapter.discover_identity(config)
    _verify_identity(profile, identity.timezone, identity.currency)
    inventory = adapter.inventory(
        config,
        GoogleAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=maximum_records),
        identity=identity,
    )
    return _google_snapshots(inventory, identity), _identity_output(identity)


def _meta_inventory(
    client: httpx.Client,
    profile: Profile,
    token_getter: Any,
    maximum_records: int,
) -> tuple[list[dict[str, Any]], IdentityOutput]:
    adapter = MetaAdsClient(SafeHttpClient(client, {"graph.facebook.com"}), token_getter)
    identity = adapter.discover_identity(
        profile.account_ref, expected_account_client_ref=profile.account_ref
    )
    _verify_identity(profile, identity.timezone, identity.currency)
    query = MetaAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=maximum_records)
    # Each fixed collection must complete before the workflow builds/appends snapshots.
    campaigns = adapter.list_campaigns(query, identity=identity)
    adsets = adapter.list_adsets(query, identity=identity)
    ads = adapter.list_ads(query, identity=identity)
    return _meta_snapshots(identity, campaigns, adsets, ads), _identity_output(identity)


def _linkedin_inventory(
    client: httpx.Client,
    profile: Profile,
    token_getter: Any,
    maximum_records: int,
) -> tuple[list[dict[str, Any]], IdentityOutput]:
    adapter = LinkedInAdsClient(SafeHttpClient(client, {"api.linkedin.com"}), token_getter)
    identity = adapter.discover_identity(
        profile.account_ref, expected_account_client_ref=profile.account_ref
    )
    _verify_identity(profile, identity.timezone, identity.currency)
    query = LinkedInAdsInventoryQuery(
        account_ref=identity.account_ref, maximum_records=maximum_records
    )
    groups = adapter.list_campaign_groups(query, identity=identity)
    campaigns = adapter.list_campaigns(query, identity=identity)
    creatives = adapter.list_creatives(query, identity=identity, campaigns=campaigns)
    return _linkedin_snapshots(identity, groups, campaigns, creatives), _identity_output(identity)


def _tiktok_inventory(
    client: httpx.Client,
    profile: Profile,
    token_getter: Any,
    maximum_records: int,
) -> tuple[list[dict[str, Any]], IdentityOutput]:
    adapter = TikTokAdsClient(SafeHttpClient(client, {"business-api.tiktok.com"}), token_getter)
    identity = adapter.discover_identity(
        profile.account_ref, expected_account_client_ref=profile.account_ref
    )
    _verify_identity(profile, identity.timezone, identity.currency)
    query = TikTokAdsInventoryQuery(
        account_ref=identity.account_ref, maximum_records=maximum_records
    )
    campaigns = adapter.list_campaigns(query, identity=identity)
    adgroups = adapter.list_adgroups(query, identity=identity)
    ads = adapter.list_ads(query, identity=identity)
    return _tiktok_snapshots(identity, campaigns, adgroups, ads), _identity_output(identity)


def _reddit_inventory(
    client: httpx.Client,
    profile: Profile,
    token_getter: Any,
    maximum_records: int,
) -> tuple[list[dict[str, Any]], IdentityOutput]:
    adapter = RedditAdsClient(SafeHttpClient(client, {"ads-api.reddit.com"}), token_getter)
    identity = adapter.discover_identity(
        profile.account_ref, expected_account_client_ref=profile.account_ref
    )
    _verify_identity(profile, identity.timezone, identity.currency)
    query = RedditAdsInventoryQuery(
        account_ref=identity.account_ref, maximum_records=maximum_records
    )
    campaigns = adapter.list_campaigns(query, identity=identity)
    ad_groups = adapter.list_ad_groups(query, identity=identity)
    ads = adapter.list_ads(query, identity=identity)
    return _reddit_snapshots(identity, campaigns, ad_groups, ads), _identity_output(identity)


def _openai_inventory(
    client: httpx.Client,
    profile: OperationalInventoryProfile,
    token_getter: Any,
    maximum_records: int,
) -> tuple[list[dict[str, Any]], IdentityOutput]:
    adapter = OpenAIAdsClient(SafeHttpClient(client, {"api.ads.openai.com"}), token_getter)
    identity = adapter.discover_identity(expected_account_client_ref=profile.account_ref)
    _verify_identity(profile, identity.timezone, identity.currency)
    hierarchy = adapter.list_hierarchy(
        OpenAIAdsHierarchyInventoryQuery(
            account_ref=identity.account_ref, maximum_records=maximum_records
        ),
        identity=identity,
    )
    return _openai_snapshots(
        identity, list(hierarchy.campaigns), list(hierarchy.ad_groups), list(hierarchy.ads)
    ), _identity_output(identity)


def _verify_identity(
    profile: OperationalInventoryProfile, timezone: str | None, currency: str
) -> None:
    if (timezone is not None and timezone != profile.timezone) or (
        profile.currency is not None and currency != profile.currency
    ):
        raise AdhelmError(
            "IDENTITY_MISMATCH", "Provider account settings differ from the selected profile."
        )


def _identity_output(identity: Any) -> IdentityOutput:
    status = getattr(identity, "status", None)
    return {
        "timezone": cast(str | None, identity.timezone),
        "currency": cast(str, identity.currency),
        "status": status if isinstance(status, str) and status else "NOT_AVAILABLE_FROM_PROVIDER",
    }


def _snapshot(
    *,
    provider_api_version: str,
    resource_type: Literal["ACCOUNT", "CAMPAIGN_GROUP", "CAMPAIGN", "AD_GROUP", "AD"],
    resource_ref: str,
    parents: list[str],
    state: dict[str, Any],
    status: str,
    quality: list[str] | None = None,
) -> dict[str, Any]:
    normalized = {key: value for key, value in state.items() if value is not None}
    state_digest = digest(normalized)
    return {
        "snapshot_id": "snapshot:"
        + digest({"api": provider_api_version, "resource": resource_ref, "state": state_digest}),
        "provider_api_version": provider_api_version,
        "connector_version": "0.1.0.dev0",
        "resource_type": resource_type,
        "resource_ref": resource_ref,
        "parent_resource_refs": parents,
        "remote_fingerprint": state_digest,
        "normalized_state": normalized,
        "normalized_state_digest": state_digest,
        "effective_status": status,
        "source_updated_at": None,
        "data_quality_statuses": quality or [],
    }


def _account_snapshot(identity: Any) -> dict[str, Any]:
    source_status = getattr(identity, "status", None)
    status = (
        source_status
        if isinstance(source_status, str) and source_status
        else "NOT_AVAILABLE_FROM_PROVIDER"
    )
    unavailable = (
        [] if isinstance(source_status, str) and source_status else ["NOT_AVAILABLE_FROM_PROVIDER"]
    )
    return _snapshot(
        provider_api_version=identity.provider_api_version,
        resource_type="ACCOUNT",
        resource_ref=identity.account_ref,
        parents=[],
        state={
            "status": status,
            "currency": identity.currency,
            "timezone": identity.timezone,
        },
        status=status,
        quality=unavailable,
    )


def _google_account_quality(
    inventory: GoogleAdsInventory, identity: GoogleAdsIdentity
) -> list[str]:
    """Account-level read facts an audit needs and cannot otherwise recover.

    The Cloud project's Test/Explorer/Basic/Standard access level is never
    returned by a reviewed read, so its absence is recorded explicitly rather than left to
    look like a healthy default.  `customer.test_account` is a property of the
    customer, and an account that returned no flag is neither test nor proven
    production.
    """
    quality = [
        GOOGLE_ADS_PAGINATION_TOTAL_DECLARED
        if inventory.pagination_completeness == "PROVEN_COMPLETE"
        else GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED,
        GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE
        if identity.cloud_project_access_level == "NOT_OBSERVABLE"
        else f"GOOGLE_ADS_CLOUD_PROJECT_ACCESS_{identity.cloud_project_access_level}",
    ]
    if identity.test_account is None:
        quality.append(GOOGLE_ADS_TEST_ACCOUNT_FLAG_NOT_RETURNED)
    else:
        quality.append(
            GOOGLE_ADS_TEST_ACCOUNT_DECLARED
            if identity.test_account
            else GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED
        )
    return quality


def _google_campaign_quality(item: GoogleAdsCampaign) -> list[str]:
    """Presence and enum facts the closed snapshot state model has no field for.

    Each fact is recorded in both directions so that a retained snapshot can
    never be read as "not evaluated" when it was evaluated and found present.
    An enum value becomes one prefixed status, because the shared closed state
    model has no field for a network, geo-target, or budget-period setting.
    """
    quality = [
        GOOGLE_ADS_AD_SCHEDULE_PRESENT
        if item.ad_schedule_present
        else GOOGLE_ADS_AD_SCHEDULE_ABSENT,
        GOOGLE_ADS_BUDGET_RETURNED if item.budget is not None else GOOGLE_ADS_BUDGET_NOT_RETURNED,
        GOOGLE_ADS_BUDGET_TYPE_RETURNED
        if item.budget_period is not None
        else GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED,
        GOOGLE_ADS_NETWORK_SETTINGS_RETURNED
        if item.target_google_search is not None
        or item.target_search_network is not None
        or item.target_content_network is not None
        or item.target_partner_search_network is not None
        else GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED,
        GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED
        if item.positive_geo_target_type is not None or item.negative_geo_target_type is not None
        else GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED,
    ]
    if item.budget_period is not None:
        quality.append(GOOGLE_ADS_BUDGET_PERIOD_PREFIX + item.budget_period)
    if item.budget_explicitly_shared is None:
        quality.append(GOOGLE_ADS_BUDGET_SHARING_NOT_RETURNED)
    else:
        quality.append(
            GOOGLE_ADS_BUDGET_EXPLICITLY_SHARED
            if item.budget_explicitly_shared
            else GOOGLE_ADS_BUDGET_NOT_EXPLICITLY_SHARED
        )
    if item.budget_delivery_method is None:
        quality.append(GOOGLE_ADS_BUDGET_DELIVERY_METHOD_NOT_RETURNED)
    else:
        quality.append(GOOGLE_ADS_BUDGET_DELIVERY_METHOD_PREFIX + item.budget_delivery_method)
    if item.target_content_network:
        quality.append(GOOGLE_ADS_NETWORK_CONTENT_ENABLED)
    if item.target_partner_search_network:
        quality.append(GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED)
    if item.positive_geo_target_type is not None:
        quality.append(GOOGLE_ADS_POSITIVE_GEO_TARGET_TYPE_PREFIX + item.positive_geo_target_type)
    if item.negative_geo_target_type is not None:
        quality.append(GOOGLE_ADS_NEGATIVE_GEO_TARGET_TYPE_PREFIX + item.negative_geo_target_type)
    return quality


def _google_ad_quality(item: GoogleAdsAd) -> list[str]:
    """Creative and policy presence facts, recorded in both directions.

    `GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED` is unconditional: it states the
    read boundary itself, so a reader never has to infer from a presence flag
    that no headline or description text exists locally.  Component counts stay
    on the adapter model; what survives into history is presence and the
    documented-minimum bucket, so no retained record can be mistaken for
    creative capture.
    """
    quality = [GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED]
    if not item.responsive_search_ad_returned:
        quality.append(GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED)
    else:
        headlines = item.headline_count or 0
        descriptions = item.description_count or 0
        quality.append(
            GOOGLE_ADS_RSA_HEADLINES_PRESENT if headlines > 0 else GOOGLE_ADS_RSA_HEADLINES_ABSENT
        )
        quality.append(
            GOOGLE_ADS_RSA_HEADLINES_AT_DOCUMENTED_MINIMUM
            if headlines >= GOOGLE_ADS_RSA_HEADLINE_MINIMUM
            else GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM
        )
        quality.append(
            GOOGLE_ADS_RSA_DESCRIPTIONS_PRESENT
            if descriptions > 0
            else GOOGLE_ADS_RSA_DESCRIPTIONS_ABSENT
        )
        quality.append(
            GOOGLE_ADS_RSA_DESCRIPTIONS_AT_DOCUMENTED_MINIMUM
            if descriptions >= GOOGLE_ADS_RSA_DESCRIPTION_MINIMUM
            else GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM
        )
    quality.append(
        GOOGLE_ADS_FINAL_URL_PRESENT if item.final_url_count > 0 else GOOGLE_ADS_FINAL_URL_ABSENT
    )
    if item.policy_approval_status is None:
        quality.append(GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED)
    else:
        quality.append(GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED)
        quality.append(GOOGLE_ADS_POLICY_APPROVAL_PREFIX + item.policy_approval_status)
    if item.policy_review_status is None:
        quality.append(GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED)
    else:
        quality.append(GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED)
        quality.append(GOOGLE_ADS_POLICY_REVIEW_PREFIX + item.policy_review_status)
    return quality


def _google_snapshots(inventory: GoogleAdsInventory, identity: Any) -> list[dict[str, Any]]:
    account = _account_snapshot(identity)
    # The account snapshot is the one place a read-boundary fact can live for
    # the whole read, so it carries them beside its own unavailability statuses.
    account = _snapshot(
        provider_api_version=identity.provider_api_version,
        resource_type="ACCOUNT",
        resource_ref=identity.account_ref,
        parents=[],
        state=dict(account["normalized_state"]),
        status=account["effective_status"],
        quality=[
            *account["data_quality_statuses"],
            GOOGLE_ADS_STATE_SHAPE_STATUS,
            *_google_account_quality(inventory, identity),
        ],
    )
    result = [account]
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="CAMPAIGN",
            resource_ref=item.campaign_ref,
            # The account snapshot in this same completed inventory read is the
            # known parent.  Do not infer any older hierarchy.
            parents=[identity.account_ref],
            state={
                "status": item.status,
                "objective": item.objective,
                "currency": identity.currency,
                "budget_minor": item.budget.minor_units if item.budget else None,
                # The shared closed state model documents only `DAILY` and
                # `LIFETIME`.  Google's `CUSTOM_PERIOD` is neither, so it is
                # not coerced into one: the exact period is retained as a
                # prefixed data-quality status instead.
                "budget_type": "DAILY" if item.budget_period == "DAILY" else None,
                "bid_strategy": item.bid_strategy,
                "schedule_start": item.start_date.isoformat() if item.start_date else None,
                "schedule_end": item.end_date.isoformat() if item.end_date else None,
            },
            status=item.status,
            quality=[GOOGLE_ADS_STATE_SHAPE_STATUS, *_google_campaign_quality(item)],
        )
        for item in inventory.campaigns
    )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD_GROUP",
            resource_ref=item.ad_group_ref,
            parents=[item.campaign_ref],
            state={
                "status": item.status,
                "objective": item.ad_group_type,
                "currency": identity.currency,
                "bid_amount_minor": item.bid_amount.minor_units if item.bid_amount else None,
            },
            status=item.status,
            quality=[GOOGLE_ADS_STATE_SHAPE_STATUS],
        )
        for item in inventory.ad_groups
    )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD",
            resource_ref=item.ad_ref,
            parents=[item.campaign_ref, item.ad_group_ref],
            state={"status": item.status, "objective": item.ad_type},
            status=item.status,
            quality=[
                item.creative_capture_status,
                GOOGLE_ADS_STATE_SHAPE_STATUS,
                *_google_ad_quality(item),
            ],
        )
        for item in inventory.ads
    )
    return result


def _meta_snapshots(
    identity: MetaAdsIdentity,
    campaigns: list[MetaAdsCampaign],
    adsets: list[MetaAdsAdSet],
    ads: list[MetaAdsAd],
) -> list[dict[str, Any]]:
    # Every Meta snapshot records the connector state-shape version beside its
    # state, never inside it, so a later reader can tell a change to what this
    # product retains from a change at Meta.  See
    # `docs/research/meta-ads-read-contract.md`.
    account = _account_snapshot(identity)
    account = _snapshot(
        provider_api_version=identity.provider_api_version,
        resource_type="ACCOUNT",
        resource_ref=account["resource_ref"],
        parents=[],
        state=dict(account["normalized_state"]),
        status=account["effective_status"],
        quality=[*account["data_quality_statuses"], META_ADS_STATE_SHAPE_STATUS],
    )
    result = [account]
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="CAMPAIGN",
            resource_ref=item.campaign_ref,
            parents=[identity.account_ref],
            state={
                "status": item.status,
                "objective": item.objective,
                "currency": identity.currency,
                "bid_strategy": item.bid_strategy,
                **_meta_budget_state(item.daily_budget, item.lifetime_budget),
                "spend_cap_minor": item.spend_cap.minor_units if item.spend_cap else None,
                "schedule_start": item.start_at.isoformat() if item.start_at else None,
                "schedule_end": item.end_at.isoformat() if item.end_at else None,
            },
            status=item.status,
            # The campaign buying type has no field in the shared closed state
            # model, so it is retained as an explicit enum fact instead of an
            # invented state key.  Every fact is stated in both directions.
            quality=[META_ADS_STATE_SHAPE_STATUS, *meta_ads_campaign_quality_statuses(item)],
        )
        for item in campaigns
    )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD_GROUP",
            resource_ref=item.adset_ref,
            parents=[item.campaign_ref],
            state={
                "status": item.status,
                "currency": identity.currency,
                **_meta_budget_state(item.daily_budget, item.lifetime_budget),
                "bid_amount_minor": item.bid_amount.minor_units if item.bid_amount else None,
                "bid_strategy": item.bid_strategy,
                "schedule_start": item.start_at.isoformat() if item.start_at else None,
                "schedule_end": item.end_at.isoformat() if item.end_at else None,
            },
            status=item.status,
            # Targeting is retained as presence only: whether the ad set
            # declared a targeting object at all, and whether it returned a
            # geography, an age bound, or an interest/flexible spec.  No
            # targeting value ever enters local history.
            quality=[META_ADS_STATE_SHAPE_STATUS, *meta_ads_ad_set_quality_statuses(item)],
        )
        for item in adsets
    )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD",
            resource_ref=item.ad_ref,
            parents=[item.campaign_ref, item.adset_ref],
            state={"status": item.status},
            status=item.status,
            # Creative association plus image-hash and link presence only; no
            # image hash, destination URL, headline, or body copy is retained.
            quality=[META_ADS_STATE_SHAPE_STATUS, *meta_ads_ad_quality_statuses(item)],
        )
        for item in ads
    )
    return result


def _meta_budget_state(daily_budget: Any, lifetime_budget: Any) -> dict[str, Any]:
    """Retain declared budgets without guessing how simultaneous values behave."""
    if daily_budget is not None and lifetime_budget is not None:
        return {
            "daily_budget_minor": daily_budget.minor_units,
            "lifetime_budget_minor": lifetime_budget.minor_units,
        }
    if daily_budget is not None:
        return {"budget_minor": daily_budget.minor_units, "budget_type": "DAILY"}
    if lifetime_budget is not None:
        return {"budget_minor": lifetime_budget.minor_units, "budget_type": "LIFETIME"}
    return {"budget_minor": None, "budget_type": None}


def _linkedin_snapshots(
    identity: LinkedInAdsIdentity,
    groups: list[LinkedInAdsCampaignGroup],
    campaigns: list[LinkedInAdsCampaign],
    creatives: list[LinkedInAdsCreative],
) -> list[dict[str, Any]]:
    result = [_account_snapshot(identity)]
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="CAMPAIGN_GROUP",
            resource_ref=item.campaign_group_ref,
            parents=[identity.account_ref],
            state={"status": item.status, "currency": identity.currency},
            status=item.status,
        )
        for item in groups
    )
    for campaign in campaigns:
        if campaign.campaign_group_ref is None:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "LinkedIn Ads campaign lacks its required campaign-group parent.",
            )
        result.append(
            _snapshot(
                provider_api_version=identity.provider_api_version,
                resource_type="CAMPAIGN",
                resource_ref=campaign.campaign_ref,
                parents=[campaign.campaign_group_ref],
                state={
                    "status": campaign.status,
                    "objective": campaign.objective,
                    "currency": identity.currency,
                },
                status=campaign.status,
            )
        )
    for creative in creatives:
        if creative.campaign_ref is None:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "LinkedIn Ads creative lacks its required campaign parent.",
            )
        result.append(
            _snapshot(
                provider_api_version=identity.provider_api_version,
                resource_type="AD",
                resource_ref=creative.creative_ref,
                parents=[creative.campaign_ref],
                state={"status": creative.status, "currency": identity.currency},
                status=creative.status or "NOT_AVAILABLE_FROM_PROVIDER",
                quality=["CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER"],
            )
        )
    return result


def _tiktok_budget_state(budget_type: str | None, budget: Any) -> tuple[dict[str, Any], list[str]]:
    if budget_type is None:
        return {}, ["BUDGET_MODE_NOT_AVAILABLE_FROM_PROVIDER"]
    if budget_type == "BUDGET_MODE_DAY":
        return (
            {
                "tiktok_ads_budget_mode": budget_type,
                "budget_minor": budget.minor_units if budget is not None else None,
                "budget_type": "DAILY",
            },
            [] if budget is not None else ["BUDGET_NOT_AVAILABLE_FROM_PROVIDER"],
        )
    if budget_type == "BUDGET_MODE_TOTAL":
        return (
            {
                "tiktok_ads_budget_mode": budget_type,
                "budget_minor": budget.minor_units if budget is not None else None,
                "budget_type": "LIFETIME",
            },
            [] if budget is not None else ["BUDGET_NOT_AVAILABLE_FROM_PROVIDER"],
        )
    if budget_type == "BUDGET_MODE_INFINITE":
        if budget is not None:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "TikTok Ads infinite budget mode included an unrepresentable monetary cap.",
            )
        return {"tiktok_ads_budget_mode": budget_type}, ["BUDGET_MODE_INFINITE"]
    raise AdhelmError("UNSUPPORTED_BUDGET_MODE", "TikTok Ads returned an unreviewed budget mode.")


def _tiktok_snapshots(
    identity: TikTokAdsIdentity,
    campaigns: list[TikTokAdsCampaign],
    adgroups: list[TikTokAdsAdGroup],
    ads: list[TikTokAdsAd],
) -> list[dict[str, Any]]:
    # Every TikTok snapshot records the connector state-shape version beside its
    # state, never inside it, so a later reader can tell a change to what this
    # product retains from a change at TikTok.  The account snapshot additionally
    # records that the bounded read proved its own declared total: the numbered
    # page walk validates `page`, `page_size` and `total_number` on every page
    # and refuses a set that does not add up, so a read that returned at all
    # returned a whole result.  See `docs/research/tiktok-ads-read-contract.md`.
    account = _account_snapshot(identity)
    account = _snapshot(
        provider_api_version=identity.provider_api_version,
        resource_type="ACCOUNT",
        resource_ref=account["resource_ref"],
        parents=[],
        state=dict(account["normalized_state"]),
        status=account["effective_status"],
        quality=[
            *account["data_quality_statuses"],
            TIKTOK_ADS_STATE_SHAPE_STATUS,
            TIKTOK_ADS_PAGINATION_TOTAL_DECLARED,
        ],
    )
    result = [account]
    for campaign in campaigns:
        state, quality = _tiktok_budget_state(campaign.budget_type, campaign.budget)
        result.append(
            _snapshot(
                provider_api_version=identity.provider_api_version,
                resource_type="CAMPAIGN",
                resource_ref=campaign.campaign_ref,
                parents=[identity.account_ref],
                state={
                    "status": campaign.status,
                    "objective": campaign.objective,
                    "currency": identity.currency,
                    **state,
                },
                status=campaign.status,
                # The campaign objective has no reviewed enumeration this build
                # could check, so it is retained as a presence fact only; the
                # budget mode and the operation status are stated in both
                # directions beside it.
                quality=[
                    *quality,
                    TIKTOK_ADS_STATE_SHAPE_STATUS,
                    *tiktok_ads_campaign_quality_statuses(campaign),
                ],
            )
        )
    for adgroup in adgroups:
        state, quality = _tiktok_budget_state(adgroup.budget_type, adgroup.budget)
        result.append(
            _snapshot(
                provider_api_version=identity.provider_api_version,
                resource_type="AD_GROUP",
                resource_ref=adgroup.adgroup_ref,
                parents=[adgroup.campaign_ref],
                state={
                    "status": adgroup.status,
                    "currency": identity.currency,
                    "bid_strategy": adgroup.bid_type,
                    "bid_amount_minor": (
                        adgroup.bid_amount.minor_units if adgroup.bid_amount is not None else None
                    ),
                    **state,
                },
                status=adgroup.status,
                quality=[
                    *quality,
                    TIKTOK_ADS_STATE_SHAPE_STATUS,
                    *tiktok_ads_ad_group_quality_statuses(adgroup),
                ],
            )
        )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD",
            resource_ref=item.ad_ref,
            parents=[item.campaign_ref, item.adgroup_ref],
            state={"status": item.status, "currency": identity.currency},
            status=item.status,
            # The reviewed `/ad/get/` projection returns identity, parents and
            # the operation status only.  Creative association, video presence,
            # ad copy and destination are not requested at all, so they stay
            # unread surfaces rather than being recorded as absent facts.
            quality=[
                "CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER",
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                *tiktok_ads_ad_quality_statuses(item),
            ],
        )
        for item in ads
    )
    return result


def _reddit_snapshots(
    identity: RedditAdsIdentity,
    campaigns: list[RedditAdsCampaign],
    ad_groups: list[RedditAdsAdGroup],
    ads: list[RedditAdsAd],
) -> list[dict[str, Any]]:
    result = [_account_snapshot(identity)]
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="CAMPAIGN",
            resource_ref=item.campaign_ref,
            parents=[identity.account_ref],
            state={
                "status": item.status,
                "objective": item.objective,
                "currency": identity.currency,
            },
            status=item.status or "NOT_AVAILABLE_FROM_PROVIDER",
        )
        for item in campaigns
    )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD_GROUP",
            resource_ref=item.ad_group_ref,
            parents=[item.campaign_ref],
            state={"status": item.status, "currency": identity.currency},
            status=item.status or "NOT_AVAILABLE_FROM_PROVIDER",
        )
        for item in ad_groups
    )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD",
            resource_ref=item.ad_ref,
            parents=[item.campaign_ref, item.ad_group_ref],
            state={"status": item.status, "currency": identity.currency},
            status=item.status or "NOT_AVAILABLE_FROM_PROVIDER",
            quality=["CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER"],
        )
        for item in ads
    )
    return result


def _openai_minor(micros: str | None, currency: str) -> int | None:
    if micros is not None and currency == "USD" and int(micros) % 10_000 == 0:
        return int(micros) // 10_000
    return None


def _openai_money_quality(micros: str | None, currency: str) -> list[str]:
    if micros is None:
        return ["AMOUNT_UNAVAILABLE_IN_OBSERVATION"]
    if currency != "USD":
        return ["CURRENCY_EXPONENT_UNVERIFIED_NATIVE_MICROS_RETAINED"]
    if int(micros) % 10_000:
        return ["SUBMINOR_AMOUNT_RETAINED_AS_NATIVE_MICROS"]
    return []


def _openai_snapshots(
    identity: OpenAIAdsIdentity,
    campaigns: list[OpenAIAdsCampaign],
    ad_groups: list[OpenAIAdsAdGroup],
    ads: list[OpenAIAdsAd],
) -> list[dict[str, Any]]:
    result = [_account_snapshot(identity)]
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="CAMPAIGN",
            resource_ref=item.campaign_ref,
            parents=[identity.account_ref],
            state={
                "status": item.status,
                "currency": identity.currency,
                "bid_strategy": item.bidding_type,
                "openai_ads_lifetime_spend_limit_micros": item.lifetime_spend_limit_micros,
                "budget_type": "LIFETIME" if item.lifetime_spend_limit_micros else None,
                "budget_minor": _openai_minor(item.lifetime_spend_limit_micros, identity.currency),
                "lifetime_budget_minor": _openai_minor(
                    item.lifetime_spend_limit_micros, identity.currency
                ),
                "schedule_start": item.start_at.isoformat() if item.start_at else None,
                "schedule_end": item.end_at.isoformat() if item.end_at else None,
            },
            status=item.status,
            quality=[
                *_openai_money_quality(item.lifetime_spend_limit_micros, identity.currency),
                *openai_ads_campaign_quality_statuses(item),
            ],
        )
        for item in campaigns
    )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD_GROUP",
            resource_ref=item.ad_group_ref,
            parents=[item.campaign_ref],
            state={
                "status": item.status,
                "currency": identity.currency,
                "openai_ads_max_bid_micros": item.max_bid_micros,
                "bid_amount_minor": _openai_minor(item.max_bid_micros, identity.currency),
            },
            status=item.status,
            quality=[
                *_openai_money_quality(item.max_bid_micros, identity.currency),
                *openai_ads_ad_group_quality_statuses(item),
            ],
        )
        for item in ad_groups
    )
    result.extend(
        _snapshot(
            provider_api_version=identity.provider_api_version,
            resource_type="AD",
            resource_ref=item.ad_ref,
            parents=[item.campaign_ref, item.ad_group_ref],
            state={"status": item.status, "currency": identity.currency},
            status=item.status,
            # The shared closed snapshot state model has no field for a
            # creative association or a copy presence flag, so these audit
            # facts are recorded as explicit normalized data-quality statuses.
            quality=["CREATIVE_BINDING_UNVERIFIED", *openai_ads_ad_quality_statuses(item)],
        )
        for item in ads
    )
    return result


def _validate_snapshot_hierarchy(
    snapshots: list[dict[str, Any]], provider: InventoryProvider
) -> None:
    """Require every declared parent to be present with its expected type.

    The inventory collections are sequential remote reads, so a parent can
    disappear or a child can move between them during the collection.  Such a
    mixed response is not a complete hierarchy and must not become history.
    """
    by_ref: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        resource_ref = snapshot["resource_ref"]
        if resource_ref in by_ref:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "Provider inventory contains duplicate resource references.",
            )
        by_ref[resource_ref] = snapshot

    accounts = [snapshot for snapshot in snapshots if snapshot["resource_type"] == "ACCOUNT"]
    if len(accounts) != 1:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            "Provider inventory must contain exactly one account resource.",
        )
    account_ref = cast(str, accounts[0]["resource_ref"])
    expected_by_provider: dict[str, dict[str, tuple[str, ...]]] = {
        "linkedin_ads": {
            "ACCOUNT": (),
            "CAMPAIGN_GROUP": ("ACCOUNT",),
            "CAMPAIGN": ("CAMPAIGN_GROUP",),
            "AD": ("CAMPAIGN",),
        },
        "openai_ads": {
            "ACCOUNT": (),
            "CAMPAIGN": ("ACCOUNT",),
            "AD_GROUP": ("CAMPAIGN",),
            "AD": ("CAMPAIGN", "AD_GROUP"),
        },
        "google_ads": {
            "ACCOUNT": (),
            "CAMPAIGN": ("ACCOUNT",),
            "AD_GROUP": ("CAMPAIGN",),
            "AD": ("CAMPAIGN", "AD_GROUP"),
        },
        "meta_ads": {
            "ACCOUNT": (),
            "CAMPAIGN": ("ACCOUNT",),
            "AD_GROUP": ("CAMPAIGN",),
            "AD": ("CAMPAIGN", "AD_GROUP"),
        },
        "tiktok_ads": {
            "ACCOUNT": (),
            "CAMPAIGN": ("ACCOUNT",),
            "AD_GROUP": ("CAMPAIGN",),
            "AD": ("CAMPAIGN", "AD_GROUP"),
        },
        "reddit_ads": {
            "ACCOUNT": (),
            "CAMPAIGN": ("ACCOUNT",),
            "AD_GROUP": ("CAMPAIGN",),
            "AD": ("CAMPAIGN", "AD_GROUP"),
        },
    }
    expected_parent_types = expected_by_provider[provider]
    for snapshot in snapshots:
        resource_type = cast(str, snapshot["resource_type"])
        parents = cast(list[str], snapshot["parent_resource_refs"])
        expected = expected_parent_types.get(resource_type)
        if expected is None:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "Provider inventory contains an unsupported resource type.",
            )
        if len(parents) != len(expected):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "Provider inventory has an incomplete resource hierarchy.",
            )
        if expected == ("ACCOUNT",) and parents != [account_ref]:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "Provider inventory campaign does not belong to the observed account.",
            )
        for parent_ref, parent_type in zip(parents, expected, strict=True):
            parent = by_ref.get(parent_ref)
            if parent is None or parent["resource_type"] != parent_type:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "Provider inventory has an incomplete resource hierarchy.",
                )
        if provider == "openai_ads" and resource_type == "AD":
            campaign_ref, ad_group_ref = parents
            ad_group = by_ref[ad_group_ref]
            if ad_group["parent_resource_refs"] != [campaign_ref]:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads ad campaign and ad-group parents disagree.",
                )


def _change_for_snapshot(
    ledger: Ledger, snapshot: dict[str, Any], provider: str, account_ref: str
) -> dict[str, Any] | None:
    baseline = ledger.snapshot_diff(snapshot, provider=provider, account_ref=account_ref)
    prior = _previous_snapshot(ledger, snapshot, provider, account_ref)
    if prior is None:
        return None
    before = prior["payload"]
    deltas = _snapshot_deltas(before, snapshot)
    if not baseline["changed"] and not deltas:
        return None
    if baseline["changed"]:
        event = dict(cast(dict[str, Any], baseline["change_payload"]))
    else:
        event = {
            "event_type": "REMOTE_CHANGE_OBSERVED",
            "operation": "OBSERVE",
            "origin": "UNKNOWN",
            "resource_type": snapshot["resource_type"],
            "resource_ref": snapshot["resource_ref"],
            "effect_status": "EXTERNAL_CHANGE_DETECTED",
            "before_snapshot_ref": prior["record_id"],
            "before_digest": before["normalized_state_digest"],
            "after_digest": snapshot["normalized_state_digest"],
            "observation_interval_start": prior["observed_at"],
        }
    event["field_deltas"] = deltas
    return event


def _previous_snapshot(
    ledger: Ledger, snapshot: dict[str, Any], provider: str, account_ref: str
) -> dict[str, Any] | None:
    for record in reversed(ledger.records("changes")):
        if (
            record["record_kind"] == "RESOURCE_SNAPSHOT"
            and record["provider"] == provider
            and record["account_ref"] == account_ref
            and record["payload"]["provider_api_version"] == snapshot["provider_api_version"]
            and record["payload"]["resource_ref"] == snapshot["resource_ref"]
        ):
            return record
    return None


def _snapshot_deltas(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    before_state = cast(dict[str, Any], before["normalized_state"])
    after_state = cast(dict[str, Any], after["normalized_state"])
    result: list[dict[str, Any]] = []
    for field in sorted(set(before_state) | set(after_state)):
        old = before_state.get(field)
        new = after_state.get(field)
        if old != new:
            result.append(
                {
                    "path": f"normalized_state.{field}",
                    "before_status": "PRESENT" if field in before_state else "ABSENT",
                    "before_value": old,
                    "after_status": "PRESENT" if field in after_state else "ABSENT",
                    "after_value": new,
                    "value_classification": "SCALAR",
                }
            )
    old_parents = before["parent_resource_refs"]
    new_parents = after["parent_resource_refs"]
    if old_parents != new_parents:
        result.append(
            {
                "path": "parent_resource_refs",
                "before_status": "PRESENT",
                "before_value": old_parents,
                "after_status": "PRESENT",
                "after_value": new_parents,
                "value_classification": "RESOURCE_REF",
            }
        )
    return result


def _counts(snapshots: list[dict[str, Any]]) -> dict[str, int]:
    result = {
        resource_type: sum(
            1 for snapshot in snapshots if snapshot["resource_type"] == resource_type
        )
        for resource_type in ("ACCOUNT", "CAMPAIGN", "AD_GROUP", "AD")
    }
    if any(snapshot["resource_type"] == "CAMPAIGN_GROUP" for snapshot in snapshots):
        result["CAMPAIGN_GROUP"] = sum(
            1 for snapshot in snapshots if snapshot["resource_type"] == "CAMPAIGN_GROUP"
        )
    return result


def _diagnostics(provider: InventoryProvider) -> list[str]:
    if provider == "openai_ads":
        return [
            "MINOR_UNIT_PROJECTION_REQUIRES_EXACT_USD_AMOUNT",
            "CREATIVE_BINDING_UNVERIFIED",
        ]
    if provider == "linkedin_ads":
        return [
            "ACCOUNT_TIMEZONE_NOT_AVAILABLE_FROM_PROVIDER",
            "CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER",
        ]
    return ["CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER"]
