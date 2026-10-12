# ruff: noqa: E501
"""Deterministic GA4 <-> provider campaign joins from a customer-declared contract.

Ticket M00.2.  This module never guesses a join key.  A provider campaign is
joined to GA4 traffic-source values only because the customer declared that
mapping, or explicitly accepted a previously proposed one, in the closed
contract below.  A deterministic heuristic tier may *propose* a candidate from
exact case-sensitive campaign-name equality, but a proposal is never applied to
reporting: it is reported as ``HEURISTIC`` and stays out of authoritative
totals, exactly as ``docs/reporting-schema.md`` requires.

The module has no provider, credential, network, or mutation surface.  It reads
already-validated normalized values and produces one content-addressed,
predecessor-linked join map.

Contract and limits: ``docs/implementation/ga4-campaign-joins.md``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.models import AD_PROVIDERS, Provider, SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.reporting import (
    GA4_JOIN_APPLIED_DISCLOSURE,
    Ga4AggregateReport,
    Ga4AggregateRow,
    JoinSource,
    JoinStatus,
    NormalizedReport,
    QualityStatus,
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

# GA4 populates these documented sentinels when it has no value or rolled the
# value into its high-cardinality bucket.  A sentinel is not an identity, so it
# can never be a declared join key or a heuristic candidate.
GA4_SENTINEL_VALUES: Final = frozenset({"(not set)", "(other)", "(direct)", "(none)"})

# DoD section 6 line 249: provider, GA4, and CRM results stay side by side and
# no output presents correlation or attributed credit as causal incrementality.
SIDE_BY_SIDE_DISCLOSURE: Final = "SOURCES_SIDE_BY_SIDE_NO_CAUSAL_OR_INCREMENTALITY_CLAIM"

# No provider connector in this product retains an advertising campaign *name*
# in local history; only opaque campaign references are retained.  The
# heuristic tier therefore compares the GA4 dimension value with a name the
# customer supplied in the contract, and that provenance is recorded.
HEURISTIC_NAME_PROVENANCE: Final = "CUSTOMER_SUPPLIED_PROVIDER_CAMPAIGN_NAME_NOT_RETAINED_HISTORY"

_MAX_KEYS_PER_ENTRY = 16
_MAX_ENTRIES = 500
_MAX_ACCOUNTS = 12
_MAX_ROWS = 10_000
_MAX_CANDIDATES = 16

Ga4JoinMatchKind = Literal[
    "SESSION_CAMPAIGN_NAME",
    "SESSION_SOURCE_MEDIUM",
    "SESSION_CAMPAIGN_NAME_SOURCE_MEDIUM",
    # The customer declares that their controlled ``utm_campaign`` value carries
    # the advertising-platform campaign ID.  GA4 reports that value as
    # ``sessionCampaignName``.
    "UTM_CAMPAIGN_ID",
]
JoinConflictReason = Literal[
    "MULTIPLE_PROVIDER_CAMPAIGNS_DECLARE_ONE_GA4_VALUE",
    "ONE_PROVIDER_CAMPAIGN_MATCHES_SEVERAL_GA4_VALUES_WITHOUT_DECLARED_MULTI_MAPPING",
]
JoinBoundaryStatus = Literal[
    "ALIGNED",
    # The GA4 property timezone and at least one declared provider account
    # timezone differ, so their calendar date boundaries are not the same
    # instant.  The rows are never silently aligned.
    "TIMEZONE_MISMATCH",
]

# Terms that would turn a side-by-side presentation into a causal or derived
# cross-source claim.  These are refused before any output is emitted.
_PROHIBITED_OUTPUT_TERMS: Final = (
    "incremental",
    "incrementality",
    "causal",
    "causality",
    "caused",
    "lift",
    "attributed_credit",
    "credit_share",
    "roas",
    "cpa",
    "cost_per_session",
    "cost_per_key_event",
    "cost_per_conversion",
    "conversion_rate",
    "session_rate",
    "ratio",
    "per_session",
    "per_key_event",
    "true_conversions",
    "blended",
)


# The only fields allowed to name a prohibited concept are the explicit
# negations that prove the concept is absent, and they may carry only these
# exact values.
_DISCLOSURE_KEYS: Final = frozenset(
    {"causal_claim", "derived_cross_source_metrics", "disclosure", "heuristic_proposals_applied"}
)
_ALLOWED_DISCLOSURE_VALUES: Final = frozenset({SIDE_BY_SIDE_DISCLOSURE, "", "False", "false"})

# Fields whose string values this product chooses itself: closed enumerations
# and fixed labels.  Only these values are scanned for a prohibited term.
#
# Every other string in a join payload is retained source data -- a GA4
# dimension value, a customer-declared provider campaign name, an opaque
# reference, a digest, a timezone, or a provider's own attribution label.  An
# advertising campaign may legitimately be called "brand-lift-q4" or
# "q4-cpa-test"; the GA4 read boundary retains that name verbatim and nothing
# in the reporting contract forbids it.  Scanning data values would make a
# lawfully retained record permanently unreviewable, which is a different and
# worse failure than the one this guard exists to prevent.
_PRODUCT_LABEL_KEYS: Final = frozenset(
    {
        "alignment",
        "basis",
        "boundary_status",
        "conflict_reasons",
        "data_quality_statuses",
        "evidence_kind",
        "join_source",
        "join_status",
        "match_kind",
        "name_provenance",
        "provenance",
        "status",
        "window_boundary",
    }
)


def refuse_cross_source_derivation(value: Any, *, depth: int = 0, scan_values: bool = True) -> None:
    """Refuse a product-chosen field name or label that states a derived claim.

    A GA4 measurement and a provider measurement may appear beside each other.
    Dividing one by the other, or labelling the pair as causal, is prohibited
    by ``docs/definition-of-done.md`` section 6, so it fails closed here rather
    than being reviewed downstream.

    The scan covers what the *product* writes: every field name, and the string
    values of the closed label fields in ``_PRODUCT_LABEL_KEYS``.  Retained
    source data -- GA4 dimension values, customer-declared campaign names,
    references and digests -- is never censored.  The explicit negation fields
    are the only place a prohibited word may appear, and only with its declared
    value.
    """
    if depth > 32:
        raise AdhelmError(
            "INPUT_TOO_DEEP", "Join output nesting exceeds the allowed depth.", exit_code=2
        )
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            if name in _DISCLOSURE_KEYS:
                if child is not False and str(child) not in _ALLOWED_DISCLOSURE_VALUES:
                    raise AdhelmError(
                        "CROSS_SOURCE_DERIVATION_PROHIBITED",
                        "A disclosure field may only state that the claim is absent.",
                        exit_code=2,
                    )
                continue
            _refuse_term(name)
            refuse_cross_source_derivation(
                child, depth=depth + 1, scan_values=name in _PRODUCT_LABEL_KEYS
            )
    elif isinstance(value, (list, tuple)):
        for child in value:
            refuse_cross_source_derivation(child, depth=depth + 1, scan_values=scan_values)
    elif isinstance(value, str) and scan_values:
        _refuse_term(value)


def _refuse_term(value: str) -> None:
    if value in _ALLOWED_DISCLOSURE_VALUES:
        return
    lowered = value.lower()
    for term in _PROHIBITED_OUTPUT_TERMS:
        if term in lowered:
            raise AdhelmError(
                "CROSS_SOURCE_DERIVATION_PROHIBITED",
                "Join output may not present a derived cross-source or causal measure.",
                exit_code=2,
                next_action=("Present provider and GA4 measurements as separate labelled fields."),
            )


def _timezone(value: str, context: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{context} must be an IANA timezone") from exc
    return value


class Ga4JoinDimensionKey(StrictModel):
    """One exact GA4 dimension value set a customer declared for a campaign."""

    schema_version: Literal[1] = 1
    match_kind: Ga4JoinMatchKind
    session_campaign_name: str | None = Field(default=None, min_length=1, max_length=160)
    session_source: str | None = Field(default=None, min_length=1, max_length=160)
    session_medium: str | None = Field(default=None, min_length=1, max_length=160)
    utm_campaign_id: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def exactly_the_declared_dimensions(self) -> Ga4JoinDimensionKey:
        by_kind: dict[Ga4JoinMatchKind, tuple[str, ...]] = {
            "SESSION_CAMPAIGN_NAME": ("session_campaign_name",),
            "SESSION_SOURCE_MEDIUM": ("session_source", "session_medium"),
            "SESSION_CAMPAIGN_NAME_SOURCE_MEDIUM": (
                "session_campaign_name",
                "session_source",
                "session_medium",
            ),
            "UTM_CAMPAIGN_ID": ("utm_campaign_id",),
        }
        required = by_kind[self.match_kind]
        for name in (
            "session_campaign_name",
            "session_source",
            "session_medium",
            "utm_campaign_id",
        ):
            value = getattr(self, name)
            if (name in required) != (value is not None):
                raise ValueError("A GA4 join key carries exactly its declared dimension values")
            if value is not None and value in GA4_SENTINEL_VALUES:
                raise ValueError("A GA4 sentinel value is not a declarable campaign identity")
        return self

    def order(self) -> tuple[str, str, str, str]:
        return (
            self.match_kind,
            self.session_campaign_name or "",
            self.session_source or "",
            self.session_medium or "",
        )

    def matches(self, row: Ga4JoinRow) -> bool:
        """Exact, case-sensitive equality only.  No normalization, no fuzz."""
        if self.match_kind == "SESSION_CAMPAIGN_NAME":
            return row.session_campaign_name == self.session_campaign_name
        if self.match_kind == "SESSION_SOURCE_MEDIUM":
            return (
                row.session_source == self.session_source
                and row.session_medium == self.session_medium
            )
        if self.match_kind == "SESSION_CAMPAIGN_NAME_SOURCE_MEDIUM":
            return (
                row.session_campaign_name == self.session_campaign_name
                and row.session_source == self.session_source
                and row.session_medium == self.session_medium
            )
        return row.session_campaign_name == self.utm_campaign_id


class DeclaredCampaignJoin(StrictModel):
    """One customer-declared provider campaign and the GA4 values it owns."""

    schema_version: Literal[1] = 1
    provider: Provider
    provider_campaign_ref: SafeRef
    # Optional, customer-supplied, and used only by the heuristic proposal tier.
    # No provider connector retains a campaign name in local history.
    provider_campaign_name: str | None = Field(default=None, min_length=1, max_length=160)
    ga4_keys: tuple[Ga4JoinDimensionKey, ...] = Field(
        default_factory=tuple, max_length=_MAX_KEYS_PER_ENTRY
    )
    # A provider campaign legitimately owning several GA4 dimension values must
    # say so.  Otherwise several matches are a CONFLICT, never a silent merge.
    multi_mapping_declared: bool = False
    join_source: JoinSource = "DECLARED"

    @field_validator("provider")
    @classmethod
    def advertising_provider_only(cls, value: str) -> str:
        if value not in AD_PROVIDERS:
            raise ValueError("A join entry declares one advertising provider campaign")
        return value

    @model_validator(mode="after")
    def declared_keys_are_unique(self) -> DeclaredCampaignJoin:
        orders = [key.order() for key in self.ga4_keys]
        if len(orders) != len(set(orders)):
            raise ValueError("A join entry cannot repeat one GA4 dimension key")
        if self.join_source == "ACCEPTED_HEURISTIC" and not self.ga4_keys:
            raise ValueError("An accepted heuristic join must declare the exact GA4 values")
        if self.provider_campaign_name in GA4_SENTINEL_VALUES:
            raise ValueError("A GA4 sentinel value is not a provider campaign name")
        return self


class Ga4PropertyBinding(StrictModel):
    """The GA4 property a join map is bound to."""

    schema_version: Literal[1] = 1
    property_ref: SafeRef
    property_timezone: str = Field(min_length=1, max_length=80)

    @field_validator("property_timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        return _timezone(value, "GA4 property timezone")


class ProviderAccountBinding(StrictModel):
    """One advertising account a join map is bound to, with its own timezone."""

    schema_version: Literal[1] = 1
    provider: Provider
    account_ref: SafeRef
    account_timezone: str = Field(min_length=1, max_length=80)

    @field_validator("provider")
    @classmethod
    def advertising_provider_only(cls, value: str) -> str:
        if value not in AD_PROVIDERS:
            raise ValueError("A join map binds advertising provider accounts")
        return value

    @field_validator("account_timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        return _timezone(value, "provider account timezone")


class CampaignJoinContract(StrictModel):
    """The entire customer declaration used for one content digest.

    Nothing in this contract authorizes a remote read or write.  It records
    which GA4 dimension values the customer says belong to which retained
    provider campaign reference.
    """

    schema_version: Literal[1] = 1
    contract_id: SafeRef
    version: int = Field(ge=1, le=1_000_000)
    supersedes_ref: Digest | None
    ga4_property: Ga4PropertyBinding
    provider_accounts: tuple[ProviderAccountBinding, ...] = Field(
        min_length=1, max_length=_MAX_ACCOUNTS
    )
    declared_joins: tuple[DeclaredCampaignJoin, ...] = Field(
        default_factory=tuple, max_length=_MAX_ENTRIES
    )
    # Whether the deterministic name-equality tier may *propose* candidates.
    # A proposal is never applied; disabling this only suppresses the report.
    heuristic_proposals_enabled: bool = True
    effective_from: datetime

    @field_validator("effective_from")
    @classmethod
    def aware_utc_effective_from(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Join-contract effective time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def internally_consistent_declaration(self) -> CampaignJoinContract:
        if self.version == 1 and self.supersedes_ref is not None:
            raise ValueError("Version one join contracts cannot supersede another declaration")
        if self.version > 1 and self.supersedes_ref is None:
            raise ValueError("Later join-contract versions require the prior content digest")
        accounts = [(item.provider, item.account_ref) for item in self.provider_accounts]
        if len(accounts) != len(set(accounts)):
            raise ValueError("A join contract cannot bind one provider account twice")
        refs = [item.provider_campaign_ref for item in self.declared_joins]
        if len(refs) != len(set(refs)):
            raise ValueError("A join contract cannot declare one provider campaign twice")
        bound = {item.provider for item in self.provider_accounts}
        if any(item.provider not in bound for item in self.declared_joins):
            raise ValueError("Every declared campaign belongs to a bound provider account")
        validate_data(self.model_dump(mode="json"))
        return self


class Ga4JoinRow(StrictModel):
    """One GA4 traffic-source dimension tuple presented for joining."""

    schema_version: Literal[1] = 1
    session_campaign_name: str = Field(min_length=1, max_length=160)
    session_source: str = Field(min_length=1, max_length=160)
    session_medium: str = Field(min_length=1, max_length=160)

    def order(self) -> tuple[str, str, str]:
        return (self.session_campaign_name, self.session_source, self.session_medium)


class HeuristicJoinProposal(StrictModel):
    """A candidate the customer has not accepted.  It is never applied."""

    schema_version: Literal[1] = 1
    provider: Provider
    provider_campaign_ref: SafeRef
    session_campaign_name: str = Field(min_length=1, max_length=160)
    session_source: str = Field(min_length=1, max_length=160)
    session_medium: str = Field(min_length=1, max_length=160)
    basis: Literal["EXACT_CASE_SENSITIVE_CAMPAIGN_NAME_EQUALITY"] = (
        "EXACT_CASE_SENSITIVE_CAMPAIGN_NAME_EQUALITY"
    )
    name_provenance: Literal["CUSTOMER_SUPPLIED_PROVIDER_CAMPAIGN_NAME_NOT_RETAINED_HISTORY"] = (
        HEURISTIC_NAME_PROVENANCE
    )
    status: Literal["HEURISTIC_PROPOSED"] = "HEURISTIC_PROPOSED"
    applied: Literal[False] = False


class CampaignJoinResolution(StrictModel):
    """The explicit state of one GA4 dimension tuple.  No state is implicit."""

    schema_version: Literal[1] = 1
    session_campaign_name: str = Field(min_length=1, max_length=160)
    session_source: str = Field(min_length=1, max_length=160)
    session_medium: str = Field(min_length=1, max_length=160)
    join_status: JoinStatus
    join_source: JoinSource | None = None
    provider: Provider | None = None
    provider_campaign_ref: SafeRef | None = None
    campaign_join_key: SafeRef | None = None
    match_kind: Ga4JoinMatchKind | None = None
    conflict_reasons: tuple[JoinConflictReason, ...] = Field(default_factory=tuple, max_length=4)
    candidate_provider_campaign_refs: tuple[SafeRef, ...] = Field(
        default_factory=tuple, max_length=_MAX_CANDIDATES
    )
    boundary_status: JoinBoundaryStatus = "ALIGNED"
    data_quality_statuses: tuple[QualityStatus, ...] = Field(default_factory=tuple, max_length=8)

    @model_validator(mode="after")
    def every_state_is_explicit(self) -> CampaignJoinResolution:
        joined = self.join_status in {"EXPLICIT_MAPPING", "EXACT_CONTROLLED_KEY"}
        if joined != (self.provider_campaign_ref is not None):
            raise ValueError("Only a joined row identifies a provider campaign")
        if joined != (self.join_source is not None):
            raise ValueError("Only a joined row declares a join source")
        if joined != (self.provider is not None):
            raise ValueError("Only a joined row declares a provider")
        if joined != (self.match_kind is not None):
            raise ValueError("Only a joined row records the matched key kind")
        if (self.join_status == "EXACT_CONTROLLED_KEY") != (self.campaign_join_key is not None):
            raise ValueError("Only a controlled-key join carries a campaign join key")
        if (self.join_status == "CONFLICT") != bool(self.conflict_reasons):
            raise ValueError("Exactly a conflicting row records conflict reasons")
        if (self.join_status in {"CONFLICT", "HEURISTIC"}) != bool(
            self.candidate_provider_campaign_refs
        ):
            raise ValueError("Exactly a conflicting or heuristic row lists its candidates")
        if ("UNMAPPED" in self.data_quality_statuses) != (self.join_status == "UNMAPPED"):
            raise ValueError("The UNMAPPED quality status belongs to an unmapped row")
        if ("TIMEZONE_MISMATCH" in self.data_quality_statuses) != (
            self.boundary_status == "TIMEZONE_MISMATCH"
        ):
            raise ValueError("A timezone-mismatched row carries the TIMEZONE_MISMATCH status")
        if len(self.data_quality_statuses) != len(set(self.data_quality_statuses)):
            raise ValueError("A join resolution cannot repeat one quality status")
        if len(self.candidate_provider_campaign_refs) != len(
            set(self.candidate_provider_campaign_refs)
        ):
            raise ValueError("A join resolution cannot repeat one candidate campaign")
        return self


class ProviderTimezoneMismatch(StrictModel):
    """A GA4 property timezone that differs from one bound provider account."""

    schema_version: Literal[1] = 1
    provider: Provider
    account_ref: SafeRef
    ga4_property_timezone: str = Field(min_length=1, max_length=80)
    provider_account_timezone: str = Field(min_length=1, max_length=80)
    status: Literal["TIMEZONE_MISMATCH"] = "TIMEZONE_MISMATCH"
    alignment: Literal["NOT_ALIGNED_DATE_BOUNDARIES_DIFFER"] = "NOT_ALIGNED_DATE_BOUNDARIES_DIFFER"


class CampaignJoinMap(StrictModel):
    """One content-addressed, predecessor-linked join map bound to its sources.

    A restatement is visible because a later version supersedes an exact prior
    content digest instead of overwriting it.
    """

    schema_version: Literal[1] = 1
    contract: CampaignJoinContract
    content_digest: Digest
    join_map_digest: Digest
    boundary_status: JoinBoundaryStatus
    timezone_mismatches: tuple[ProviderTimezoneMismatch, ...] = Field(
        default_factory=tuple, max_length=_MAX_ACCOUNTS
    )
    resolutions: tuple[CampaignJoinResolution, ...] = Field(
        default_factory=tuple, max_length=_MAX_ROWS
    )
    proposals: tuple[HeuristicJoinProposal, ...] = Field(
        default_factory=tuple, max_length=_MAX_ROWS
    )
    # Declared campaigns whose GA4 values were absent from the presented rows.
    unmatched_declared_campaign_refs: tuple[SafeRef, ...] = Field(
        default_factory=tuple, max_length=_MAX_ENTRIES
    )
    provenance: Literal["CUSTOMER_DECLARED"] = "CUSTOMER_DECLARED"
    execution_authority: Literal[False] = False
    causal_claim: Literal[False] = False
    derived_cross_source_metrics: Literal[False] = False
    disclosure: Literal["SOURCES_SIDE_BY_SIDE_NO_CAUSAL_OR_INCREMENTALITY_CLAIM"] = (
        SIDE_BY_SIDE_DISCLOSURE
    )

    @model_validator(mode="after")
    def digests_and_ordering_are_provable(self) -> CampaignJoinMap:
        if self.content_digest != digest(self.contract):
            raise ValueError("Join-contract digest must match its exact canonical declaration")
        if self.join_map_digest != _join_map_digest(
            content_digest=self.content_digest,
            boundary_status=self.boundary_status,
            resolutions=self.resolutions,
            proposals=self.proposals,
        ):
            raise ValueError("Join-map digest must match its exact canonical resolution set")
        orders = [
            (row.session_campaign_name, row.session_source, row.session_medium)
            for row in self.resolutions
        ]
        if orders != sorted(orders):
            raise ValueError("Join resolutions must be canonically ordered")
        if len(orders) != len(set(orders)):
            raise ValueError("A join map cannot resolve one GA4 dimension tuple twice")
        proposal_orders = [
            (
                row.session_campaign_name,
                row.session_source,
                row.session_medium,
                row.provider,
                row.provider_campaign_ref,
            )
            for row in self.proposals
        ]
        if proposal_orders != sorted(proposal_orders):
            raise ValueError("Heuristic proposals must be canonically ordered")
        mismatched = self.boundary_status == "TIMEZONE_MISMATCH"
        if mismatched != bool(self.timezone_mismatches):
            raise ValueError("A mismatched boundary status must name its provider accounts")
        if any(row.boundary_status != self.boundary_status for row in self.resolutions):
            raise ValueError("Every resolution carries the map's boundary status")
        declared = {item.provider_campaign_ref for item in self.contract.declared_joins}
        if any(ref not in declared for ref in self.unmatched_declared_campaign_refs):
            raise ValueError("An unmatched campaign must be declared by this contract")
        if list(self.unmatched_declared_campaign_refs) != sorted(
            self.unmatched_declared_campaign_refs
        ):
            raise ValueError("Unmatched declared campaigns must be canonically ordered")
        return self

    def joined_resolutions(self) -> tuple[CampaignJoinResolution, ...]:
        """Only the rows a customer declaration actually joined."""
        return tuple(
            row
            for row in self.resolutions
            if row.join_status in {"EXPLICIT_MAPPING", "EXACT_CONTROLLED_KEY"}
        )

    def status_counts(self) -> dict[str, int]:
        counts = {
            "EXPLICIT_MAPPING": 0,
            "EXACT_CONTROLLED_KEY": 0,
            "HEURISTIC": 0,
            "CONFLICT": 0,
            "UNMAPPED": 0,
        }
        for row in self.resolutions:
            counts[row.join_status] = counts.get(row.join_status, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        """Bounded, agent-visible facts carrying no derived cross-source measure."""
        data: dict[str, Any] = {
            "join_map_digest": self.join_map_digest,
            "content_digest": self.content_digest,
            "contract_ref": self.contract.contract_id,
            "version": self.contract.version,
            "supersedes_digest": self.contract.supersedes_ref,
            "ga4_property_ref": self.contract.ga4_property.property_ref,
            "ga4_property_timezone": self.contract.ga4_property.property_timezone,
            "provider_account_refs": sorted(
                item.account_ref for item in self.contract.provider_accounts
            ),
            "boundary_status": self.boundary_status,
            "resolved_row_count": len(self.resolutions),
            "status_counts": self.status_counts(),
            "heuristic_proposal_count": len(self.proposals),
            "heuristic_proposals_applied": False,
            "unmatched_declared_campaign_count": len(self.unmatched_declared_campaign_refs),
            "provenance": self.provenance,
            "execution_authority": False,
            "causal_claim": False,
            "derived_cross_source_metrics": False,
            "disclosure": self.disclosure,
        }
        refuse_cross_source_derivation(data)
        return data


def _join_map_digest(
    *,
    content_digest: str,
    boundary_status: str,
    resolutions: Sequence[CampaignJoinResolution],
    proposals: Sequence[HeuristicJoinProposal],
) -> str:
    return digest(
        {
            "content_digest": content_digest,
            "boundary_status": boundary_status,
            "resolutions": [row.model_dump(mode="json") for row in resolutions],
            "proposals": [row.model_dump(mode="json") for row in proposals],
        }
    )


def ga4_join_rows_from_report(report: Ga4AggregateReport) -> tuple[Ga4JoinRow, ...]:
    """Project the traffic-source rows of one GA4 aggregate onto join inputs.

    Key-event totals carry no session dimensions and are not joinable.  Rows
    whose campaign, source, or medium is a GA4 sentinel are excluded from
    joining but are not lost: they stay `UNMAPPED` on the report itself.
    """
    rows: list[Ga4JoinRow] = []
    for row in report.rows:
        candidate = _join_row(row.session_campaign_name, row.session_source, row.session_medium)
        if candidate is not None:
            rows.append(candidate)
    return _ordered_rows(rows)


def ga4_join_rows_from_normalized(report: NormalizedReport) -> tuple[Ga4JoinRow, ...]:
    """Project a retained GA4 normalized report onto join inputs.

    M00.1 persists the exact returned dimension values, so a retained report is
    joinable without re-reading GA4.
    """
    if report.envelope.provider != "ga4":
        raise AdhelmError(
            "JOIN_SOURCE_INVALID",
            "Only a retained GA4 report supplies GA4 join dimensions.",
            exit_code=2,
        )
    rows: list[Ga4JoinRow] = []
    for row in report.performance_rows:
        extensions = row.provider_extensions
        candidate = _join_row(
            extensions.get("ga4_sessionCampaignName"),
            extensions.get("ga4_sessionSource"),
            extensions.get("ga4_sessionMedium"),
        )
        if candidate is not None:
            rows.append(candidate)
    return _ordered_rows(rows)


def _join_row(campaign: object, source: object, medium: object) -> Ga4JoinRow | None:
    """Return the joinable tuple, or ``None`` when the row cannot be joined.

    A GA4 sentinel -- ``(not set)``, ``(other)``, ``(direct)``, ``(none)`` -- is
    an absence or aggregation marker, not a campaign identity.  It can never be
    a declared key or a heuristic candidate, so it is not presented for joining
    at all and the row keeps its ``UNMAPPED`` state on the report itself.
    """
    if not (isinstance(campaign, str) and isinstance(source, str) and isinstance(medium, str)):
        return None
    if not campaign or not source or not medium:
        return None
    if {campaign, source, medium} & GA4_SENTINEL_VALUES:
        return None
    return Ga4JoinRow(session_campaign_name=campaign, session_source=source, session_medium=medium)


def _ordered_rows(rows: Iterable[Ga4JoinRow]) -> tuple[Ga4JoinRow, ...]:
    unique = {row.order(): row for row in rows}
    return tuple(unique[key] for key in sorted(unique))


def resolve_campaign_joins(
    contract: CampaignJoinContract, ga4_rows: Sequence[Ga4JoinRow]
) -> CampaignJoinMap:
    """Resolve every presented GA4 row to one explicit, deterministic state.

    Nothing is joined that the contract did not declare.  Every other outcome
    is `HEURISTIC`, `CONFLICT`, or `UNMAPPED`, and each is visible.
    """
    if len(ga4_rows) > _MAX_ROWS:
        raise AdhelmError(
            "INPUT_TOO_LARGE", "Join input exceeds the normalized row bound.", exit_code=2
        )
    rows = _ordered_rows(ga4_rows)
    validate_data([row.model_dump(mode="json") for row in rows])

    mismatches = tuple(
        ProviderTimezoneMismatch(
            provider=account.provider,
            account_ref=account.account_ref,
            ga4_property_timezone=contract.ga4_property.property_timezone,
            provider_account_timezone=account.account_timezone,
        )
        for account in sorted(contract.provider_accounts, key=lambda item: item.account_ref)
        if account.account_timezone != contract.ga4_property.property_timezone
    )
    boundary: JoinBoundaryStatus = "TIMEZONE_MISMATCH" if mismatches else "ALIGNED"

    # Direction one: every declared key matched against every presented row.
    matches: dict[tuple[str, str, str], list[tuple[DeclaredCampaignJoin, Ga4JoinDimensionKey]]] = {
        row.order(): [] for row in rows
    }
    matched_rows_by_entry: dict[str, list[tuple[str, str, str]]] = {}
    for entry in sorted(contract.declared_joins, key=lambda item: item.provider_campaign_ref):
        for key in sorted(entry.ga4_keys, key=lambda item: item.order()):
            for row in rows:
                if not key.matches(row):
                    continue
                matches[row.order()].append((entry, key))
                matched_rows_by_entry.setdefault(entry.provider_campaign_ref, []).append(
                    row.order()
                )

    # Direction two: an entry that matched several distinct GA4 tuples without
    # declaring a multi-mapping is a conflict, not an implicit fan-out.
    fanned_out = {
        entry.provider_campaign_ref
        for entry in contract.declared_joins
        if not entry.multi_mapping_declared
        and len(set(matched_rows_by_entry.get(entry.provider_campaign_ref, ()))) > 1
    }

    proposals = _proposals(contract, rows, matches)
    proposals_by_row: dict[tuple[str, str, str], list[HeuristicJoinProposal]] = {}
    for proposal in proposals:
        proposals_by_row.setdefault(
            (
                proposal.session_campaign_name,
                proposal.session_source,
                proposal.session_medium,
            ),
            [],
        ).append(proposal)

    resolutions = tuple(
        _resolution(
            row,
            matches[row.order()],
            proposals_by_row.get(row.order(), []),
            fanned_out=fanned_out,
            boundary=boundary,
        )
        for row in rows
    )
    unmatched = tuple(
        sorted(
            entry.provider_campaign_ref
            for entry in contract.declared_joins
            if entry.ga4_keys and not matched_rows_by_entry.get(entry.provider_campaign_ref)
        )
    )
    content_digest = digest(contract)
    return CampaignJoinMap(
        contract=contract,
        content_digest=content_digest,
        join_map_digest=_join_map_digest(
            content_digest=content_digest,
            boundary_status=boundary,
            resolutions=resolutions,
            proposals=proposals,
        ),
        boundary_status=boundary,
        timezone_mismatches=mismatches,
        resolutions=resolutions,
        proposals=proposals,
        unmatched_declared_campaign_refs=unmatched,
    )


def _proposals(
    contract: CampaignJoinContract,
    rows: Sequence[Ga4JoinRow],
    matches: Mapping[tuple[str, str, str], Sequence[tuple[DeclaredCampaignJoin, Any]]],
) -> tuple[HeuristicJoinProposal, ...]:
    """Propose only exact case-sensitive campaign-name equality, never applied."""
    if not contract.heuristic_proposals_enabled:
        return ()
    proposals: list[HeuristicJoinProposal] = []
    for row in rows:
        if matches[row.order()]:
            # A declared mapping already owns this row.  A weaker candidate
            # never competes with an explicit customer declaration.
            continue
        if row.session_campaign_name in GA4_SENTINEL_VALUES:
            continue
        for entry in contract.declared_joins:
            if (
                entry.provider_campaign_name is not None
                and entry.provider_campaign_name == row.session_campaign_name
            ):
                proposals.append(
                    HeuristicJoinProposal(
                        provider=entry.provider,
                        provider_campaign_ref=entry.provider_campaign_ref,
                        session_campaign_name=row.session_campaign_name,
                        session_source=row.session_source,
                        session_medium=row.session_medium,
                    )
                )
    proposals.sort(
        key=lambda item: (
            item.session_campaign_name,
            item.session_source,
            item.session_medium,
            item.provider,
            item.provider_campaign_ref,
        )
    )
    return tuple(proposals)


def _resolution(
    row: Ga4JoinRow,
    declared: Sequence[tuple[DeclaredCampaignJoin, Ga4JoinDimensionKey]],
    proposals: Sequence[HeuristicJoinProposal],
    *,
    fanned_out: set[str],
    boundary: JoinBoundaryStatus,
) -> CampaignJoinResolution:
    quality: list[QualityStatus] = []
    if boundary == "TIMEZONE_MISMATCH":
        quality.append("TIMEZONE_MISMATCH")
    candidates = sorted({entry.provider_campaign_ref for entry, _ in declared})
    reasons: list[JoinConflictReason] = []
    if len(candidates) > 1:
        reasons.append("MULTIPLE_PROVIDER_CAMPAIGNS_DECLARE_ONE_GA4_VALUE")
    if any(ref in fanned_out for ref in candidates):
        reasons.append(
            "ONE_PROVIDER_CAMPAIGN_MATCHES_SEVERAL_GA4_VALUES_WITHOUT_DECLARED_MULTI_MAPPING"
        )
    if reasons:
        return CampaignJoinResolution(
            session_campaign_name=row.session_campaign_name,
            session_source=row.session_source,
            session_medium=row.session_medium,
            join_status="CONFLICT",
            conflict_reasons=tuple(reasons),
            candidate_provider_campaign_refs=tuple(candidates),
            boundary_status=boundary,
            data_quality_statuses=tuple(sorted(quality)),
        )
    if declared:
        entry, key = declared[0]
        controlled = key.match_kind == "UTM_CAMPAIGN_ID"
        status: JoinStatus = "EXACT_CONTROLLED_KEY" if controlled else "EXPLICIT_MAPPING"
        return CampaignJoinResolution(
            session_campaign_name=row.session_campaign_name,
            session_source=row.session_source,
            session_medium=row.session_medium,
            join_status=status,
            join_source=entry.join_source,
            provider=entry.provider,
            provider_campaign_ref=entry.provider_campaign_ref,
            campaign_join_key=key.utm_campaign_id if controlled else None,
            match_kind=key.match_kind,
            boundary_status=boundary,
            data_quality_statuses=tuple(sorted(quality)),
        )
    if proposals:
        # Visible, excluded from every joined total, and never applied.
        return CampaignJoinResolution(
            session_campaign_name=row.session_campaign_name,
            session_source=row.session_source,
            session_medium=row.session_medium,
            join_status="HEURISTIC",
            candidate_provider_campaign_refs=tuple(
                sorted({item.provider_campaign_ref for item in proposals})
            ),
            boundary_status=boundary,
            data_quality_statuses=tuple(sorted(quality)),
        )
    return CampaignJoinResolution(
        session_campaign_name=row.session_campaign_name,
        session_source=row.session_source,
        session_medium=row.session_medium,
        join_status="UNMAPPED",
        boundary_status=boundary,
        data_quality_statuses=tuple(sorted({*quality, "UNMAPPED"})),
    )


def _chain_invalid(requirement: str, next_action: str) -> AdhelmError:
    """One coded refusal per broken join-contract chain invariant.

    The rendered `Diagnostic` already carries `JOIN_MAP_CHAIN_INVALID`, so the
    message states only which invariant failed.
    """
    return AdhelmError("JOIN_MAP_CHAIN_INVALID", requirement, exit_code=2, next_action=next_action)


def validate_predecessor(previous: CampaignJoinMap, contract: CampaignJoinContract) -> None:
    """Refuse a restatement that does not link the exact prior declaration."""
    catalog_action = "Read measurement join catalog and supersede the retained digest."
    if contract.contract_id != previous.contract.contract_id:
        raise _chain_invalid(
            "A superseding join contract must keep its contract reference.", catalog_action
        )
    if contract.version != previous.contract.version + 1:
        raise _chain_invalid(
            "A superseding join contract must increment the retained version by one.",
            catalog_action,
        )
    if contract.supersedes_ref != previous.content_digest:
        raise _chain_invalid(
            "A superseding join contract must name the exact prior content digest.",
            catalog_action,
        )
    # A restatement revises which GA4 values belong to which campaign.  It is
    # not a way to move a retained, content-addressed chain onto a different
    # GA4 property or a different advertising account, which would silently
    # re-target every earlier version's declaration.
    if contract.ga4_property != previous.contract.ga4_property:
        raise _chain_invalid(
            "A superseding join contract must keep its GA4 property reference and timezone.",
            "Declare a new contract reference to bind a different GA4 property.",
        )
    if set(contract.provider_accounts) != set(previous.contract.provider_accounts):
        raise _chain_invalid(
            "A superseding join contract must keep its bound advertising accounts.",
            "Declare a new contract reference to bind a different advertising account.",
        )


def apply_join_map(report: Ga4AggregateReport, join_map: CampaignJoinMap) -> Ga4AggregateReport:
    """Attach declared join results to one GA4 aggregate report's rows.

    Only a declared or explicitly accepted mapping changes a row's join state.
    A proposal, a conflict, and an unmatched value stay visible and excluded.
    """
    if report.envelope.source_account_ref != join_map.contract.ga4_property.property_ref:
        raise AdhelmError(
            "JOIN_MAP_PROPERTY_MISMATCH",
            "The join map is bound to a different GA4 property.",
            exit_code=2,
        )
    if report.property_timezone != join_map.contract.ga4_property.property_timezone:
        raise AdhelmError(
            "JOIN_MAP_PROPERTY_MISMATCH",
            "The join map records a different GA4 property timezone.",
            exit_code=2,
        )
    resolved = report.join_map_digest()
    if resolved is not None and resolved != join_map.join_map_digest:
        # Two declarations cannot both own one report's rows.  Re-read or
        # re-project the aggregate against the single map that should apply.
        raise AdhelmError(
            "JOIN_MAP_ALREADY_APPLIED",
            "This GA4 report was already resolved by a different declared join map.",
            exit_code=2,
            next_action=(
                "Project the unresolved GA4 aggregate against exactly one retained join map."
            ),
        )
    by_row: dict[tuple[str | None, str | None, str | None], CampaignJoinResolution] = {
        (row.session_campaign_name, row.session_source, row.session_medium): row
        for row in join_map.resolutions
    }
    joined: list[Ga4AggregateRow] = []
    for row in report.rows:
        resolution = by_row.get((row.session_campaign_name, row.session_source, row.session_medium))
        if resolution is None:
            joined.append(row)
            continue
        statuses = {
            status for status in row.data_quality_statuses if status not in {"UNMAPPED", "COMPLETE"}
        } | set(resolution.data_quality_statuses)
        if not statuses:
            # A row with no remaining caveat is explicitly complete rather than
            # silently statusless.
            statuses = {"COMPLETE"}
        applied = resolution.join_status in {"EXPLICIT_MAPPING", "EXACT_CONTROLLED_KEY"}
        joined.append(
            row.model_copy(
                update={
                    "join_status": resolution.join_status,
                    "join_source": resolution.join_source,
                    "join_map_digest": join_map.join_map_digest,
                    "provider_campaign_ref": resolution.provider_campaign_ref,
                    "campaign_join_key": resolution.campaign_join_key,
                    "join_disclosure": GA4_JOIN_APPLIED_DISCLOSURE,
                    "data_quality_statuses": sorted(statuses),
                }
            )
        )
        if not applied:
            # Defensive: a non-joined row must never carry a campaign identity.
            assert joined[-1].provider_campaign_ref is None
    # Re-validate through the model rather than mutating in place.
    return Ga4AggregateReport.model_validate_json(
        canonical_bytes(
            {
                **report.model_dump(mode="json"),
                "rows": [row.model_dump(mode="json") for row in joined],
            }
        )
    )


__all__ = [
    "GA4_SENTINEL_VALUES",
    "HEURISTIC_NAME_PROVENANCE",
    "SIDE_BY_SIDE_DISCLOSURE",
    "CampaignJoinContract",
    "CampaignJoinMap",
    "CampaignJoinResolution",
    "DeclaredCampaignJoin",
    "Ga4JoinDimensionKey",
    "Ga4JoinRow",
    "Ga4PropertyBinding",
    "HeuristicJoinProposal",
    "JoinBoundaryStatus",
    "JoinConflictReason",
    "ProviderAccountBinding",
    "ProviderTimezoneMismatch",
    "apply_join_map",
    "ga4_join_rows_from_normalized",
    "ga4_join_rows_from_report",
    "refuse_cross_source_derivation",
    "resolve_campaign_joins",
    "validate_predecessor",
]
