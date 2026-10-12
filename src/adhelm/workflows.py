"""Typed workflows joining explicit profiles, adapters, observations and sharing."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from pydantic import ValidationError

from adhelm.artifact_workflows import (
    ReportArtifactPreflight,
    finalize_report_artifacts,
    preflight_report_artifacts,
)
from adhelm.canonical import digest, parse_json
from adhelm.config import load_configuration
from adhelm.credentials import credential, export_key
from adhelm.errors import AdhelmError
from adhelm.input_files import read_json_input_file
from adhelm.ledger import Ledger
from adhelm.meta_ads_token_read_session import configured_meta_ads_token_read_session
from adhelm.models import (
    Configuration,
    ConfiguredProfile,
    MetaAccountTokenProfile,
    Profile,
    validate_configuration,
)
from adhelm.projections import _redact_payload
from adhelm.providers.ga4 import Ga4AggregateQuery, Ga4DataClient, Ga4ReportQuery
from adhelm.providers.google_ads import GoogleAdsClient, GoogleAdsReadConfig, GoogleAdsReportQuery
from adhelm.providers.linkedin_ads import LinkedInAdsClient, LinkedInAdsReportQuery
from adhelm.providers.meta_ads import MetaAdsClient, MetaAdsReportQuery
from adhelm.providers.openai_ads import OpenAIAdsClient, OpenAIAdsReportQuery
from adhelm.providers.reddit_ads import RedditAdsClient, RedditAdsReportQuery
from adhelm.providers.tiktok_ads import TikTokAdsClient, TikTokAdsReportQuery
from adhelm.reporting import Ga4AggregateReport, LedgerCheckpoint, NormalizedReport
from adhelm.strict_json import validate_json_input
from adhelm.transport import SafeHttpClient

ReportProvider = Literal[
    "ga4", "openai_ads", "google_ads", "meta_ads", "linkedin_ads", "tiktok_ads", "reddit_ads"
]
ReportQuery = (
    Ga4ReportQuery
    | Ga4AggregateQuery
    | OpenAIAdsReportQuery
    | GoogleAdsReportQuery
    | MetaAdsReportQuery
    | LinkedInAdsReportQuery
    | TikTokAdsReportQuery
    | RedditAdsReportQuery
)


_REPORT_QUERY_MODELS: dict[ReportProvider, type[ReportQuery]] = {
    "ga4": Ga4ReportQuery,
    "openai_ads": OpenAIAdsReportQuery,
    "google_ads": GoogleAdsReportQuery,
    "meta_ads": MetaAdsReportQuery,
    "linkedin_ads": LinkedInAdsReportQuery,
    "tiktok_ads": TikTokAdsReportQuery,
    "reddit_ads": RedditAdsReportQuery,
}


def parse_report_query(provider: ReportProvider, raw: bytes) -> ReportQuery:
    """Validate a report input against the provider's closed query model.

    GA4's fixed aggregate contract is selected by its own required discriminator.
    Every input that validated as a `Ga4ReportQuery` before still does:
    `aggregate_shape` is not a field of that model, so a request carrying it was
    already rejected.
    """
    query_model = _REPORT_QUERY_MODELS[provider]
    if provider == "ga4":
        probe = parse_json(raw)
        if isinstance(probe, dict) and "aggregate_shape" in probe:
            query_model = Ga4AggregateQuery
    return validate_json_input(query_model, raw)


@dataclass(frozen=True)
class ReportPreconditions:
    """What `report <provider>` derives from the profile, input and options before egress."""

    query: ReportQuery
    google_config: GoogleAdsReadConfig | None


def require_report_preconditions(
    profile: ConfiguredProfile,
    provider: ReportProvider,
    raw_input: bytes,
    *,
    stream_id: str | None,
    ci_enabled: bool,
) -> ReportPreconditions:
    """Run every report check that needs only the profile, the input and the options.

    This is the single home of the checks `report <provider>` makes before it reads a
    credential, the export key, the ledger or a provider, and `pilot run` calls it
    before its first step so the two cannot drift. It reads and writes nothing:

    * the input parses against the provider's closed query model;
    * the query's account is the selected profile's account;
    * LinkedIn fixed reporting runs on an explicit UTC profile;
    * GA4 has the expected web-stream identity and its property is the profile's;
    * Google Ads has a scoped profile;
    * a Meta token profile is not used under ``--ci``, and the query's source time
      zone and currency are the account declaration's.
    """
    query = parse_report_query(provider, raw_input)
    if query.account_ref != profile.account_ref:
        raise AdhelmError(
            "IDENTITY_MISMATCH", "Query account differs from the selected read profile."
        )
    if provider == "linkedin_ads" and profile.timezone != "UTC":
        raise AdhelmError(
            "TIMEZONE_MISMATCH", "LinkedIn fixed reporting requires an explicit UTC profile."
        )
    if provider == "ga4":
        if not stream_id:
            raise AdhelmError(
                "EXPECTED_STREAM_REQUIRED", "GA4 needs the expected web-stream identity."
            )
        if (
            not isinstance(query, Ga4ReportQuery | Ga4AggregateQuery)
            or query.property_id != profile.account_ref
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "GA4 property differs from the selected profile."
            )
    google_config: GoogleAdsReadConfig | None = None
    if provider == "google_ads":
        if not isinstance(profile, Profile):
            raise AdhelmError(
                "SCOPED_PROFILE_REQUIRED", "Google Ads requires a scoped OAuth profile."
            )
        google_config = GoogleAdsReadConfig(
            account_ref=profile.account_ref,
            customer_client_ref=profile.account_ref,
            login_customer_client_ref=profile.login_customer_ref,
        )
    if isinstance(profile, MetaAccountTokenProfile):
        if ci_enabled:
            raise AdhelmError(
                "META_TOKEN_INTERACTIVE_STORE_REQUIRED",
                "Meta token profiles require the configured operating-system credential store.",
                exit_code=4,
            )
        if not isinstance(query, MetaAdsReportQuery) or (
            query.source_timezone != profile.timezone
            or (query.currency is not None and query.currency != profile.currency)
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Meta Ads report settings differ from the configured account declaration.",
                exit_code=2,
            )
    return ReportPreconditions(query, google_config)


def _configuration(root: Path) -> Configuration:
    """Detach configuration before any artifact, key, credential, or HTTP path."""
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


def run_report(
    root: Path,
    provider: ReportProvider,
    profile_id: str,
    input_file: Path,
    *,
    stream_id: str | None = None,
    ephemeral: bool = False,
    ci_enabled: bool = False,
) -> dict[str, Any]:
    configuration = _configuration(root)
    artifact_preflight = preflight_report_artifacts(
        configuration.output,
        provider=provider,
        profile_id=profile_id,
        ephemeral=ephemeral,
    )
    profiles = [p for p in configuration.profiles if p.profile_id == profile_id]
    if len(profiles) != 1 or profiles[0].provider != provider or profiles[0].purpose != "READ":
        raise AdhelmError(
            "READ_PROFILE_REQUIRED",
            "An explicit matching read profile is required.",
            next_action="Configure a provider read profile using the installed schema.",
        )
    profile = profiles[0]
    preconditions = require_report_preconditions(
        profile,
        provider,
        read_json_input_file(input_file),
        stream_id=stream_id,
        ci_enabled=ci_enabled,
    )
    query = preconditions.query
    google_config = preconditions.google_config
    ledger = Ledger(root)
    checkpoint = ledger.checkpoint()
    # Acquire sharing prerequisites before any provider query or local report append.
    key = export_key()
    if isinstance(profile, MetaAccountTokenProfile):
        assert isinstance(query, MetaAdsReportQuery)
        with configured_meta_ads_token_read_session(
            root, profile.profile_id, expected_configuration_digest=digest(configuration)
        ) as session:
            report = session.run_report(query)
            canonical_query = query.model_copy(
                update={"account_ref": session.identity.account_ref, "currency": profile.currency}
            )
            report = _report_provenance(report, canonical_query, checkpoint)
            with session.retention_recheck() as recheck:
                recheck()
                stored = (
                    persist_report(ledger, report, _before_durable_append=recheck)
                    if not ephemeral
                    else None
                )
        # Artifact projection reads history after all configuration and
        # credential locks are released; it never obtains provider credentials.
        return _share_report(root, ledger, report, stored, key, artifact_preflight)
    # Google resource reads may need a protected OAuth refresh.  Resolve each
    # primary credential once before identity discovery so every request in one
    # sequential observation sees the same bearer token.
    google_primary_token: str | None = None
    if provider in {"google_ads", "ga4"}:
        google_primary_token = credential(profile, ci_enabled=ci_enabled)

    def getter() -> str:
        if google_primary_token is not None:
            return google_primary_token
        return credential(profile, ci_enabled=ci_enabled)

    ga4_aggregate: Ga4AggregateReport | None = None
    with httpx.Client(trust_env=False) as client:
        if isinstance(query, Ga4ReportQuery | Ga4AggregateQuery):
            adapter = Ga4DataClient(
                SafeHttpClient(
                    client, {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"}
                ),
                getter,
            )
            identity = adapter.discover_identity(
                query.property_id, expected_web_stream_id=stream_id
            )
            if identity.timezone != profile.timezone or (
                profile.currency is not None and identity.currency != profile.currency
            ):
                raise AdhelmError(
                    "IDENTITY_MISMATCH", "GA4 account settings differ from the profile."
                )
            # The user input is checked against the configured raw property ID
            # above.  Bind downstream provenance to the discovered canonical
            # property reference before the adapter can issue its report read.
            if isinstance(query, Ga4AggregateQuery):
                query = Ga4AggregateQuery.model_validate(
                    {**query.model_dump(), "account_ref": identity.property_ref}
                )
                ga4_aggregate = adapter.run_aggregate_report(query, identity=identity)
                report = ga4_aggregate.to_normalized_report()
            else:
                query = Ga4ReportQuery.model_validate(
                    {**query.model_dump(), "account_ref": identity.property_ref}
                )
                report = adapter.run_report(query, identity=identity)
        elif isinstance(query, OpenAIAdsReportQuery):
            ads = OpenAIAdsClient(SafeHttpClient(client, {"api.ads.openai.com"}), getter)
            ads_identity = ads.discover_identity(expected_account_client_ref=profile.account_ref)
            if ads_identity.timezone != profile.timezone or (
                profile.currency is not None and ads_identity.currency != profile.currency
            ):
                raise AdhelmError(
                    "IDENTITY_MISMATCH", "OpenAI account settings differ from the profile."
                )
            query = OpenAIAdsReportQuery.model_validate(
                {**query.model_dump(), "account_ref": ads_identity.account_ref}
            )
            report = (
                ads.run_campaign_report(query, identity=ads_identity)
                if query.level == "CAMPAIGN"
                else ads.run_ad_report(query, identity=ads_identity)
            )
        elif isinstance(query, GoogleAdsReportQuery):
            assert google_config is not None
            assert google_primary_token is not None

            google = GoogleAdsClient(SafeHttpClient(client, {"googleads.googleapis.com"}), getter)
            google_identity = google.discover_identity(google_config)
            if google_identity.timezone != profile.timezone or (
                profile.currency is not None and google_identity.currency != profile.currency
            ):
                raise AdhelmError(
                    "IDENTITY_MISMATCH", "Google Ads settings differ from the profile."
                )
            report = google.run_daily_report(google_config, query, identity=google_identity)
        elif isinstance(query, MetaAdsReportQuery):
            meta = MetaAdsClient(SafeHttpClient(client, {"graph.facebook.com"}), getter)
            meta_identity = meta.discover_identity(
                profile.account_ref, expected_account_client_ref=profile.account_ref
            )
            if meta_identity.timezone != profile.timezone or (
                profile.currency is not None and meta_identity.currency != profile.currency
            ):
                raise AdhelmError("IDENTITY_MISMATCH", "Meta Ads settings differ from the profile.")
            query = MetaAdsReportQuery.model_validate(
                {**query.model_dump(), "account_ref": meta_identity.account_ref}
            )
            report = (
                meta.run_campaign_report(query, identity=meta_identity)
                if query.level == "CAMPAIGN"
                else meta.run_ad_report(query, identity=meta_identity)
            )
        elif isinstance(query, TikTokAdsReportQuery):
            tiktok = TikTokAdsClient(SafeHttpClient(client, {"business-api.tiktok.com"}), getter)
            tiktok_identity = tiktok.discover_identity(
                profile.account_ref, expected_account_client_ref=profile.account_ref
            )
            if tiktok_identity.timezone != profile.timezone or (
                profile.currency is not None and tiktok_identity.currency != profile.currency
            ):
                raise AdhelmError(
                    "IDENTITY_MISMATCH", "TikTok Ads settings differ from the profile."
                )
            query = TikTokAdsReportQuery.model_validate(
                {**query.model_dump(), "account_ref": tiktok_identity.account_ref}
            )
            report = (
                tiktok.run_campaign_report(query, identity=tiktok_identity)
                if query.level == "CAMPAIGN"
                else tiktok.run_ad_report(query, identity=tiktok_identity)
            )
        elif isinstance(query, LinkedInAdsReportQuery):
            linkedin = LinkedInAdsClient(SafeHttpClient(client, {"api.linkedin.com"}), getter)
            linkedin_identity = linkedin.discover_identity(
                profile.account_ref, expected_account_client_ref=profile.account_ref
            )
            if profile.currency is not None and linkedin_identity.currency != profile.currency:
                raise AdhelmError(
                    "IDENTITY_MISMATCH", "LinkedIn currency differs from the profile."
                )
            query = LinkedInAdsReportQuery.model_validate(
                {**query.model_dump(), "account_ref": linkedin_identity.account_ref}
            )
            report = (
                linkedin.run_campaign_report(query, identity=linkedin_identity)
                if query.level == "CAMPAIGN"
                else linkedin.run_ad_report(query, identity=linkedin_identity)
            )
        else:
            reddit = RedditAdsClient(SafeHttpClient(client, {"ads-api.reddit.com"}), getter)
            reddit_identity = reddit.discover_identity(
                profile.account_ref, expected_account_client_ref=profile.account_ref
            )
            if (
                reddit_identity.timezone is not None
                and reddit_identity.timezone != profile.timezone
            ) or (profile.currency is not None and reddit_identity.currency != profile.currency):
                raise AdhelmError("IDENTITY_MISMATCH", "Reddit settings differ from the profile.")
            query = RedditAdsReportQuery.model_validate(
                {**query.model_dump(), "account_ref": reddit_identity.account_ref}
            )
            report = (
                reddit.run_campaign_report(query, identity=reddit_identity)
                if query.level == "CAMPAIGN"
                else reddit.run_ad_report(query, identity=reddit_identity)
            )
    report = _report_provenance(report, query, checkpoint)
    stored = persist_report(ledger, report) if not ephemeral else None
    shared = _share_report(root, ledger, report, stored, key, artifact_preflight)
    if isinstance(query, GoogleAdsReportQuery) and query.include_campaign_names:
        from adhelm.report_sharing import report_for_output

        shared["performance_rows"] = report_for_output(report, key, include_campaign_names=True)[
            "performance_rows"
        ]
    if ga4_aggregate is not None:
        # Report-level GA4 completeness and quality facts that the normalized
        # rows cannot carry.  Rows are deliberately not repeated here.
        shared["ga4_aggregate"] = ga4_aggregate.quality_summary()
    return shared


def _report_provenance(
    report: NormalizedReport, query: ReportQuery, checkpoint: dict[str, Any]
) -> NormalizedReport:
    checkpoints = [
        LedgerCheckpoint(
            stream_id=value["stream_id"],
            high_watermark_sequence=value["high_watermark"],
            head_digest=value["head_digest"],
        )
        for value in checkpoint["streams"].values()
    ]
    envelope = report.envelope.model_copy(
        update={
            "source_ledger_checkpoints": checkpoints,
            "query_contract_hash": digest(
                query.model_dump(mode="json", exclude={"query_contract_hash", "report_id"})
            ),
        }
    )
    return NormalizedReport(
        envelope=envelope,
        performance_rows=report.performance_rows,
        conversion_rows=report.conversion_rows,
    )


def _share_report(
    root: Path,
    ledger: Ledger,
    report: NormalizedReport,
    stored: dict[str, Any] | None,
    key: bytes,
    artifact_preflight: ReportArtifactPreflight,
) -> dict[str, Any]:
    shared = cast(dict[str, Any], _redact_payload(report.model_dump(mode="json"), key, False))
    shared["retained_record_digest"] = stored["record_digest"] if stored else None
    if stored is not None:
        artifact_result = finalize_report_artifacts(
            root,
            ledger,
            stored_record=stored,
            preflight=artifact_preflight,
            key=key,
        )
        shared["artifact_result"] = artifact_result.to_data()
    else:
        shared["artifact_result"] = {
            "effective_policy": {
                "outcome": artifact_preflight.completion.outcome,
                "reason": artifact_preflight.completion.reason,
                "formats": list(artifact_preflight.completion.formats),
                "preferences_digest": artifact_preflight.completion.preferences_digest,
            },
            "source_record_digest": None,
            "checkpoint_digest": None,
            "reused_prior": False,
            "generation_failure_code": None,
            "artifacts": [],
        }
    return shared


def persist_report(
    ledger: Ledger,
    report: NormalizedReport,
    *,
    _before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Append a report revision. Existing observations are never overwritten."""
    observation_key = "observation:" + digest(
        {
            "provider": report.envelope.provider,
            "account": report.envelope.source_account_ref,
            "query": report.envelope.query_contract_hash,
        }
    )
    prior = [
        record
        for record in ledger.records("performance")
        if record["record_kind"] == "PERFORMANCE_OBSERVATION"
        and record["payload"]["observation_key"] == observation_key
    ]
    revision = max((r["payload"]["observation_revision"] for r in prior), default=0) + 1
    payload = report.to_ledger_payload(
        observation_key=observation_key,
        observation_revision=revision,
        source_report_digest=digest(report),
        query_as_of=datetime.now(UTC),
        supersedes_record_id=prior[-1]["record_id"] if prior else None,
    )
    return ledger.append(
        "performance",
        "PERFORMANCE_OBSERVATION",
        payload,
        provider=report.envelope.provider,
        account_ref=report.envelope.source_account_ref,
        _before_durable_append=_before_durable_append,
    )
