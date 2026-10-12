# ruff: noqa: E501
"""Narrow GA4 Data API and readonly Admin identity adapter.

This module never persists transport responses.  It accepts only allowlisted
aggregate report shapes and returns normalized reporting models suitable for a
caller to append to the local ledger after review.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, field_validator, model_validator

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.models import Currency, SafeRef, StrictModel
from adhelm.reporting import (
    GA4_ATTRIBUTION_DISCLOSURE,
    ConversionBreakdownRow,
    Ga4AggregateMetric,
    Ga4AggregateReport,
    Ga4AggregateRow,
    Ga4AggregateShape,
    Ga4CellStatus,
    Ga4Completeness,
    Ga4DimensionStatus,
    Ga4MetricCell,
    Ga4MetricRestriction,
    Ga4SamplingConsistency,
    Ga4SamplingWindow,
    NormalizedReport,
    PerformanceRow,
    QualityStatus,
    ReportEnvelope,
)
from adhelm.transport import SafeHttpClient

if TYPE_CHECKING:  # pragma: no cover - typing only
    from adhelm.campaign_joins import CampaignJoinMap

_DATA_HOST = "analyticsdata.googleapis.com"
_ADMIN_HOST = "analyticsadmin.googleapis.com"
_STANDARD_DIMENSIONS = frozenset({"date", "sessionCampaignId", "sessionSource", "sessionMedium"})
_STANDARD_METRICS = frozenset({"sessions", "activeUsers", "eventCount", "keyEvents"})
_REALTIME_DIMENSIONS = frozenset({"eventName"})
_REALTIME_METRICS = frozenset({"activeUsers", "eventCount", "keyEvents"})
_IDENTITY_PROPERTY_FIELDS = "name,timeZone,currencyCode"
_IDENTITY_STREAM_LIST_FIELDS = "dataStreams(name,type),nextPageToken"
_IDENTITY_PAGE_SIZE = 200
_MAX_IDENTITY_PAGES = 5
_MAX_IDENTITY_STREAMS = _IDENTITY_PAGE_SIZE * _MAX_IDENTITY_PAGES
_IDENTITY_STREAM_TYPES = frozenset(
    {"WEB_DATA_STREAM", "ANDROID_APP_DATA_STREAM", "IOS_APP_DATA_STREAM"}
)

# Fixed, reviewed aggregate request shapes.  Callers select a shape; they never
# supply dimension or metric names.  Every dimension and metric below is a
# documented GA4 Data API core-reporting field.  There is no documented
# `keyEvents:<key event name>` metric: the only documented per-key-event
# parameterized metrics are the `sessionKeyEventRate:` and `userKeyEventRate:`
# rates, which are fractions rather than counts.  Per-key-event counts
# therefore come from the `eventName` dimension restricted to the property's
# configured key events.  See docs/research/ga4-data-aggregate-contract.md.
_AGGREGATE_REQUESTS: dict[
    Ga4AggregateShape, tuple[tuple[str, ...], tuple[Ga4AggregateMetric, ...]]
] = {
    "CAMPAIGN_SOURCE_MEDIUM": (
        ("sessionCampaignName", "sessionSource", "sessionMedium"),
        ("sessions", "totalUsers", "keyEvents"),
    ),
    "CAMPAIGN_SOURCE_MEDIUM_KEY_EVENT": (
        ("sessionCampaignName", "sessionSource", "sessionMedium", "eventName"),
        ("keyEvents",),
    ),
    "KEY_EVENT_TOTALS": (("eventName",), ("keyEvents",)),
}
_KEY_EVENT_SHAPES = frozenset({"CAMPAIGN_SOURCE_MEDIUM_KEY_EVENT", "KEY_EVENT_TOTALS"})
_MAX_AGGREGATE_PAGES = 50
_MAX_AGGREGATE_ROWS = 10_000
_NOT_SET = "(not set)"
_OTHER_ROW = "(other)"

TokenGetter = Callable[[], str]
ReferenceFor = Callable[[Literal["property", "stream", "campaign", "event"], str], str]


def _default_reference_for(
    kind: Literal["property", "stream", "campaign", "event"], value: str
) -> str:
    # Provider IDs may be retained as local operational references. Export code
    # is responsible for converting them to keyed opaque references.
    return f"ga4.{kind}.{value}"


def _property_id(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9]{1,20}", value) is None:
        raise AdhelmError("INVALID_GA4_PROPERTY", "GA4 property identity is invalid.", exit_code=2)
    return value


def _safe_token(getter: TokenGetter) -> str:
    token = getter()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(not 33 <= ord(c) <= 126 for c in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GA4 authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local GA4 profile.",
        )
    return token


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        value = parse_json(response.content, maximum_bytes=8_388_608)
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GA4 {context} response is not JSON."
        ) from exc
    if not isinstance(value, dict):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GA4 {context} response has an invalid shape."
        )
    from adhelm.privacy import validate_data

    try:
        validate_data(value)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "GA4 response contains prohibited data."
        ) from exc
    return value


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GA4 {context} included an unsupported field."
        )


def _bounded_string(value: Any, context: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 {context} is invalid.")
    # Avoid importing response text into a model before the central scan runs.
    from adhelm.privacy import validate_data

    try:
        validate_data(value)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "GA4 response contains prohibited data."
        ) from exc
    return value


def _nonnegative(value: Any, context: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GA4 {context} is not a nonnegative integer."
        )
    parsed = int(value)
    if parsed > 9_007_199_254_740_991:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 {context} exceeds the numeric bound.")
    return parsed


def _timezone(value: str, context: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GA4 {context} is not an IANA timezone."
        ) from exc


def _optional_string(value: Any, context: str, *, maximum: int = 160) -> None:
    if value is not None:
        _bounded_string(value, context, maximum=maximum)


def _validate_property_response(value: dict[str, Any]) -> None:
    _keys(
        value,
        {
            "name",
            "parent",
            "createTime",
            "updateTime",
            "displayName",
            "industryCategory",
            "timeZone",
            "currencyCode",
            "serviceLevel",
            "deleteTime",
            "expireTime",
            "propertyType",
        },
        "property",
    )
    if not isinstance(value.get("name"), str):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 property name is invalid.")
    for name in (
        "parent",
        "createTime",
        "updateTime",
        "displayName",
        "industryCategory",
        "serviceLevel",
        "deleteTime",
        "expireTime",
        "propertyType",
    ):
        _optional_string(value.get(name), f"property {name}", maximum=256)
    _optional_string(value.get("currencyCode"), "property currency", maximum=3)


def _validate_stream(value: dict[str, Any]) -> None:
    _keys(
        value,
        {
            "name",
            "type",
            "displayName",
            "createTime",
            "updateTime",
            "webStreamData",
            "androidAppStreamData",
            "iosAppStreamData",
        },
        "data stream",
    )
    for name in ("name", "type"):
        _bounded_string(value.get(name), f"data stream {name}", maximum=256)
    for name in ("displayName", "createTime", "updateTime"):
        _optional_string(value.get(name), f"data stream {name}", maximum=256)
    nested_schemas = {
        "webStreamData": {"measurementId", "firebaseAppId", "defaultUri"},
        "androidAppStreamData": {"firebaseAppId", "packageName"},
        "iosAppStreamData": {"firebaseAppId", "bundleId"},
    }
    for field, allowed in nested_schemas.items():
        nested = value.get(field)
        if nested is None:
            continue
        if not isinstance(nested, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 {field} is invalid.")
        _keys(nested, allowed, field)
        for nested_name, nested_value in nested.items():
            _bounded_string(nested_value, f"{field} {nested_name}", maximum=2_048)


def _validate_identity_property_projection(value: dict[str, Any]) -> None:
    """Validate the exact property fields requested by identity discovery."""
    _keys(value, {"name", "timeZone", "currencyCode"}, "projected property")
    _validate_property_response(value)


def _identity_stream_projection(value: dict[str, Any], property_id: str) -> tuple[str, str]:
    """Return one exact, property-bound stream identity from a list projection."""
    _keys(value, {"name", "type"}, "projected data stream")
    _validate_stream(value)
    name = value.get("name")
    stream_type = value.get("type")
    if type(name) is not str or type(stream_type) is not str:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 projected data stream is invalid.")
    prefix = f"properties/{property_id}/dataStreams/"
    stream_id = name.removeprefix(prefix)
    if (
        not name.startswith(prefix)
        or re.fullmatch(r"[0-9]{1,20}", stream_id) is None
        or stream_type not in _IDENTITY_STREAM_TYPES
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 projected data stream is invalid.")
    return stream_id, stream_type


class _MetadataFacts(StrictModel):
    """The documented `ResponseMetaData` facts this adapter is allowed to keep."""

    quality: list[QualityStatus] = Field(default_factory=list, max_length=8)
    thresholded: bool = False
    other_row: bool = False
    empty_reason: str | None = Field(default=None, max_length=160)
    sampling: tuple[Ga4SamplingWindow, ...] = Field(default_factory=tuple, max_length=8)
    restrictions: tuple[Ga4MetricRestriction, ...] = Field(default_factory=tuple, max_length=32)


def _validate_metadata(value: dict[str, Any]) -> _MetadataFacts:
    """Validate `ResponseMetaData` against the documented v1beta field names.

    `SamplingMetadata` carries the singular scalar fields `samplesReadCount`
    and `samplingSpaceSize`, and `ActiveMetricRestriction` carries the plural
    `restrictedMetricTypes` enum list.  See
    docs/research/ga4-data-aggregate-contract.md for the cited definitions.
    """
    _keys(
        value,
        {
            "currencyCode",
            "timeZone",
            "dataLossFromOtherRow",
            "samplingMetadatas",
            "schemaRestrictionResponse",
            "subjectToThresholding",
            "emptyReason",
        },
        "metadata",
    )
    for name in ("dataLossFromOtherRow", "subjectToThresholding"):
        if name in value and not isinstance(value[name], bool):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GA4 metadata {name} is invalid.")
    empty_reason = value.get("emptyReason")
    if empty_reason is not None:
        empty_reason = _bounded_string(empty_reason, "metadata empty reason", maximum=160)
    sampling = value.get("samplingMetadatas", [])
    if not isinstance(sampling, list) or len(sampling) > 4:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 sampling metadata is invalid.")
    windows: list[Ga4SamplingWindow] = []
    for index, item in enumerate(sampling):
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 sampling metadata item is invalid.")
        _keys(item, {"samplesReadCount", "samplingSpaceSize"}, "sampling metadata")
        windows.append(
            Ga4SamplingWindow(
                date_range_index=index,
                samples_read_count=_nonnegative(
                    item.get("samplesReadCount"), "sampling metadata read count"
                ),
                sampling_space_size=_nonnegative(
                    item.get("samplingSpaceSize"), "sampling metadata space size"
                ),
            )
        )
    restriction = value.get("schemaRestrictionResponse")
    restrictions: list[Ga4MetricRestriction] = []
    if restriction is not None:
        if not isinstance(restriction, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GA4 schema restriction metadata is invalid."
            )
        _keys(restriction, {"activeMetricRestrictions"}, "schema restriction metadata")
        active = restriction.get("activeMetricRestrictions", [])
        if not isinstance(active, list) or len(active) > 32:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 metric restrictions are invalid.")
        for item in active:
            if not isinstance(item, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 metric restriction is invalid.")
            _keys(item, {"metricName", "restrictedMetricTypes"}, "metric restriction")
            kinds = item.get("restrictedMetricTypes", [])
            if not isinstance(kinds, list) or len(kinds) > 8:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GA4 metric restriction types are invalid."
                )
            restrictions.append(
                Ga4MetricRestriction(
                    metric_name=_bounded_string(
                        item.get("metricName"), "restricted metric name", maximum=160
                    ),
                    restricted_metric_types=tuple(
                        _bounded_string(kind, "restricted metric type", maximum=80)
                        for kind in kinds
                    ),
                )
            )
    statuses: list[QualityStatus] = []
    if windows:
        statuses.append("SAMPLED")
    if value.get("subjectToThresholding") is True:
        statuses.append("THRESHOLDED")
    if value.get("dataLossFromOtherRow") is True or restriction is not None:
        statuses.append("PROVIDER_REPORTED_UNKNOWN")
    return _MetadataFacts(
        quality=statuses,
        thresholded=value.get("subjectToThresholding") is True,
        other_row=value.get("dataLossFromOtherRow") is True,
        empty_reason=empty_reason,
        sampling=tuple(windows),
        restrictions=tuple(restrictions),
    )


class Ga4Identity(StrictModel):
    schema_version: Literal[1] = 1
    property_ref: SafeRef
    property_client_ref: SafeRef
    web_stream_ref: SafeRef
    web_stream_client_ref: SafeRef
    timezone: str = Field(min_length=1, max_length=80)
    currency: Currency | None = None
    admin_api_version: Literal["v1beta"] = "v1beta"


class Ga4ReportQuery(StrictModel):
    property_id: str = Field(pattern=r"^[0-9]{1,20}$")
    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_start: date
    window_end: date
    dimensions: list[str] = Field(default_factory=list, max_length=4)
    metrics: list[str] = Field(min_length=1, max_length=4)
    max_rows: int = Field(default=10_000, ge=1, le=10_000)
    page_size: int = Field(default=1_000, ge=1, le=10_000)
    source_timezone: str = Field(min_length=1, max_length=80)
    currency: Currency | None = None

    @field_validator("dimensions")
    @classmethod
    def standard_dimensions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(item not in _STANDARD_DIMENSIONS for item in value):
            raise ValueError("GA4 report dimensions are not allowlisted")
        return value

    @field_validator("metrics")
    @classmethod
    def standard_metrics(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(item not in _STANDARD_METRICS for item in value):
            raise ValueError("GA4 report metrics are not allowlisted")
        return value

    @model_validator(mode="after")
    def valid_window(self) -> Ga4ReportQuery:
        if self.window_end < self.window_start:
            raise ValueError("GA4 report window end must not precede its start")
        if (self.window_end - self.window_start).days > 366:
            raise ValueError("GA4 report window must be bounded to 367 days")
        if self.max_rows > self.page_size * 10:
            raise ValueError("GA4 complete-report bound exceeds the ten-page limit")
        return self


class Ga4RealtimeQuery(StrictModel):
    property_id: str = Field(pattern=r"^[0-9]{1,20}$")
    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    allowed_event_names: list[str] = Field(min_length=1, max_length=20)
    metrics: list[str] = Field(default_factory=lambda: ["eventCount"], min_length=1, max_length=3)
    max_rows: int = Field(default=1_000, ge=1, le=1_000)
    page_size: int = Field(default=1_000, ge=1, le=1_000)
    source_timezone: str = Field(min_length=1, max_length=80)
    currency: Currency | None = None

    @field_validator("allowed_event_names")
    @classmethod
    def declared_events_only(cls, value: list[str]) -> list[str]:
        import re

        if len(value) != len(set(value)) or any(
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", item) for item in value
        ):
            raise ValueError("Realtime event names must be declared safe identifiers")
        return value

    @field_validator("metrics")
    @classmethod
    def realtime_metrics(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(item not in _REALTIME_METRICS for item in value):
            raise ValueError("GA4 realtime metrics are not allowlisted")
        return value

    @model_validator(mode="after")
    def bounded_pages(self) -> Ga4RealtimeQuery:
        if self.max_rows > self.page_size * 10:
            raise ValueError("GA4 complete-report bound exceeds the ten-page limit")
        return self


class Ga4AggregateQuery(StrictModel):
    """A fixed, reviewed GA4 aggregate request.

    The caller selects one reviewed shape.  Dimension and metric names are
    never accepted from input, so an unreviewed GA4 field cannot reach the
    provider or the local ledger.
    """

    aggregate_shape: Ga4AggregateShape
    property_id: str = Field(pattern=r"^[0-9]{1,20}$")
    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    # Property-local calendar dates.  GA4 `DateRange` treats both ends as
    # inclusive; the adapter records the exact UTC instants they denote.
    window_start: date
    window_end: date
    # The property's configured key events, supplied by the caller from the
    # GA4 Admin inventory.  The Data API cannot enumerate them.
    key_event_names: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    page_size: int = Field(default=1_000, ge=1, le=10_000)
    page_bound: int = Field(default=10, ge=1, le=_MAX_AGGREGATE_PAGES)
    source_timezone: str = Field(min_length=1, max_length=80)
    # Required: an aggregate report is refused unless the property currency is
    # bound before any data query.
    currency: Currency

    @field_validator("key_event_names")
    @classmethod
    def declared_events_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", item) is None for item in value
        ):
            raise ValueError("GA4 key-event names must be declared safe identifiers")
        return value

    @model_validator(mode="after")
    def fixed_shape_is_complete(self) -> Ga4AggregateQuery:
        if self.window_end < self.window_start:
            raise ValueError("GA4 report window end must not precede its start")
        if (self.window_end - self.window_start).days > 366:
            raise ValueError("GA4 report window must be bounded to 367 days")
        if (self.aggregate_shape in _KEY_EVENT_SHAPES) != bool(self.key_event_names):
            raise ValueError("Only key-event aggregates declare the property's key events")
        if self.page_size * self.page_bound > _MAX_AGGREGATE_ROWS:
            raise ValueError("GA4 aggregate page bound exceeds the normalized row limit")
        return self


class _ParsedPage(StrictModel):
    row_count: int = Field(ge=0)
    rows: list[tuple[dict[str, str], dict[str, int]]] = Field(default_factory=list)
    quality: list[QualityStatus] = Field(default_factory=list)
    timezone: str | None = None
    currency: Currency | None = None
    metadata: _MetadataFacts = Field(default_factory=_MetadataFacts)


def _row_quality(base: list[QualityStatus], extra: list[QualityStatus]) -> list[QualityStatus]:
    """Return one de-duplicated, ordered quality list for a persisted row.

    Every GA4 aggregate row is `UNMAPPED` until ticket M00.2 resolves the
    deterministic provider join.
    """
    return sorted({*base, *extra, "UNMAPPED"})


def _cell(
    metric: Ga4AggregateMetric,
    value: int | None,
    measured: Ga4CellStatus,
    restricted: set[str],
) -> Ga4MetricCell:
    """Represent one measurement without ever substituting zero for absence."""
    if metric in restricted:
        return Ga4MetricCell(metric=metric, status="RESTRICTED")
    if value is None:
        return Ga4MetricCell(metric=metric, status="NOT_RETURNED")
    return Ga4MetricCell(metric=metric, status=measured, value=value)


def _bound_property_settings(
    query: Ga4AggregateQuery, identity: Ga4Identity
) -> tuple[ZoneInfo, str]:
    """Bind the property timezone and currency before any aggregate egress."""
    zone = _timezone(identity.timezone, "identity timezone")
    if identity.currency is None:
        raise AdhelmError(
            "EXPECTED_CURRENCY_REQUIRED",
            "GA4 property currency is unavailable; the fixed aggregate report is refused.",
            exit_code=2,
            next_action="Re-read GA4 property identity with a profile that returns currencyCode.",
        )
    if query.source_timezone != identity.timezone:
        raise AdhelmError(
            "TIMEZONE_MISMATCH", "GA4 aggregate timezone differs from property identity."
        )
    if query.currency != identity.currency:
        raise AdhelmError(
            "CURRENCY_MISMATCH", "GA4 aggregate currency differs from verified identity."
        )
    return zone, identity.currency


class Ga4DataClient:
    """GA4 aggregate/realtime reader plus readonly Admin identity lookup.

    ``token_getter`` is deliberately the only authentication input; the token
    is used only in the HTTP header and is never added to an exception, model,
    report, or ledger payload.
    """

    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        *,
        reference_for: ReferenceFor = _default_reference_for,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        connector_version: str = "0.1.0.dev0",
    ) -> None:
        hosts = set(transport.allowed_hosts)
        if not {_DATA_HOST, _ADMIN_HOST}.issubset(hosts):
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "GA4 adapter requires fixed Data and Admin hosts."
            )
        self._transport = transport
        self._token_getter = token_getter
        self._reference_for = reference_for
        self._now = now
        self._connector_version = connector_version

    def discover_identity(
        self, property_id: str, *, expected_web_stream_id: str | None = None
    ) -> Ga4Identity:
        property_id = _property_id(property_id)
        if expected_web_stream_id is not None:
            _property_id(expected_web_stream_id)
        if self._transport.client.params:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID",
                "GA4 identity lookup requires no default query parameters.",
            )
        headers = {"authorization": f"Bearer {_safe_token(self._token_getter)}"}
        property_response = self._transport.request(
            "GET",
            f"https://{_ADMIN_HOST}/v1beta/properties/{property_id}",
            headers=headers,
            params={"fields": _IDENTITY_PROPERTY_FIELDS},
        )
        property_data = _json(property_response, "property")
        _validate_identity_property_projection(property_data)
        if property_data.get("name") != f"properties/{property_id}":
            raise AdhelmError(
                "IDENTITY_MISMATCH", "GA4 property identity does not match the configured profile."
            )
        timezone = _bounded_string(property_data.get("timeZone"), "property timezone", maximum=80)
        _timezone(timezone, "property timezone")
        currency_value = property_data.get("currencyCode")
        currency = (
            _bounded_string(currency_value, "property currency", maximum=3)
            if currency_value
            else None
        )
        if currency is not None and (
            len(currency) != 3 or not currency.isupper() or not currency.isalpha()
        ):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 property currency is invalid.")

        selected: str | None = None
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        seen_stream_ids: set[str] = set()
        for _ in range(_MAX_IDENTITY_PAGES):
            params: dict[str, Any] = {
                "pageSize": _IDENTITY_PAGE_SIZE,
                "fields": _IDENTITY_STREAM_LIST_FIELDS,
            }
            if page_token is not None:
                params["pageToken"] = page_token
            streams_response = self._transport.request(
                "GET",
                f"https://{_ADMIN_HOST}/v1beta/properties/{property_id}/dataStreams",
                headers=headers,
                params=params,
            )
            stream_data = _json(streams_response, "data-stream list")
            _keys(stream_data, {"dataStreams", "nextPageToken"}, "data-stream list")
            streams = stream_data.get("dataStreams", [])
            if not isinstance(streams, list) or len(streams) > 200:
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 data-stream list is invalid.")
            for stream in streams:
                if not isinstance(stream, dict):
                    raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 data stream is invalid.")
                stream_id, stream_type = _identity_stream_projection(stream, property_id)
                if stream_id in seen_stream_ids:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID",
                        "GA4 data-stream list contains a duplicate resource.",
                    )
                seen_stream_ids.add(stream_id)
                if len(seen_stream_ids) > _MAX_IDENTITY_STREAMS:
                    raise AdhelmError(
                        "PAGINATION_LIMIT", "GA4 data-stream inventory exceeds the item bound."
                    )
                if stream_type != "WEB_DATA_STREAM":
                    continue
                if expected_web_stream_id is None or stream_id == expected_web_stream_id:
                    if selected is not None:
                        raise AdhelmError(
                            "IDENTITY_AMBIGUOUS", "Multiple GA4 web streams match this profile."
                        )
                    selected = stream_id
            next_token = stream_data.get("nextPageToken")
            if next_token is None:
                break
            page_token = _bounded_string(next_token, "data-stream page token", maximum=2048)
            if not streams or page_token in seen_page_tokens:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GA4 data-stream pagination made no safe progress."
                )
            seen_page_tokens.add(page_token)
        else:
            raise AdhelmError(
                "PAGINATION_LIMIT", "GA4 data-stream inventory exceeded the page limit."
            )
        if selected is None:
            raise AdhelmError(
                "NOT_CONFIGURED_OPTIONAL", "Configured GA4 property has no matching web stream."
            )
        return Ga4Identity(
            property_ref=self._reference_for("property", property_id),
            property_client_ref=property_id,
            web_stream_ref=self._reference_for("stream", selected),
            web_stream_client_ref=selected,
            timezone=timezone,
            currency=currency,
        )

    def run_report(self, query: Ga4ReportQuery, *, identity: Ga4Identity) -> NormalizedReport:
        property_id = self._identity_matches(query.account_ref, query.property_id, identity)
        if query.currency is not None and query.currency != identity.currency:
            raise AdhelmError(
                "CURRENCY_MISMATCH", "GA4 query currency differs from verified identity."
            )
        if query.source_timezone != identity.timezone:
            raise AdhelmError(
                "TIMEZONE_MISMATCH", "GA4 report timezone differs from property identity."
            )
        self._reject_future_window(query, identity)
        body = {
            "dateRanges": [
                {
                    "startDate": query.window_start.isoformat(),
                    "endDate": query.window_end.isoformat(),
                }
            ],
            "dimensions": [{"name": item} for item in query.dimensions],
            "metrics": [{"name": item} for item in query.metrics],
            "keepEmptyRows": False,
            "limit": str(query.page_size),
            "offset": "0",
        }
        pages = self._read_pages(
            f"https://{_DATA_HOST}/v1beta/properties/{property_id}:runReport",
            body,
            query.dimensions,
            query.metrics,
            query.max_rows,
            realtime=False,
        )
        self._verify_metadata(pages, identity)
        observed_at = self._timestamp()
        quality = self._combined_quality(pages)
        envelope = ReportEnvelope(
            report_id=query.report_id,
            provider="ga4",
            source_account_ref=query.account_ref,
            query_contract_hash=query.query_contract_hash,
            window_start=query.window_start,
            window_end=query.window_end,
            source_timezone=identity.timezone,
            report_timezone=identity.timezone,
            currency=identity.currency or query.currency,
            # The Data API exposes no attribution model on this surface; the
            # earlier "GA4_DATA_API" label implied more than is knowable.
            attribution_model=GA4_ATTRIBUTION_DISCLOSURE,
            attribution_window=None,
            observed_at=observed_at,
            data_fresh_through=None,
            data_quality_statuses=quality,
            provider_api_version="v1beta",
            connector_version=self._connector_version,
        )
        rows = [
            self._standard_row(query, values, metrics, quality)
            for page in pages
            for values, metrics in page.rows
        ]
        return NormalizedReport(envelope=envelope, performance_rows=rows)

    def run_realtime_report(
        self, query: Ga4RealtimeQuery, *, identity: Ga4Identity
    ) -> NormalizedReport:
        property_id = self._identity_matches(query.account_ref, query.property_id, identity)
        if query.currency is not None and query.currency != identity.currency:
            raise AdhelmError(
                "CURRENCY_MISMATCH", "GA4 query currency differs from verified identity."
            )
        if query.source_timezone != identity.timezone:
            raise AdhelmError(
                "TIMEZONE_MISMATCH", "GA4 report timezone differs from property identity."
            )
        dimensions = ["eventName"]
        body = {
            "dimensions": [{"name": "eventName"}],
            "metrics": [{"name": item} for item in query.metrics],
            "limit": str(query.page_size),
            "dimensionFilter": {
                "filter": {
                    "fieldName": "eventName",
                    "inListFilter": {"values": query.allowed_event_names, "caseSensitive": True},
                }
            },
        }
        pages = self._read_pages(
            f"https://{_DATA_HOST}/v1beta/properties/{property_id}:runRealtimeReport",
            body,
            dimensions,
            query.metrics,
            query.max_rows,
            realtime=True,
        )
        self._verify_metadata(pages, identity)
        observed_at = self._timestamp()
        quality = self._combined_quality(pages, realtime=True)
        envelope = ReportEnvelope(
            report_id=query.report_id,
            provider="ga4",
            source_account_ref=query.account_ref,
            query_contract_hash=query.query_contract_hash,
            window_start=observed_at.date(),
            window_end=observed_at.date(),
            source_timezone=identity.timezone,
            report_timezone=identity.timezone,
            currency=identity.currency or query.currency,
            attribution_model="GA4_REALTIME",
            attribution_window=None,
            observed_at=observed_at,
            data_fresh_through=None,
            data_quality_statuses=quality,
            provider_api_version="v1beta",
            connector_version=self._connector_version,
        )
        rows: list[PerformanceRow] = []
        conversions: list[ConversionBreakdownRow] = []
        for page in pages:
            for values, metrics in page.rows:
                event_name = values["eventName"]
                if event_name not in query.allowed_event_names:
                    raise AdhelmError(
                        "SENSITIVE_DATA_RISK", "GA4 realtime response included an undeclared event."
                    )
                event_ref = self._reference_for("event", event_name)
                rows.append(
                    PerformanceRow(
                        report_id=query.report_id,
                        resource_level="ACCOUNT",
                        metric_grain="ACCOUNT",
                        resource_type="GA4_EVENT",
                        resource_ref=query.account_ref,
                        dimension_type="GA4_EVENT",
                        dimension_value=event_ref,
                        provider_reported_results=metrics.get("keyEvents"),
                        active_users=metrics.get("activeUsers"),
                        event_count=metrics.get("eventCount"),
                        key_events=metrics.get("keyEvents"),
                        data_quality_statuses=quality,
                    )
                )
                if "keyEvents" in metrics:
                    conversions.append(
                        ConversionBreakdownRow(
                            report_id=query.report_id,
                            resource_ref=query.account_ref,
                            conversion_source="GA4",
                            conversion_action_ref=event_ref,
                            conversion_kind="KEY_EVENT",
                            counting_method="GA4_CONFIGURED",
                            attribution_model="GA4_REALTIME",
                            conversion_count=metrics["keyEvents"],
                            data_quality_statuses=quality,
                        )
                    )
        return NormalizedReport(
            envelope=envelope, performance_rows=rows, conversion_rows=conversions
        )

    def run_aggregate_report(
        self,
        query: Ga4AggregateQuery,
        *,
        identity: Ga4Identity,
        join_map: CampaignJoinMap | None = None,
    ) -> Ga4AggregateReport:
        """Read one fixed GA4 aggregate with explicit completeness and quality.

        Property timezone and currency are bound from the already-verified
        identity before any data query; the read is refused when either is
        unavailable.  Exceeding the page bound or failing `rowCount`
        reconciliation downgrades completeness instead of silently presenting a
        partial table as exhaustive.

        ``join_map`` is an optional, already-retained, customer-declared join
        map (ticket M00.2).  It changes no request, adds no egress, and only
        attaches the declared join state to the returned rows.  Without it the
        rows keep `UNMAPPED` and the deferred join disclosure.
        """
        property_id = self._identity_matches(query.account_ref, query.property_id, identity)
        zone, currency = _bound_property_settings(query, identity)
        self._reject_future_end(query.window_end, identity)
        dimensions, metrics = _AGGREGATE_REQUESTS[query.aggregate_shape]
        body: dict[str, Any] = {
            "dateRanges": [
                {
                    "startDate": query.window_start.isoformat(),
                    "endDate": query.window_end.isoformat(),
                }
            ],
            "dimensions": [{"name": item} for item in dimensions],
            "metrics": [{"name": item} for item in metrics],
            "keepEmptyRows": False,
            "limit": str(query.page_size),
            "offset": "0",
            # A fixed total order makes limit/offset paging and the normalized
            # output deterministic for one unchanged provider result.
            "orderBys": [
                {"dimension": {"dimensionName": item, "orderType": "ALPHANUMERIC"}, "desc": False}
                for item in dimensions
            ],
        }
        if query.aggregate_shape in _KEY_EVENT_SHAPES:
            body["dimensionFilter"] = {
                "filter": {
                    "fieldName": "eventName",
                    "inListFilter": {
                        "values": list(query.key_event_names),
                        "caseSensitive": True,
                    },
                }
            }
        pages, declared, completeness = self._read_aggregate_pages(
            f"https://{_DATA_HOST}/v1beta/properties/{property_id}:runReport",
            body,
            list(dimensions),
            list(metrics),
            page_bound=query.page_bound,
        )
        self._verify_metadata(pages, identity)
        observed = [values for page in pages for values, _ in page.rows]
        measurements = [numbers for page in pages for _, numbers in page.rows]
        consumed = len(observed)
        facts = [page.metadata for page in pages]
        # The adapter sends exactly one date range, and GA4 documents one
        # `samplingMetadatas` entry per date range.  More entries than ranges
        # means the response does not answer the request that was sent.
        if any(len(fact.sampling) > 1 for fact in facts):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "GA4 returned more sampling metadata than the requested date ranges.",
            )
        # Every page must therefore report the same single window.
        # Concatenating pages would multiply one window by the page count and
        # silently misstate the sample ratio.
        sampling: tuple[Ga4SamplingWindow, ...] = next(
            (fact.sampling for fact in facts if fact.sampling), ()
        )
        sampling_consistency: Ga4SamplingConsistency = "NOT_SAMPLED"
        if sampling:
            sampling_consistency = (
                "CONSISTENT"
                if all(fact.sampling == sampling for fact in facts)
                else "SAMPLING_METADATA_INCONSISTENT"
            )
        restrictions: tuple[Ga4MetricRestriction, ...] = ()
        for fact in facts:
            if fact.restrictions:
                restrictions = fact.restrictions
                break
        empty_reason = next((fact.empty_reason for fact in facts if fact.empty_reason), None)
        thresholded = any(fact.thresholded for fact in facts)
        other_row = any(fact.other_row for fact in facts)
        rows = self._aggregate_rows(
            query,
            observed,
            measurements,
            metrics=metrics,
            sampled=bool(sampling),
            thresholded=thresholded,
            restricted={item.metric_name for item in restrictions},
            base_quality=sorted({status for page in pages for status in page.quality}),
        )
        # A GA4 "(other)" dimension value is itself proof of roll-up, whether or
        # not the response also set `dataLossFromOtherRow`.
        other_row = other_row or any(
            item.endswith("_OTHER_ROW") for row in rows for item in row.dimension_statuses
        )
        if completeness == "COMPLETE":
            if declared is None or declared != consumed:
                completeness = "INCOMPLETE_ROW_COUNT_MISMATCH"
            elif empty_reason is not None:
                # GA4 declined to report, for a reason it does not enumerate.
                # That is never evidence that zero rows match the query.
                completeness = "NOT_AVAILABLE_FROM_PROVIDER"
            elif consumed == 0:
                # Thresholding suppresses rows below the minimum aggregation
                # threshold, so no returned rows does not mean no activity.
                completeness = "EMPTY_THRESHOLDED" if thresholded else "EMPTY"
            elif any(cell.status == "THRESHOLD_WITHHELD" for row in rows for cell in row.cells):
                # Every provider row was consumed, but a configured key event
                # was suppressed, so this is not every matching row.
                completeness = "WITHHELD"
        report_quality: set[QualityStatus] = {status for page in pages for status in page.quality}
        if completeness in {"INCOMPLETE_PAGE_BOUND_EXCEEDED", "INCOMPLETE_ROW_COUNT_MISMATCH"}:
            report_quality.add("PARTIAL_PAGE_FAILURE")
        if other_row or sampling_consistency == "SAMPLING_METADATA_INCONSISTENT":
            report_quality.add("PROVIDER_REPORTED_UNKNOWN")
        if empty_reason is not None:
            report_quality.add("NOT_AVAILABLE_FROM_PROVIDER")
        if any("MISSING_EVENT" in row.data_quality_statuses for row in rows):
            report_quality.add("MISSING_EVENT")
        if not report_quality:
            report_quality.add("COMPLETE")
        observed_at = self._timestamp()
        envelope = ReportEnvelope(
            report_id=query.report_id,
            provider="ga4",
            source_account_ref=query.account_ref,
            query_contract_hash=query.query_contract_hash,
            window_start=query.window_start,
            window_end=query.window_end,
            source_timezone=identity.timezone,
            report_timezone=identity.timezone,
            currency=currency,
            # The Data API exposes no attribution model on this surface.
            attribution_model=GA4_ATTRIBUTION_DISCLOSURE,
            attribution_window=None,
            observed_at=observed_at,
            # No provider freshness instant exists to record.
            data_fresh_through=None,
            data_quality_statuses=sorted(report_quality),
            provider_api_version="v1beta",
            connector_version=self._connector_version,
        )
        report = Ga4AggregateReport(
            envelope=envelope,
            aggregate_shape=query.aggregate_shape,
            completeness=completeness,
            declared_row_count=declared,
            consumed_row_count=consumed,
            pages_consumed=len(pages),
            page_bound=query.page_bound,
            page_size=query.page_size,
            window_start_instant=datetime.combine(
                query.window_start, time.min, tzinfo=zone
            ).astimezone(UTC),
            window_end_exclusive_instant=datetime.combine(
                query.window_end + timedelta(days=1), time.min, tzinfo=zone
            ).astimezone(UTC),
            property_timezone=identity.timezone,
            property_currency=currency,
            subject_to_thresholding=thresholded,
            other_row_aggregation=other_row,
            sampling=sampling,
            sampling_consistency=sampling_consistency,
            restricted_metrics=restrictions,
            empty_reason=empty_reason,
            declared_key_events=query.key_event_names,
            rows=rows,
        )
        if join_map is None:
            return report
        # The join is a pure, local projection over rows already validated by
        # the read boundary.  It is applied after the report is complete so a
        # join failure can never be mistaken for a provider read failure.
        from adhelm.campaign_joins import apply_join_map

        return apply_join_map(report, join_map)

    def _read_aggregate_pages(
        self,
        url: str,
        body: dict[str, Any],
        dimensions: list[str],
        metrics: list[str],
        *,
        page_bound: int,
    ) -> tuple[list[_ParsedPage], int | None, Ga4Completeness]:
        """Consume limit/offset pages up to a bound without raising on a bound.

        A bound overrun is a reportable completeness fact, not a transport
        failure: the caller must still see how many rows were consumed.
        """
        headers = {"authorization": f"Bearer {_safe_token(self._token_getter)}"}
        pages: list[_ParsedPage] = []
        offset = 0
        declared: int | None = None
        completeness: Ga4Completeness = "COMPLETE"
        for _ in range(page_bound):
            response = self._transport.request(
                "POST", url, headers=headers, json={**body, "offset": str(offset)}, mutation=False
            )
            page = self._parse_page(_json(response, "report"), dimensions, metrics, realtime=False)
            if declared is None:
                declared = page.row_count
            elif declared != page.row_count:
                # The provider result changed underneath this read.  Report it
                # rather than stitching two different tables together: the
                # disagreeing page is discarded, not merged.
                completeness = "INCOMPLETE_ROW_COUNT_MISMATCH"
                break
            pages.append(page)
            offset += len(page.rows)
            if offset > page.row_count:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GA4 aggregate rows exceed the declared total."
                )
            if offset >= page.row_count:
                break
            if not page.rows:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GA4 aggregate pagination made no progress."
                )
        else:
            completeness = "INCOMPLETE_PAGE_BOUND_EXCEEDED"
        return pages, declared, completeness

    def _aggregate_rows(
        self,
        query: Ga4AggregateQuery,
        observed: list[dict[str, str]],
        measurements: list[dict[str, int]],
        *,
        metrics: tuple[Ga4AggregateMetric, ...],
        sampled: bool,
        thresholded: bool,
        restricted: set[str],
        base_quality: list[QualityStatus],
    ) -> tuple[Ga4AggregateRow, ...]:
        measured: Ga4CellStatus = "SAMPLED_ESTIMATE" if sampled else "OBSERVED"
        rows: list[Ga4AggregateRow] = []
        seen: set[tuple[str | None, str | None, str | None, str | None]] = set()
        for values, numbers in zip(observed, measurements, strict=True):
            event_name = values.get("eventName")
            rolled_up_event = event_name == _OTHER_ROW
            # GA4 rolls high-cardinality dimension values into a documented
            # "(other)" row, and its own guidance states that filters do not
            # look inside that row.  The eventName inListFilter therefore
            # cannot exclude it, so it is an expected aggregation fact.  Any
            # other undeclared name is still an undeclared event.
            if (
                event_name is not None
                and not rolled_up_event
                and event_name not in query.key_event_names
            ):
                raise AdhelmError(
                    "SENSITIVE_DATA_RISK",
                    "GA4 aggregate response included an undeclared event name.",
                )
            campaign = values.get("sessionCampaignName")
            source = values.get("sessionSource")
            medium = values.get("sessionMedium")
            key = (campaign, source, medium, event_name)
            if key in seen:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "GA4 aggregate returned a duplicate dimension combination.",
                )
            seen.add(key)
            statuses: list[Ga4DimensionStatus] = []
            if campaign == _NOT_SET:
                statuses.append("CAMPAIGN_NOT_SET")
            elif campaign == _OTHER_ROW:
                statuses.append("CAMPAIGN_OTHER_ROW")
            if source == _NOT_SET:
                statuses.append("SOURCE_NOT_SET")
            elif source == _OTHER_ROW:
                statuses.append("SOURCE_OTHER_ROW")
            if medium == _NOT_SET:
                statuses.append("MEDIUM_NOT_SET")
            elif medium == _OTHER_ROW:
                statuses.append("MEDIUM_OTHER_ROW")
            if rolled_up_event:
                statuses.append("EVENT_OTHER_ROW")
            rows.append(
                Ga4AggregateRow(
                    report_id=query.report_id,
                    aggregate_shape=query.aggregate_shape,
                    # Dimension values are retained exactly as GA4 returned
                    # them so ticket M00.2 can join on the provider's own text.
                    session_campaign_name=campaign,
                    session_source=source,
                    session_medium=medium,
                    event_name=event_name,
                    conversion_action_ref=(
                        self._reference_for("event", event_name)
                        if query.aggregate_shape == "KEY_EVENT_TOTALS"
                        and event_name is not None
                        and not rolled_up_event
                        else None
                    ),
                    dimension_statuses=tuple(statuses),
                    cells=tuple(
                        _cell(metric, numbers.get(metric), measured, restricted)
                        for metric in metrics
                    ),
                    data_quality_statuses=_row_quality(
                        base_quality,
                        # A rolled-up bucket aggregates values GA4 declined to
                        # name; "(not set)" is a real reported value.
                        ["PROVIDER_REPORTED_UNKNOWN"]
                        if any(item.endswith("_OTHER_ROW") for item in statuses)
                        else [],
                    ),
                )
            )
        if query.aggregate_shape == "KEY_EVENT_TOTALS":
            # A configured key event with no returned row is withheld or
            # simply absent.  It is never rendered as a zero count.
            returned = {
                row.event_name for row in rows if "EVENT_OTHER_ROW" not in row.dimension_statuses
            }
            missing_status: Ga4CellStatus = "THRESHOLD_WITHHELD" if thresholded else "NOT_RETURNED"
            missing_quality: QualityStatus = "THRESHOLDED" if thresholded else "MISSING_EVENT"
            for name in query.key_event_names:
                if name in returned:
                    continue
                rows.append(
                    Ga4AggregateRow(
                        report_id=query.report_id,
                        aggregate_shape=query.aggregate_shape,
                        event_name=name,
                        conversion_action_ref=self._reference_for("event", name),
                        dimension_statuses=("KEY_EVENT_NOT_RETURNED",),
                        cells=tuple(
                            Ga4MetricCell(metric=metric, status=missing_status)
                            for metric in metrics
                        ),
                        data_quality_statuses=_row_quality(base_quality, [missing_quality]),
                    )
                )
        rows.sort(
            key=lambda row: (
                row.session_campaign_name or "",
                row.session_source or "",
                row.session_medium or "",
                row.event_name or "",
            )
        )
        return tuple(rows)

    def _identity_matches(self, account_ref: str, property_id: str, identity: Ga4Identity) -> str:
        # Typed identities may be supplied directly; SafeRef is not a GA4 property-ID type.
        selected = _property_id(property_id)
        discovered = _property_id(identity.property_client_ref)
        if selected != discovered:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "GA4 report property differs from discovered identity."
            )
        if identity.property_ref != self._reference_for("property", discovered):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "GA4 identity reference differs from its property ID."
            )
        if account_ref != identity.property_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "GA4 query account differs from discovered property identity."
            )
        return discovered

    def _reject_future_window(self, query: Ga4ReportQuery, identity: Ga4Identity) -> None:
        """Fail before egress when the selected source-local date is future."""
        self._reject_future_end(query.window_end, identity)

    def _reject_future_end(self, window_end: date, identity: Ga4Identity) -> None:
        current = self._timestamp()
        source_today = current.astimezone(_timezone(identity.timezone, "identity timezone")).date()
        if window_end > source_today:
            raise AdhelmError(
                "INVALID_REPORT_WINDOW",
                "GA4 report window may not end after the current source-local date.",
            )

    def _read_pages(
        self,
        url: str,
        body: dict[str, Any],
        dimensions: list[str],
        metrics: list[str],
        maximum_rows: int,
        *,
        realtime: bool,
    ) -> list[_ParsedPage]:
        headers = {"authorization": f"Bearer {_safe_token(self._token_getter)}"}
        pages: list[_ParsedPage] = []
        offset = 0
        expected_count: int | None = None
        for _ in range(1 if realtime else 10):
            page_body = body if realtime else {**body, "offset": str(offset)}
            response = self._transport.request(
                "POST", url, headers=headers, json=page_body, mutation=False
            )
            page = self._parse_page(
                _json(response, "report"), dimensions, metrics, realtime=realtime
            )
            if expected_count is None:
                expected_count = page.row_count
            elif expected_count != page.row_count:
                raise AdhelmError(
                    "REPORT_CHANGED_DURING_PAGINATION", "Report row count changed between pages."
                )
            if page.row_count > maximum_rows:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "GA4 report exceeds the configured complete-result bound.",
                )
            pages.append(page)
            offset += len(page.rows)
            if offset > page.row_count:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "Report rows exceed the declared total."
                )
            if offset >= page.row_count:
                return pages
            if not page.rows:
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 pagination made no progress.")
        raise AdhelmError("PAGINATION_LIMIT", "GA4 report exceeded the bounded page count.")

    def _parse_page(
        self, data: dict[str, Any], dimensions: list[str], metrics: list[str], *, realtime: bool
    ) -> _ParsedPage:
        _keys(
            data,
            {"dimensionHeaders", "metricHeaders", "rows", "rowCount", "metadata", "kind"},
            "report",
        )
        dimension_headers = data.get("dimensionHeaders", [])
        metric_headers = data.get("metricHeaders", [])
        rows = data.get("rows", [])
        if (
            not isinstance(dimension_headers, list)
            or not isinstance(metric_headers, list)
            or not isinstance(rows, list)
        ):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 report collections are invalid.")
        if len(rows) > 10_000:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 report page exceeds the row bound.")
        if [item.get("name") for item in dimension_headers if isinstance(item, dict)] != dimensions:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GA4 dimension headers differ from the approved query."
            )
        if [item.get("name") for item in metric_headers if isinstance(item, dict)] != metrics:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GA4 metric headers differ from the approved query."
            )
        for header in [*dimension_headers, *metric_headers]:
            if not isinstance(header, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 report header is invalid.")
            _keys(header, {"name", "type"}, "report header")
            _optional_string(header.get("type"), "report header type", maximum=80)
        row_count = data.get("rowCount", 0)
        if type(row_count) is not int or not 0 <= row_count <= 9_007_199_254_740_991:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GA4 rowCount must be a nonnegative integer."
            )
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 metadata is invalid.")
        facts = _validate_metadata(metadata)
        quality = facts.quality
        timezone = metadata.get("timeZone")
        if timezone is not None:
            timezone = _bounded_string(timezone, "metadata timezone", maximum=80)
        currency = metadata.get("currencyCode")
        if currency is not None:
            currency = _bounded_string(currency, "metadata currency", maximum=3)
            if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 metadata currency is invalid.")
        parsed: list[tuple[dict[str, str], dict[str, int]]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 report row is invalid.")
            _keys(row, {"dimensionValues", "metricValues"}, "report row")
            raw_dimensions = row.get("dimensionValues")
            raw_metrics = row.get("metricValues")
            if not isinstance(raw_dimensions, list) or not isinstance(raw_metrics, list):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 report values are invalid.")
            if len(raw_dimensions) != len(dimensions) or len(raw_metrics) != len(metrics):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GA4 report value count differs from query."
                )
            dimension_values: dict[str, str] = {}
            for name, value in zip(dimensions, raw_dimensions, strict=True):
                if not isinstance(value, dict):
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "GA4 dimension value is invalid."
                    )
                _keys(value, {"value"}, "dimension value")
                dimension_values[name] = _bounded_string(value.get("value"), "dimension value")
            metric_values: dict[str, int] = {}
            for name, value in zip(metrics, raw_metrics, strict=True):
                if not isinstance(value, dict):
                    raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 metric value is invalid.")
                _keys(value, {"value"}, "metric value")
                metric_values[name] = _nonnegative(value.get("value"), "metric value")
            parsed.append((dimension_values, metric_values))
        return _ParsedPage(
            row_count=row_count,
            rows=parsed,
            quality=quality,
            timezone=timezone,
            currency=currency,
            metadata=facts,
        )

    def _standard_row(
        self,
        query: Ga4ReportQuery,
        values: dict[str, str],
        metrics: dict[str, int],
        quality: list[QualityStatus],
    ) -> PerformanceRow:
        campaign_id = values.get("sessionCampaignId")
        quality = list(quality)
        unmapped_campaign = campaign_id in {"(not set)", "(other)", ""}
        if unmapped_campaign:
            campaign_id = None
            quality.append("UNMAPPED")
        if campaign_id and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", campaign_id) is None:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GA4 campaign identifier is unsupported."
            )
        campaign_ref = self._reference_for("campaign", campaign_id) if campaign_id else None
        dimension_values: dict[str, str | int | bool | None] = {
            f"ga4_{name}": value for name, value in values.items() if name != "sessionCampaignId"
        }
        breakdown: dict[str, str] = {}
        names = {
            "date": "DATE",
            "sessionSource": "SESSION_SOURCE",
            "sessionMedium": "SESSION_MEDIUM",
        }
        for name, normalized in names.items():
            if name in values:
                value = values[name]
                if name == "date":
                    try:
                        observed_date = datetime.strptime(value, "%Y%m%d").date()
                    except ValueError as exc:
                        raise AdhelmError(
                            "PROVIDER_RESPONSE_INVALID", "GA4 date is invalid."
                        ) from exc
                    if not query.window_start <= observed_date <= query.window_end:
                        raise AdhelmError(
                            "PROVIDER_RESPONSE_INVALID", "GA4 date is outside the query."
                        )
                    value = observed_date.isoformat()
                breakdown[normalized] = value
        if unmapped_campaign:
            breakdown["CAMPAIGN_ID_STATUS"] = "UNMAPPED"
        dimension_type: str | None = None
        dimension_value: str | None = None
        if len(breakdown) == 1:
            dimension_type, dimension_value = next(iter(breakdown.items()))
        elif breakdown:
            dimension_type = "GA4_COMPOSITE_BREAKDOWN"
            dimension_value = canonical_bytes(breakdown).decode()
        if dimension_value is not None and len(dimension_value) > 160:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GA4 breakdown exceeds the safe output bound."
            )
        return PerformanceRow(
            report_id=query.report_id,
            resource_level="CAMPAIGN" if campaign_ref else "ACCOUNT",
            metric_grain="CAMPAIGN" if campaign_ref else "ACCOUNT",
            resource_type="GA4_CAMPAIGN" if campaign_ref else "GA4_PROPERTY",
            resource_ref=campaign_ref or query.account_ref,
            campaign_ref=campaign_ref,
            dimension_type=dimension_type,
            dimension_value=dimension_value,
            provider_reported_results=metrics.get("keyEvents"),
            sessions=metrics.get("sessions"),
            active_users=metrics.get("activeUsers"),
            event_count=metrics.get("eventCount"),
            key_events=metrics.get("keyEvents"),
            data_quality_statuses=quality,
            provider_extensions=dimension_values,
        )

    @staticmethod
    def _verify_metadata(pages: list[_ParsedPage], identity: Ga4Identity) -> None:
        for page in pages:
            if page.timezone is not None and page.timezone != identity.timezone:
                raise AdhelmError(
                    "TIMEZONE_MISMATCH", "Report timezone differs from verified identity."
                )
            if page.currency is not None and page.currency != identity.currency:
                raise AdhelmError(
                    "CURRENCY_MISMATCH", "Report currency differs from verified identity."
                )

    @staticmethod
    def _combined_quality(
        pages: list[_ParsedPage], *, realtime: bool = False
    ) -> list[QualityStatus]:
        statuses = {status for page in pages for status in page.quality}
        if realtime:
            statuses.add("PROCESSING_PENDING")
        elif not statuses:
            statuses.add("COMPLETE")
        return sorted(statuses)

    def _timestamp(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AdhelmError("CLOCK_INVALID", "Adapter clock must return an aware timestamp.")
        return value.astimezone(UTC)
