# ruff: noqa: E501
"""Strict normalized aggregate reporting contracts.

These models deliberately represent only reviewed aggregate fields.  Provider
responses stay in memory and are parsed into these types before reaching the
ledger or any agent-visible output.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Final, Literal

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import digest
from adhelm.models import Currency, Money, Provider, SafeRef, StrictModel
from adhelm.privacy import validate_data

Hash = str
QualityStatus = Literal[
    "COMPLETE",
    "PROCESSING_PENDING",
    "PARTIAL_PAGE_FAILURE",
    "STALE",
    "SAMPLED",
    "THRESHOLDED",
    "MISSING_EVENT",
    "DUPLICATE_FIRING",
    "TIMEZONE_MISMATCH",
    "CURRENCY_MISMATCH",
    "ATTRIBUTION_MISMATCH",
    "UNMAPPED",
    "AMBIGUOUS_REMOTE_STATE",
    "PROVIDER_REPORTED_UNKNOWN",
    "MIXED_CREATIVE_VERSION",
    "CREATIVE_NOT_EXPOSED",
    "COMBINATION_UNKNOWN",
    "CREATIVE_METADATA_MISSING",
    "EXTRACTION_FAILED",
    "MINIMUM_CELL_SUPPRESSED",
    "SOURCE_EDITION_UNAVAILABLE",
    "NOT_AVAILABLE_FROM_PROVIDER",
    "NOT_SUPPORTED_BY_ANALYZER",
]
ResourceLevel = Literal[
    "ACCOUNT",
    "CAMPAIGN",
    "AD_GROUP",
    "AD",
    "CREATIVE",
    "KEYWORD",
    "SEARCH_TERM",
    "PLACEMENT",
    "TARGET_SEGMENT",
    "CONVERSION_ACTION",
]
JoinStatus = Literal[
    "EXACT_PROVIDER_ID",
    "EXACT_CONTROLLED_KEY",
    "EXPLICIT_MAPPING",
    # A candidate name match.  `docs/reporting-schema.md` requires it to stay
    # visible and excluded from authoritative totals; it is never applied as a
    # join.  Ticket M00.2 produces it from the proposal tier.
    "HEURISTIC",
    "UNMAPPED",
    "CONFLICT",
]
# How a joined row came to be joined.  A join is only ever a customer
# declaration; `ACCEPTED_HEURISTIC` records that the declaration originated in
# a proposal the customer then accepted in the contract.
JoinSource = Literal["DECLARED", "ACCEPTED_HEURISTIC"]
MetricGrain = Literal["ACCOUNT", "CAMPAIGN", "AD_GROUP", "AD", "RENDITION", "COMPONENT", "UNKNOWN"]
_MAX_MONEY_MINOR_UNITS = 9_007_199_254_740_991
_LINKEDIN_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def canonical_metric_decimal(value: object) -> str:
    """Preserve a bounded nonnegative provider decimal without a binary-float round trip."""
    if type(value) not in {int, str, Decimal}:
        raise ValueError("Expected an exact provider decimal")
    text = str(value)
    if len(text) > 80 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", text):
        raise ValueError("Invalid provider decimal")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError("Invalid provider decimal") from None
    if not number.is_finite() or number < 0 or number > _MAX_MONEY_MINOR_UNITS:
        raise ValueError("Provider decimal exceeds bounds")
    exponent = number.as_tuple().exponent
    if not isinstance(exponent, int) or not -18 <= exponent <= 18:
        raise ValueError("Provider decimal precision exceeds bounds")
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone offset")
    return value


def _linkedin_native_amount(value: str) -> Decimal:
    """Validate LinkedIn's retained BigDecimal wire string without rounding it."""
    if (
        not 1 <= len(value) <= 80
        or not value.isascii()
        or _LINKEDIN_DECIMAL.fullmatch(value) is None
    ):
        raise ValueError("LinkedIn native spend must be an exact nonnegative decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(
            "LinkedIn native spend must be an exact nonnegative decimal string"
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("LinkedIn native spend must be an exact nonnegative decimal string")
    return amount


def _linkedin_rounded_minor(value: str) -> int:
    amount = _linkedin_native_amount(value)
    with localcontext() as context_precision:
        context_precision.prec = 100
        rounded = (amount * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    if rounded > _MAX_MONEY_MINOR_UNITS:
        raise ValueError("LinkedIn native spend exceeds the supported money bound")
    return int(rounded)


class LedgerCheckpoint(StrictModel):
    stream_id: SafeRef
    high_watermark_sequence: int = Field(ge=0)
    head_digest: str | None = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def empty_stream_has_no_head(self) -> LedgerCheckpoint:
        if (self.high_watermark_sequence == 0) != (self.head_digest is None):
            raise ValueError("Only an empty stream has no head digest")
        return self


class ReportEnvelope(StrictModel):
    """Query provenance shared by every normalized aggregate row."""

    schema_version: Literal[1] = 1
    report_id: SafeRef
    provider: Provider
    source_account_ref: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_start: date
    window_end: date
    source_timezone: str = Field(min_length=1, max_length=80)
    report_timezone: str = Field(min_length=1, max_length=80)
    currency: Currency | None = None
    attribution_model: str = Field(min_length=1, max_length=120)
    attribution_window: str | None = Field(default=None, max_length=120)
    observed_at: datetime
    data_fresh_through: datetime | None = None
    data_quality_statuses: list[QualityStatus] = Field(default_factory=list, max_length=32)
    provider_api_version: str = Field(min_length=1, max_length=80)
    connector_version: str = Field(min_length=1, max_length=80)
    source_ledger_checkpoints: list[LedgerCheckpoint] = Field(default_factory=list, max_length=8)

    @field_validator("observed_at", "data_fresh_through")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @model_validator(mode="after")
    def valid_window(self) -> ReportEnvelope:
        if self.window_end < self.window_start:
            raise ValueError("Report window end must not precede its start")
        if self.data_fresh_through is not None and self.data_fresh_through > self.observed_at:
            raise ValueError("Data freshness cannot be after observation")
        return self


class PerformanceRow(StrictModel):
    """One aggregate row at one declared metric grain.

    No list of renditions/components is accepted.  An ad-grain row therefore
    cannot be copied across an eligible creative pool.
    """

    schema_version: Literal[1] = 1
    report_id: SafeRef
    resource_level: ResourceLevel
    metric_grain: MetricGrain
    resource_type: str = Field(min_length=1, max_length=80)
    resource_ref: SafeRef
    parent_resource_ref: SafeRef | None = None
    campaign_ref: SafeRef | None = None
    campaign_join_key: SafeRef | None = None
    creative_family_id: SafeRef | None = None
    creative_version_id: SafeRef | None = None
    served_rendition_id: SafeRef | None = None
    component_ref: SafeRef | None = None
    creative_deployment_id: SafeRef | None = None
    resource_status: str | None = Field(default=None, max_length=80)
    objective: str | None = Field(default=None, max_length=80)
    budget: Money | None = None
    spend_cap: Money | None = None
    bid_strategy: str | None = Field(default=None, max_length=80)
    bid_amount: Money | None = None
    target_amount: Money | None = None
    schedule_start: datetime | None = None
    schedule_end: datetime | None = None
    dimension_type: str | None = Field(default=None, max_length=80)
    dimension_value: str | None = Field(default=None, max_length=160)
    impressions: int | None = Field(default=None, ge=0)
    clicks: int | None = Field(default=None, ge=0)
    spend: Money | None = None
    reach: int | None = Field(default=None, ge=0)
    video_views: int | None = Field(default=None, ge=0)
    engagements: int | None = Field(default=None, ge=0)
    provider_reported_results: int | None = Field(default=None, ge=0)
    sessions: int | None = Field(default=None, ge=0)
    active_users: int | None = Field(default=None, ge=0)
    event_count: int | None = Field(default=None, ge=0)
    key_events: int | None = Field(default=None, ge=0)
    join_status: JoinStatus = "UNMAPPED"
    data_quality_statuses: list[QualityStatus] = Field(default_factory=list, max_length=32)
    provider_extensions: dict[str, str | int | bool | None] = Field(
        default_factory=dict, max_length=24
    )

    @field_validator("provider_extensions")
    @classmethod
    def typed_extensions_only(
        cls, value: dict[str, str | int | bool | None]
    ) -> dict[str, str | int | bool | None]:
        allowed = {
            "ga4_date",
            "ga4_sessionCampaignName",
            "ga4_sessionSource",
            "ga4_sessionMedium",
            "ga4_eventName",
            "ga4_totalUsers",
            "ga4_event_ref",
            "ga4_cell_status_sessions",
            "ga4_cell_status_totalUsers",
            "ga4_cell_status_keyEvents",
            "ga4_dimension_statuses",
            "ga4_join_source",
            "ga4_join_map_digest",
            "ga4_completeness",
            "ga4_declared_row_count",
            "ga4_consumed_row_count",
            "ga4_pages_consumed",
            "ga4_page_bound",
            "ga4_subject_to_thresholding",
            "ga4_other_row_aggregation",
            "ga4_sampling_consistency",
            "ga4_empty_reason",
            "ga4_restricted_metrics",
            "google_ads_cost_micros",
            "google_ads_rounding",
            "google_ads_campaign_name",
            "reddit_ads_spend_micros",
            "reddit_ads_rounding",
            "linkedin_ads_cost_in_local_currency",
            "linkedin_ads_rounding",
        }
        if any(key not in allowed or not isinstance(item, str) for key, item in value.items()):
            raise ValueError("Unsupported provider reporting extension")
        name = value.get("google_ads_campaign_name")
        if name is not None:
            assert isinstance(name, str)
            if not 1 <= len(name) <= 255 or not all(character.isprintable() for character in name):
                raise ValueError("Invalid campaign label")
            validate_data(name)
        native_keys = {
            "google_ads_cost_micros": "google_ads_rounding",
            "reddit_ads_spend_micros": "reddit_ads_rounding",
            "linkedin_ads_cost_in_local_currency": "linkedin_ads_rounding",
        }
        present = [key for key in native_keys if key in value]
        if len(present) > 1:
            raise ValueError("Only one provider native spend value may be retained")
        for native_key, rounding_key in native_keys.items():
            if native_key not in value:
                if rounding_key in value:
                    raise ValueError("Rounding provenance needs the original amount")
                continue
            native = value[native_key]
            if native_key == "linkedin_ads_cost_in_local_currency":
                assert isinstance(native, str)
                _linkedin_rounded_minor(native)
                if value.get(rounding_key) != "HALF_EVEN_TO_MINOR":
                    raise ValueError("Native spend normalization must declare its rounding policy")
                continue
            if (
                not isinstance(native, str)
                or not 1 <= len(native) <= 19
                or not native.isascii()
                or not native.isdecimal()
                or int(native) > 9_223_372_036_854_775_807
            ):
                raise ValueError("Native spend must be an exact nonnegative decimal integer")
            if value.get(rounding_key) != "HALF_EVEN_TO_MINOR":
                raise ValueError("Native spend normalization must declare its rounding policy")
        return value

    @field_validator("schedule_start", "schedule_end")
    @classmethod
    def schedule_is_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)

    @model_validator(mode="after")
    def metric_grain_is_honest(self) -> PerformanceRow:
        native = self.provider_extensions.get("google_ads_cost_micros")
        native = self.provider_extensions.get("reddit_ads_spend_micros", native)
        if native is not None:
            assert isinstance(native, str)
            rounded = int((Decimal(native) / 10_000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
            if (
                self.spend is None
                or self.spend.currency != "USD"
                or self.spend.minor_units != rounded
            ):
                raise ValueError("Normalized spend disagrees with retained native micros")
        linkedin_native = self.provider_extensions.get("linkedin_ads_cost_in_local_currency")
        if linkedin_native is not None:
            assert isinstance(linkedin_native, str)
            if (
                self.spend is None
                or self.spend.currency != "USD"
                or self.spend.minor_units != _linkedin_rounded_minor(linkedin_native)
            ):
                raise ValueError("Normalized spend disagrees with retained LinkedIn decimal")
        expected = {"ACCOUNT": "ACCOUNT", "CAMPAIGN": "CAMPAIGN", "AD_GROUP": "AD_GROUP"}
        if self.resource_level in expected:
            if self.metric_grain != expected[self.resource_level]:
                raise ValueError("Aggregate resource level and metric grain disagree")
            if any(
                (
                    self.creative_family_id,
                    self.creative_version_id,
                    self.creative_deployment_id,
                    self.served_rendition_id,
                    self.component_ref,
                )
            ):
                raise ValueError("Account/campaign/group aggregates cannot identify a creative")
        if self.schedule_start and self.schedule_end and self.schedule_end < self.schedule_start:
            raise ValueError("Schedule end must not precede schedule start")
        if self.metric_grain in {"ACCOUNT", "CAMPAIGN", "AD_GROUP", "AD", "UNKNOWN"}:
            if self.served_rendition_id is not None or self.component_ref is not None:
                raise ValueError("Aggregate metric grain cannot identify a rendition or component")
        elif self.metric_grain == "RENDITION":
            if self.served_rendition_id is None or self.component_ref is not None:
                raise ValueError("Rendition metrics require exactly one rendition")
        elif self.metric_grain == "COMPONENT":
            if self.component_ref is None:
                raise ValueError("Component metrics require exactly one component")
        if self.metric_grain in {"AD", "UNKNOWN"} and self.creative_version_id is not None:
            required = {"CREATIVE_NOT_EXPOSED", "COMBINATION_UNKNOWN"}
            if not required.intersection(self.data_quality_statuses):
                raise ValueError("Ad-level dynamic creative metrics require an exposure caveat")
        return self


class ConversionBreakdownRow(StrictModel):
    schema_version: Literal[1, 2] = 1
    report_id: SafeRef
    campaign_ref: SafeRef | None = None
    campaign_join_key: SafeRef | None = None
    resource_ref: SafeRef | None = None
    conversion_source: Literal["PROVIDER", "GA4", "HUBSPOT", "SALESFORCE"]
    conversion_action_ref: SafeRef | None = None
    conversion_contract_version_ref: SafeRef | None = None
    conversion_mapping_version_ref: SafeRef | None = None
    conversion_kind: str = Field(min_length=1, max_length=80)
    counting_method: str | None = Field(default=None, max_length=80)
    attribution_model: str = Field(min_length=1, max_length=120)
    attribution_window: str | None = Field(default=None, max_length=120)
    conversion_count: int | None = Field(default=None, ge=0)
    conversion_count_decimal: str | None = None
    conversion_value: Money | None = None
    join_status: JoinStatus = "UNMAPPED"
    data_quality_statuses: list[QualityStatus] = Field(default_factory=list, max_length=32)
    provider_extensions: dict[str, str | int | bool | None] = Field(
        default_factory=dict, max_length=24
    )

    @field_validator("provider_extensions")
    @classmethod
    def reviewed_extensions_only(
        cls, value: dict[str, str | int | bool | None]
    ) -> dict[str, str | int | bool | None]:
        # GA4 cell/report provenance and Google Ads exact aggregate provenance
        # are closed allowlists. Null remains distinct from a measured zero.
        allowed = {
            "ga4_cell_status_keyEvents",
            "ga4_dimension_statuses",
            "ga4_join_source",
            "ga4_join_map_digest",
            "ga4_completeness",
            "google_ads_date",
            "google_ads_conversion_value",
            "google_ads_rounding",
        }
        if any(key not in allowed or not isinstance(item, str) for key, item in value.items()):
            raise ValueError("Unsupported conversion reporting extension")
        return value

    @model_serializer(mode="wrap")
    def omit_absent_decimal(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        result: dict[str, object] = dict(handler(self))
        if self.conversion_count_decimal is None:
            result.pop("conversion_count_decimal", None)
        return result

    @model_validator(mode="after")
    def exact_conversion_values(self) -> ConversionBreakdownRow:
        if self.schema_version == 1 and (
            self.conversion_count_decimal is not None
            or any(key.startswith("google_ads_") for key in self.provider_extensions)
        ):
            raise ValueError("Exact Google Ads conversion fields require schema version 2")
        if self.conversion_count_decimal is not None:
            if (
                canonical_metric_decimal(self.conversion_count_decimal)
                != self.conversion_count_decimal
            ):
                raise ValueError("Conversion count must be a canonical decimal string")
            count = Decimal(self.conversion_count_decimal)
            expected = int(count) if count == count.to_integral_value() else None
            if self.conversion_count != expected:
                raise ValueError("Integer conversion count disagrees with exact decimal")
        native = self.provider_extensions.get("google_ads_conversion_value")
        if native is not None:
            if canonical_metric_decimal(native) != native:
                raise ValueError("Conversion value must be a canonical decimal string")
            if self.provider_extensions.get("google_ads_rounding") != "HALF_EVEN_TO_MINOR":
                raise ValueError("Conversion value must disclose rounding")
            with localcontext() as context:
                context.prec = 80
                minor = int((Decimal(native) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
            if self.conversion_value is None or self.conversion_value.minor_units != minor:
                raise ValueError("Conversion value disagrees with exact native value")
        return self


def upcast_conversion_row(row: ConversionBreakdownRow) -> ConversionBreakdownRow:
    """Produce a v2 view without modifying the original persisted v1 row."""
    values = row.model_dump(mode="python")
    if row.schema_version == 1:
        values["schema_version"] = 2
        values["conversion_count_decimal"] = (
            str(row.conversion_count) if row.conversion_count is not None else None
        )
    return ConversionBreakdownRow.model_validate(values)


class PerformanceObservationPayload(StrictModel):
    """The exact nested payload expected by the append-only performance ledger."""

    observation_key: SafeRef
    observation_revision: int = Field(ge=1)
    supersedes_record_id: SafeRef | None = None
    query_as_of: datetime | None = None
    source_report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    report_envelope: ReportEnvelope
    performance_rows: list[PerformanceRow] = Field(default_factory=list, max_length=10_000)
    conversion_rows: list[ConversionBreakdownRow] = Field(default_factory=list, max_length=10_000)

    @field_validator("query_as_of")
    @classmethod
    def query_as_of_is_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)


class NormalizedReport(StrictModel):
    envelope: ReportEnvelope
    performance_rows: list[PerformanceRow] = Field(default_factory=list, max_length=10_000)
    conversion_rows: list[ConversionBreakdownRow] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def consistent_and_nonduplicated(self) -> NormalizedReport:
        report_id = self.envelope.report_id
        if any(row.report_id != report_id for row in self.performance_rows):
            raise ValueError("Performance row belongs to a different report")
        if any(row.report_id != report_id for row in self.conversion_rows):
            raise ValueError("Conversion row belongs to a different report")
        currency = self.envelope.currency
        allowed_prefix = {
            "ga4": "ga4_",
            "google_ads": "google_ads_",
            "reddit_ads": "reddit_ads_",
            "linkedin_ads": "linkedin_ads_",
        }.get(self.envelope.provider)
        for performance in self.performance_rows:
            if any(
                allowed_prefix is None or not key.startswith(allowed_prefix)
                for key in performance.provider_extensions
            ):
                raise ValueError("Reporting extensions must belong to the declared provider")
            for amount in (
                performance.budget,
                performance.spend_cap,
                performance.bid_amount,
                performance.target_amount,
                performance.spend,
            ):
                if amount is not None and amount.currency != currency:
                    raise ValueError("Money currency disagrees with the report envelope")
        for conversion in self.conversion_rows:
            if any(
                allowed_prefix is None or not key.startswith(allowed_prefix)
                for key in conversion.provider_extensions
            ):
                raise ValueError("Conversion extensions must belong to the declared provider")
            if (
                conversion.conversion_value is not None
                and conversion.conversion_value.currency != currency
            ):
                raise ValueError("Conversion currency disagrees with the report envelope")

        # This intentionally ignores creative identity: cloning one ad-grain
        # metric across creative versions/renditions is a fanout defect.
        seen: set[tuple[str, str, str | None, str | None, str, str, str | None]] = set()
        for row in self.performance_rows:
            key = (
                row.resource_ref,
                row.metric_grain,
                row.dimension_type,
                row.dimension_value,
                row.resource_level,
                digest(row.provider_extensions),
                row.served_rendition_id if row.metric_grain == "RENDITION" else row.component_ref,
            )
            if key in seen:
                raise ValueError("Duplicate metric row would fan out aggregate performance")
            seen.add(key)
        return self

    def to_ledger_payload(
        self,
        *,
        observation_key: str,
        observation_revision: int,
        source_report_digest: str,
        query_as_of: datetime | None = None,
        supersedes_record_id: str | None = None,
    ) -> dict[str, object]:
        """Return only the reviewed payload shape accepted by ``Ledger.append``."""
        payload = PerformanceObservationPayload(
            observation_key=observation_key,
            observation_revision=observation_revision,
            supersedes_record_id=supersedes_record_id,
            query_as_of=query_as_of,
            source_report_digest=source_report_digest,
            report_envelope=self.envelope,
            performance_rows=self.performance_rows,
            conversion_rows=self.conversion_rows,
        )
        return payload.model_dump(mode="json")


class CampaignJoin(StrictModel):
    status: JoinStatus
    provider_campaign_ref: SafeRef
    source_campaign_ref: SafeRef | None = None
    campaign_join_key: SafeRef | None = None


def resolve_campaign_join(
    *,
    provider_campaign_ref: str,
    source_campaign_ref: str | None,
    provider_exact_id: str | None,
    source_exact_id: str | None,
    provider_controlled_key: str | None,
    source_controlled_key: str | None,
    explicit_mapping: Mapping[str, str] | None = None,
) -> CampaignJoin:
    """Resolve only an exact ID, controlled key, or caller-approved mapping.

    Campaign names, fuzzy matching, time proximity, and inferred values are
    deliberately absent from this interface.
    """
    validate_data(
        {
            "provider_campaign_ref": provider_campaign_ref,
            "source_campaign_ref": source_campaign_ref,
            "provider_exact_id": provider_exact_id,
            "source_exact_id": source_exact_id,
            "provider_controlled_key": provider_controlled_key,
            "source_controlled_key": source_controlled_key,
            "explicit_mapping": dict(explicit_mapping or {}),
        }
    )
    exact = (
        provider_exact_id is not None
        and source_exact_id is not None
        and provider_exact_id == source_exact_id
    )
    controlled = (
        provider_controlled_key is not None
        and source_controlled_key is not None
        and provider_controlled_key == source_controlled_key
    )
    mapped = (
        explicit_mapping is not None
        and provider_campaign_ref in explicit_mapping
        and source_campaign_ref is not None
        and explicit_mapping[provider_campaign_ref] == source_campaign_ref
    )
    matches = sum((exact, controlled, mapped))
    if matches > 1:
        return CampaignJoin(status="CONFLICT", provider_campaign_ref=provider_campaign_ref)
    if exact:
        return CampaignJoin(
            status="EXACT_PROVIDER_ID",
            provider_campaign_ref=provider_campaign_ref,
            source_campaign_ref=source_campaign_ref,
        )
    if controlled:
        return CampaignJoin(
            status="EXACT_CONTROLLED_KEY",
            provider_campaign_ref=provider_campaign_ref,
            source_campaign_ref=source_campaign_ref,
            campaign_join_key=provider_controlled_key,
        )
    if mapped:
        return CampaignJoin(
            status="EXPLICIT_MAPPING",
            provider_campaign_ref=provider_campaign_ref,
            source_campaign_ref=source_campaign_ref,
        )
    return CampaignJoin(status="UNMAPPED", provider_campaign_ref=provider_campaign_ref)


# ---------------------------------------------------------------------------
# GA4 Data API fixed aggregate contract (M00)
#
# These models are additive and GA4-specific.  They deliberately do not change
# any existing provider report shape.  Their purpose is to keep a withheld,
# thresholded, restricted, or never-returned measurement distinguishable from a
# real observed zero, which `PerformanceRow`'s optional integers alone cannot
# express for a declared key-event universe.
#
# Contract and citations: docs/implementation/ga4-data-aggregates.md and
# docs/research/ga4-data-aggregate-contract.md.
# ---------------------------------------------------------------------------

Ga4AggregateShape = Literal[
    "CAMPAIGN_SOURCE_MEDIUM",
    "CAMPAIGN_SOURCE_MEDIUM_KEY_EVENT",
    "KEY_EVENT_TOTALS",
]
Ga4AggregateMetric = Literal["sessions", "totalUsers", "keyEvents"]
Ga4CellStatus = Literal[
    "OBSERVED",
    "SAMPLED_ESTIMATE",
    "THRESHOLD_WITHHELD",
    "RESTRICTED",
    "NOT_RETURNED",
]
Ga4Completeness = Literal[
    "COMPLETE",
    "EMPTY",
    # GA4 declared the read subject to thresholding and returned no rows, so
    # rows may have been suppressed rather than absent.
    "EMPTY_THRESHOLDED",
    # Pagination reconciled, but thresholding withheld at least one declared
    # key event, so the returned rows are not every matching row.
    "WITHHELD",
    # GA4 declined to report, with a reason it does not enumerate.
    "NOT_AVAILABLE_FROM_PROVIDER",
    "INCOMPLETE_PAGE_BOUND_EXCEEDED",
    "INCOMPLETE_ROW_COUNT_MISMATCH",
]
Ga4SamplingConsistency = Literal[
    "NOT_SAMPLED",
    "CONSISTENT",
    # Pages disagreed about the sampling metadata for the single date range.
    "SAMPLING_METADATA_INCONSISTENT",
]
Ga4DimensionStatus = Literal[
    "CAMPAIGN_NOT_SET",
    "CAMPAIGN_OTHER_ROW",
    "SOURCE_NOT_SET",
    "SOURCE_OTHER_ROW",
    "MEDIUM_NOT_SET",
    "MEDIUM_OTHER_ROW",
    # GA4 rolled high-cardinality event names into its documented "(other)"
    # bucket.  A dimension filter cannot exclude it, so it is an expected
    # aggregation fact rather than an undeclared event.
    "EVENT_OTHER_ROW",
    "KEY_EVENT_NOT_RETURNED",
]
# The Data API exposes no attribution model on `runReport` or its
# `ResponseMetaData`.  Attribution settings live only on the separate Admin API
# v1alpha `properties.getAttributionSettings` surface, which the read-only
# reporting profile deliberately does not call.
GA4_ATTRIBUTION_DISCLOSURE: Final = "GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED"
# The Data API returns no per-row or per-report freshness instant.  Only the
# local observation time is provable, so `data_fresh_through` stays null.
GA4_FRESHNESS_DISCLOSURE: Final = "NO_PROVIDER_FRESHNESS_FIELD_OBSERVED_AT_ONLY"
# AGENTS rule 16 and docs/measurement-control.md: a GA4 key event is never a
# provider conversion or another provider's native signal.
GA4_KEY_EVENT_DISCLOSURE: Final = "GA4_KEY_EVENT_IS_NOT_A_PROVIDER_CONVERSION"
# Deterministic provider-campaign joins are ticket M00.2.  A report read without
# a declared join map keeps this disclosure: rows carry the exact returned
# dimension values so the join can be applied later without re-reading GA4.
GA4_JOIN_DISCLOSURE: Final = "DETERMINISTIC_PROVIDER_JOIN_DEFERRED"
# A report whose rows were resolved against a retained, customer-declared join
# map (ticket M00.2).  The digest of that map is carried on every resolved row.
GA4_JOIN_APPLIED_DISCLOSURE: Final = "DETERMINISTIC_PROVIDER_JOIN_APPLIED_FROM_DECLARED_JOIN_MAP"
Ga4JoinDisclosure = Literal[
    "DETERMINISTIC_PROVIDER_JOIN_DEFERRED",
    "DETERMINISTIC_PROVIDER_JOIN_APPLIED_FROM_DECLARED_JOIN_MAP",
]


class Ga4SamplingWindow(StrictModel):
    """One `samplingMetadatas` entry, positionally bound to its date range."""

    date_range_index: int = Field(ge=0, le=3)
    samples_read_count: int = Field(ge=0, le=_MAX_MONEY_MINOR_UNITS)
    sampling_space_size: int = Field(ge=0, le=_MAX_MONEY_MINOR_UNITS)


class Ga4MetricRestriction(StrictModel):
    """One `schemaRestrictionResponse.activeMetricRestrictions` entry."""

    metric_name: str = Field(min_length=1, max_length=160)
    restricted_metric_types: tuple[str, ...] = Field(default_factory=tuple, max_length=8)


class Ga4MetricCell(StrictModel):
    """One measurement whose availability is explicit rather than numeric.

    A withheld, restricted, or never-returned cell carries ``value is None``.
    Zero is reserved for a value GA4 actually reported.
    """

    metric: Ga4AggregateMetric
    status: Ga4CellStatus
    value: int | None = Field(default=None, ge=0, le=_MAX_MONEY_MINOR_UNITS)

    @model_validator(mode="after")
    def only_measured_cells_carry_a_number(self) -> Ga4MetricCell:
        measured = self.status in {"OBSERVED", "SAMPLED_ESTIMATE"}
        if measured != (self.value is not None):
            raise ValueError("Only an observed or sampled GA4 cell may carry a number")
        return self


class Ga4AggregateRow(StrictModel):
    """One fixed aggregate row carrying unmodified GA4 dimension values."""

    schema_version: Literal[1] = 1
    report_id: SafeRef
    aggregate_shape: Ga4AggregateShape
    session_campaign_name: str | None = Field(default=None, max_length=160)
    session_source: str | None = Field(default=None, max_length=160)
    session_medium: str | None = Field(default=None, max_length=160)
    event_name: str | None = Field(default=None, max_length=160)
    conversion_action_ref: SafeRef | None = None
    dimension_statuses: tuple[Ga4DimensionStatus, ...] = Field(default_factory=tuple, max_length=8)
    cells: tuple[Ga4MetricCell, ...] = Field(min_length=1, max_length=8)
    join_status: JoinStatus = "UNMAPPED"
    # Populated only by ticket M00.2, from a retained customer-declared join
    # map.  A heuristic proposal, a conflict, and an unmatched value never mint
    # a provider campaign identity here.
    join_source: JoinSource | None = None
    join_map_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    provider_campaign_ref: SafeRef | None = None
    campaign_join_key: SafeRef | None = None
    join_disclosure: Ga4JoinDisclosure = GA4_JOIN_DISCLOSURE
    data_quality_statuses: list[QualityStatus] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def shape_determines_present_dimensions(self) -> Ga4AggregateRow:
        traffic = self.aggregate_shape in {
            "CAMPAIGN_SOURCE_MEDIUM",
            "CAMPAIGN_SOURCE_MEDIUM_KEY_EVENT",
        }
        present = (
            self.session_campaign_name is not None
            and self.session_source is not None
            and self.session_medium is not None
        )
        absent = (
            self.session_campaign_name is None
            and self.session_source is None
            and self.session_medium is None
        )
        if traffic and not present:
            raise ValueError("Traffic-source aggregates require every session dimension")
        if not traffic and not absent:
            raise ValueError("Key-event totals carry no session dimensions")
        if (self.aggregate_shape != "CAMPAIGN_SOURCE_MEDIUM") != (self.event_name is not None):
            raise ValueError("Only key-event aggregates carry an event name")
        rolled_up = "EVENT_OTHER_ROW" in self.dimension_statuses
        expects_reference = self.aggregate_shape == "KEY_EVENT_TOTALS" and not rolled_up
        if expects_reference != (self.conversion_action_ref is not None):
            raise ValueError("Only a named key-event total carries a conversion action reference")
        if rolled_up and self.event_name is None:
            raise ValueError("An event roll-up row requires the returned event dimension value")
        if len(self.dimension_statuses) != len(set(self.dimension_statuses)):
            raise ValueError("A GA4 aggregate row cannot repeat one dimension status")
        if len(self.data_quality_statuses) != len(set(self.data_quality_statuses)):
            raise ValueError("A GA4 aggregate row cannot repeat one quality status")
        metrics = [cell.metric for cell in self.cells]
        if len(metrics) != len(set(metrics)):
            raise ValueError("A GA4 aggregate row cannot repeat one metric")
        # Join state is explicit.  A resolved row names the exact map that
        # resolved it, and only a declared or explicitly accepted mapping may
        # carry a provider campaign identity.
        resolved = self.join_map_digest is not None
        if resolved != (self.join_disclosure == GA4_JOIN_APPLIED_DISCLOSURE):
            raise ValueError("A resolved GA4 row must declare the applied join disclosure")
        joined = self.join_status in {"EXPLICIT_MAPPING", "EXACT_CONTROLLED_KEY"}
        if not resolved and (joined or self.join_status in {"HEURISTIC", "CONFLICT"}):
            raise ValueError("A joined, heuristic, or conflicting row requires a join map digest")
        if joined != (self.provider_campaign_ref is not None):
            raise ValueError("Only a declared GA4 join identifies a provider campaign")
        if joined != (self.join_source is not None):
            raise ValueError("Only a declared GA4 join records a join source")
        if not joined and self.campaign_join_key is not None:
            raise ValueError("Only a declared GA4 join carries a campaign join key")
        if ("UNMAPPED" in self.data_quality_statuses) != (self.join_status == "UNMAPPED"):
            raise ValueError("The UNMAPPED quality status belongs to an unmapped GA4 row")
        return self


class Ga4AggregateReport(StrictModel):
    """A fixed GA4 aggregate read with explicit completeness and quality.

    ``consumed_row_count`` is what this read actually retrieved.  It is never
    presented as exhaustive unless ``completeness`` is ``COMPLETE``.
    """

    schema_version: Literal[1] = 1
    envelope: ReportEnvelope
    aggregate_shape: Ga4AggregateShape
    completeness: Ga4Completeness
    declared_row_count: int | None = Field(default=None, ge=0)
    consumed_row_count: int = Field(ge=0)
    pages_consumed: int = Field(ge=0)
    page_bound: int = Field(ge=1)
    page_size: int = Field(ge=1)
    # Property-local inclusive calendar boundary plus the exact UTC instants it
    # denotes.  GA4 `DateRange.startDate`/`endDate` are both inclusive.
    window_boundary: Literal["INCLUSIVE_START_INCLUSIVE_END_PROPERTY_LOCAL_DATES"] = (
        "INCLUSIVE_START_INCLUSIVE_END_PROPERTY_LOCAL_DATES"
    )
    window_start_instant: datetime
    window_end_exclusive_instant: datetime
    property_timezone: str = Field(min_length=1, max_length=80)
    property_currency: Currency
    subject_to_thresholding: bool
    other_row_aggregation: bool
    # One window per requested date range.  The adapter sends exactly one, so
    # pages must agree; disagreement is recorded rather than silently merged.
    sampling: tuple[Ga4SamplingWindow, ...] = Field(default_factory=tuple, max_length=4)
    sampling_consistency: Ga4SamplingConsistency = "NOT_SAMPLED"
    restricted_metrics: tuple[Ga4MetricRestriction, ...] = Field(
        default_factory=tuple, max_length=32
    )
    empty_reason: str | None = Field(default=None, max_length=160)
    declared_key_events: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    attribution_disclosure: Literal["GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED"] = (
        GA4_ATTRIBUTION_DISCLOSURE
    )
    freshness_disclosure: Literal["NO_PROVIDER_FRESHNESS_FIELD_OBSERVED_AT_ONLY"] = (
        GA4_FRESHNESS_DISCLOSURE
    )
    key_event_disclosure: Literal["GA4_KEY_EVENT_IS_NOT_A_PROVIDER_CONVERSION"] = (
        GA4_KEY_EVENT_DISCLOSURE
    )
    rows: tuple[Ga4AggregateRow, ...] = Field(default_factory=tuple, max_length=10_000)

    @field_validator("window_start_instant", "window_end_exclusive_instant")
    @classmethod
    def boundary_is_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def completeness_is_provable(self) -> Ga4AggregateReport:
        if self.envelope.provider != "ga4":
            raise ValueError("A GA4 aggregate report must declare the ga4 provider")
        if self.envelope.data_fresh_through is not None:
            raise ValueError("GA4 exposes no freshness instant to record")
        if self.window_end_exclusive_instant <= self.window_start_instant:
            raise ValueError("The exclusive window end must follow its inclusive start")
        if self.envelope.currency != self.property_currency:
            raise ValueError("Report currency must be the bound property currency")
        if self.envelope.report_timezone != self.property_timezone:
            raise ValueError("Report timezone must be the bound property timezone")
        if any(row.report_id != self.envelope.report_id for row in self.rows):
            raise ValueError("Aggregate row belongs to a different report")
        if any(row.aggregate_shape != self.aggregate_shape for row in self.rows):
            raise ValueError("Aggregate row belongs to a different request shape")
        if self.pages_consumed > self.page_bound:
            raise ValueError("Consumed pages cannot exceed the configured page bound")
        if self.completeness == "COMPLETE":
            if self.declared_row_count is None:
                raise ValueError("A complete GA4 report must reconcile a declared row count")
            if self.declared_row_count != self.consumed_row_count:
                raise ValueError("A complete GA4 report must consume every declared row")
            if self.empty_reason is not None:
                raise ValueError("A complete GA4 report is not empty")
        if self.completeness in {"EMPTY", "EMPTY_THRESHOLDED"} and self.consumed_row_count:
            raise ValueError("An empty GA4 report consumed no provider rows")
        if (self.completeness == "EMPTY_THRESHOLDED") and not self.subject_to_thresholding:
            raise ValueError("A thresholded empty report must record the provider flag")
        if (self.completeness == "NOT_AVAILABLE_FROM_PROVIDER") != (self.empty_reason is not None):
            raise ValueError("A provider-declined report is exactly one with an empty reason")
        withheld = any(
            cell.status == "THRESHOLD_WITHHELD" for row in self.rows for cell in row.cells
        )
        if withheld and not self.subject_to_thresholding:
            raise ValueError("A withheld cell requires the provider thresholding flag")
        if withheld and self.completeness in {"COMPLETE", "EMPTY"}:
            # A table of suppressed key events is neither complete nor empty.
            raise ValueError("A report carrying withheld rows is neither complete nor empty")
        if self.completeness == "WITHHELD" and not withheld:
            raise ValueError("A withheld report must carry at least one withheld measurement")
        if (self.sampling_consistency == "NOT_SAMPLED") != (not self.sampling):
            raise ValueError("Sampling windows and the sampling consistency status disagree")
        indexes = [item.date_range_index for item in self.sampling]
        if len(indexes) != len(set(indexes)):
            raise ValueError("Each sampling window belongs to one distinct date range")
        if self.completeness == "INCOMPLETE_PAGE_BOUND_EXCEEDED" and (
            self.pages_consumed != self.page_bound
        ):
            raise ValueError("A page-bound refusal must have consumed the whole bound")
        if len({row.join_map_digest for row in self.rows if row.join_map_digest is not None}) > 1:
            raise ValueError("A GA4 report cannot mix two declared join maps")
        return self

    def rolled_up(self) -> bool:
        """Whether any retained row is one of GA4's "(other)" buckets.

        A row marked as a roll-up is itself proof that GA4 hid members, whether
        or not the response also set ``dataLossFromOtherRow``.  This is derived
        from the rows rather than trusted from the report-level flag, so a
        report rebuilt by a later ticket (M00.2 reconstructs these rows for the
        deterministic join) cannot lose the fact by leaving the flag unset.
        """
        return self.other_row_aggregation or any(
            status.endswith("_OTHER_ROW") for row in self.rows for status in row.dimension_statuses
        )

    def exhaustive(self) -> bool:
        """Whether this read may be presented as every matching GA4 row.

        Pagination reconciling is necessary but not sufficient: a report GA4
        declared subject to thresholding, rolled into its "(other)" bucket, or
        declined with an unenumerated `emptyReason` is not proof that the
        returned rows are every matching row.
        """
        return (
            self.completeness in {"COMPLETE", "EMPTY"}
            and not self.subject_to_thresholding
            and not self.rolled_up()
            and self.empty_reason is None
        )

    def join_map_digest(self) -> str | None:
        """The single declared join map that resolved these rows, if any."""
        digests = {row.join_map_digest for row in self.rows if row.join_map_digest is not None}
        return next(iter(digests), None)

    def join_disclosure(self) -> str:
        """Whether the deterministic provider join is still deferred (M00.2)."""
        return (
            GA4_JOIN_APPLIED_DISCLOSURE
            if self.join_map_digest() is not None
            else GA4_JOIN_DISCLOSURE
        )

    def join_status_counts(self) -> dict[str, int]:
        """Every join state stays countable, including the excluded ones."""
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.join_status] = counts.get(row.join_status, 0) + 1
        return dict(sorted(counts.items()))

    def quality_summary(self) -> dict[str, object]:
        """Bounded report-level facts for agent-visible output.

        Rows are excluded: they are already carried by the normalized report.
        """
        return {
            "aggregate_shape": self.aggregate_shape,
            "completeness": self.completeness,
            "exhaustive": self.exhaustive(),
            "declared_row_count": self.declared_row_count,
            "consumed_row_count": self.consumed_row_count,
            "pages_consumed": self.pages_consumed,
            "page_bound": self.page_bound,
            "window_boundary": self.window_boundary,
            "window_start_instant": self.window_start_instant.isoformat(),
            "window_end_exclusive_instant": self.window_end_exclusive_instant.isoformat(),
            "property_timezone": self.property_timezone,
            "property_currency": self.property_currency,
            "subject_to_thresholding": self.subject_to_thresholding,
            "other_row_aggregation": self.rolled_up(),
            "sampling": [item.model_dump(mode="json") for item in self.sampling],
            "sampling_consistency": self.sampling_consistency,
            "restricted_metrics": [
                item.model_dump(mode="json") for item in self.restricted_metrics
            ],
            "empty_reason": self.empty_reason,
            "attribution_disclosure": self.attribution_disclosure,
            "freshness_disclosure": self.freshness_disclosure,
            "key_event_disclosure": self.key_event_disclosure,
            "join_disclosure": self.join_disclosure(),
            "join_map_digest": self.join_map_digest(),
            "join_status_counts": self.join_status_counts(),
        }

    def _report_provenance(self) -> dict[str, str]:
        """Report-level completeness facts that must reach the canonical ledger.

        `quality_summary()` is agent-visible output only.  Under AGENTS rule 22
        the append-only history is canonical, so a later reader of a stored GA4
        observation must be able to see how complete that read was.
        """
        provenance = {
            "ga4_completeness": self.completeness,
            "ga4_declared_row_count": (
                "null" if self.declared_row_count is None else str(self.declared_row_count)
            ),
            "ga4_consumed_row_count": str(self.consumed_row_count),
            "ga4_pages_consumed": str(self.pages_consumed),
            "ga4_page_bound": str(self.page_bound),
            "ga4_subject_to_thresholding": "true" if self.subject_to_thresholding else "false",
            "ga4_other_row_aggregation": "true" if self.rolled_up() else "false",
            "ga4_sampling_consistency": self.sampling_consistency,
        }
        if self.empty_reason is not None:
            provenance["ga4_empty_reason"] = self.empty_reason
        if self.restricted_metrics:
            provenance["ga4_restricted_metrics"] = ",".join(
                sorted(item.metric_name for item in self.restricted_metrics)
            )
        return provenance

    def to_normalized_report(self) -> NormalizedReport:
        """Project the aggregate onto the existing persisted report contract.

        A cell without a number stays ``None``.  It is never coerced to zero,
        so a withheld or never-returned measurement cannot be read as no
        activity.  Each cell's status and the report-level completeness facts
        are persisted alongside it, so a stored ``null`` remains attributable to
        thresholding, a metric restriction, or simple absence.
        """
        performance: list[PerformanceRow] = []
        conversions: list[ConversionBreakdownRow] = []
        provenance = self._report_provenance()
        for row in self.rows:
            values = {cell.metric: cell.value for cell in row.cells}
            statuses = {cell.metric: cell.status for cell in row.cells}
            extensions: dict[str, str | int | bool | None] = {}
            if row.session_campaign_name is not None:
                extensions["ga4_sessionCampaignName"] = row.session_campaign_name
            if row.session_source is not None:
                extensions["ga4_sessionSource"] = row.session_source
            if row.session_medium is not None:
                extensions["ga4_sessionMedium"] = row.session_medium
            if row.event_name is not None:
                extensions["ga4_eventName"] = row.event_name
            if values.get("totalUsers") is not None:
                extensions["ga4_totalUsers"] = str(values["totalUsers"])
            for metric, status in statuses.items():
                extensions[f"ga4_cell_status_{metric}"] = status
            if row.dimension_statuses:
                extensions["ga4_dimension_statuses"] = ",".join(row.dimension_statuses)
            if row.join_source is not None:
                extensions["ga4_join_source"] = row.join_source
            if row.join_map_digest is not None:
                extensions["ga4_join_map_digest"] = row.join_map_digest
            extensions.update(provenance)
            performance.append(
                PerformanceRow(
                    report_id=row.report_id,
                    # GA4 traffic-source values are not provider campaign
                    # identities.  A campaign reference appears only when the
                    # customer's declared join map (M00.2) resolved this exact
                    # dimension tuple to a retained provider campaign.
                    resource_level="ACCOUNT",
                    metric_grain="ACCOUNT",
                    resource_type="GA4_PROPERTY",
                    resource_ref=self.envelope.source_account_ref,
                    campaign_ref=row.provider_campaign_ref,
                    campaign_join_key=row.campaign_join_key,
                    dimension_type="GA4_" + row.aggregate_shape,
                    dimension_value=digest(
                        {
                            "campaign": row.session_campaign_name,
                            "source": row.session_source,
                            "medium": row.session_medium,
                            "event": row.event_name,
                        }
                    ),
                    sessions=values.get("sessions"),
                    key_events=values.get("keyEvents"),
                    provider_reported_results=values.get("keyEvents"),
                    join_status=row.join_status,
                    data_quality_statuses=row.data_quality_statuses,
                    provider_extensions=extensions,
                )
            )
            # Only the property-wide key-event totals produce an unambiguous
            # conversion breakdown today.  A campaign-scoped key-event row
            # cannot be identified without the deterministic provider join that
            # ticket M00.2 owns, and emitting one per campaign under the same
            # account reference would create indistinguishable duplicates.
            if row.aggregate_shape == "KEY_EVENT_TOTALS":
                rolled_up = "EVENT_OTHER_ROW" in row.dimension_statuses
                conversion_extensions: dict[str, str | int | bool | None] = {
                    "ga4_cell_status_keyEvents": statuses["keyEvents"],
                    "ga4_completeness": self.completeness,
                }
                if row.join_source is not None:
                    conversion_extensions["ga4_join_source"] = row.join_source
                if row.join_map_digest is not None:
                    conversion_extensions["ga4_join_map_digest"] = row.join_map_digest
                if row.dimension_statuses:
                    conversion_extensions["ga4_dimension_statuses"] = ",".join(
                        row.dimension_statuses
                    )
                conversions.append(
                    ConversionBreakdownRow(
                        report_id=row.report_id,
                        resource_ref=self.envelope.source_account_ref,
                        campaign_ref=row.provider_campaign_ref,
                        campaign_join_key=row.campaign_join_key,
                        conversion_source="GA4",
                        conversion_action_ref=row.conversion_action_ref,
                        # The "(other)" bucket is an aggregation of key events
                        # GA4 declined to name; it is not one key event.
                        conversion_kind="KEY_EVENT_OTHER_ROW" if rolled_up else "KEY_EVENT",
                        counting_method="GA4_CONFIGURED",
                        attribution_model=self.envelope.attribution_model,
                        conversion_count=values.get("keyEvents"),
                        join_status=row.join_status,
                        data_quality_statuses=row.data_quality_statuses,
                        provider_extensions=conversion_extensions,
                    )
                )
        return NormalizedReport(
            envelope=self.envelope,
            performance_rows=performance,
            conversion_rows=conversions,
        )
