# ruff: noqa: E501
"""Independent review findings for P01-OA (OpenAI Ads), integrated at d31e69e.

P01-OA shipped after a coordinator self-review only.  This module is the
independent reviewer's evidence: every test below fails against the integrated
product code and states the behaviour the follow-up fix ticket must produce.
No product code is changed here.

Every case is offline.  Nothing contacts OpenAI, no credential is read from a
real OS store, and no test reconciles against OpenAI Ads Manager.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from adhelm import openai_account_key_lifecycle as lifecycle
from adhelm.audit import AuditPolicy
from adhelm.audits import (
    ProviderAuditContext,
    ProviderAuditEvidence,
    ProviderAuditSections,
    ResourceSnapshotState,
    RetainedCreativeCatalog,
    RetainedCreativeCatalogEntry,
    RetainedInventory,
    RetainedSnapshotRecord,
)
from adhelm.audits.openai_ads import evaluate
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Money, OpenAIAccountKeyProfile, OutputPreferences
from adhelm.providers.openai_ads import (
    OPENAI_ADS_AD_REVIEW_STATUS_RETURNED,
    OPENAI_ADS_CONTEXT_HINTS_DECLARED,
    OPENAI_ADS_CONVERSION_SETTINGS_ASSOCIATED,
    OPENAI_ADS_CREATIVE_BODY_PRESENT,
    OPENAI_ADS_CREATIVE_FILE_ASSOCIATED,
    OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT,
    OPENAI_ADS_CREATIVE_TITLE_PRESENT,
    OPENAI_ADS_TARGETING_DECLARED,
    OPENAI_ADS_TARGETING_NOT_DECLARED,
    OpenAIAdsAd,
    openai_ads_ad_quality_statuses,
)
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.transport import SafeHttpClient

OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, 18, tzinfo=UTC)
ACCOUNT = "openai_ads.account.acct_1"
CAMPAIGN = "openai_ads.campaign.camp_1"
AD_GROUP = "openai_ads.ad_group.grp_1"
AD = "openai_ads.ad.ad_1"
REPORT_DIGEST = "a" * 64

HEALTHY_CAMPAIGN_QUALITY = (
    OPENAI_ADS_TARGETING_DECLARED,
    OPENAI_ADS_CONVERSION_SETTINGS_ASSOCIATED,
)
HEALTHY_AD_QUALITY = (
    OPENAI_ADS_CREATIVE_FILE_ASSOCIATED,
    OPENAI_ADS_CREATIVE_TITLE_PRESENT,
    OPENAI_ADS_CREATIVE_BODY_PRESENT,
    OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT,
    OPENAI_ADS_AD_REVIEW_STATUS_RETURNED,
)


# --- shared offline fixtures --------------------------------------------------


def _digest(seed: int) -> str:
    return format(seed, "064x")


def _envelope() -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report:openai-review",
        provider="openai_ads",
        source_account_ref=ACCOUNT,
        query_contract_hash="b" * 64,
        window_start=date(2026, 8, 25),
        window_end=date(2026, 8, 31),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="OPENAI_ADS_PROVIDER_REPORTED",
        attribution_window=None,
        observed_at=OBSERVED,
        data_quality_statuses=["COMPLETE"],
        provider_api_version="v1",
        connector_version="test",
    )


def _row(
    *,
    resource_ref: str = CAMPAIGN,
    resource_level: str = "CAMPAIGN",
    metric_grain: str = "CAMPAIGN",
    resource_type: str = "OPENAI_ADS_CAMPAIGN",
    campaign_ref: str = CAMPAIGN,
    impressions: int | None = 100,
    clicks: int | None = 4,
    minor_units: int = 125,
) -> PerformanceRow:
    return PerformanceRow(
        report_id="report:openai-review",
        resource_level=resource_level,  # type: ignore[arg-type,unused-ignore]
        metric_grain=metric_grain,  # type: ignore[arg-type]
        resource_type=resource_type,
        resource_ref=resource_ref,
        campaign_ref=campaign_ref,
        dimension_type="DATE",
        dimension_value="2026-08-25",
        impressions=impressions,
        clicks=clicks,
        spend=Money(minor_units=minor_units, currency="USD"),
        data_quality_statuses=["COMPLETE"],
    )


def _report(rows: list[PerformanceRow] | None = None) -> NormalizedReport:
    return NormalizedReport(
        envelope=_envelope(),
        performance_rows=[_row()] if rows is None else rows,
    )


def _record(
    *,
    seed: int,
    resource_type: str,
    resource_ref: str,
    parents: tuple[str, ...] = (),
    state: dict[str, Any] | None = None,
    status: str = "active",
    quality: tuple[str, ...] = (),
) -> RetainedSnapshotRecord:
    normalized: dict[str, Any] = (
        state if state is not None else {"status": status, "currency": "USD"}
    )
    return RetainedSnapshotRecord(
        record_digest=_digest(seed),
        observed_at=OBSERVED,
        snapshot=ResourceSnapshotState(
            provider_api_version="v1",
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_ref=resource_ref,
            parent_resource_refs=parents,
            normalized_state=normalized,
            normalized_state_digest=digest(normalized),
            effective_status=status,
            data_quality_statuses=quality,
        ),
    )


def _records(
    *,
    status: str = "active",
    campaign_quality: tuple[str, ...] = HEALTHY_CAMPAIGN_QUALITY,
) -> tuple[RetainedSnapshotRecord, ...]:
    campaign_state = {
        "status": status,
        "currency": "USD",
        "bid_strategy": "clicks",
        "openai_ads_lifetime_spend_limit_micros": "10000000",
        "budget_type": "LIFETIME",
        "budget_minor": 1000,
        "lifetime_budget_minor": 1000,
        "schedule_start": "2026-08-01T00:00:00+00:00",
        "schedule_end": "2026-09-30T00:00:00+00:00",
    }
    group_state = {
        "status": status,
        "currency": "USD",
        "openai_ads_max_bid_micros": "250000",
        "bid_amount_minor": 25,
    }
    return (
        _record(seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state=campaign_state,
            status=status,
            quality=campaign_quality,
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state=group_state,
            status=status,
            quality=(OPENAI_ADS_CONTEXT_HINTS_DECLARED,),
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_GROUP),
            status=status,
            quality=HEALTHY_AD_QUALITY,
        ),
    )


def _inventory(
    records: tuple[RetainedSnapshotRecord, ...],
    *,
    completeness: str = "COMPLETE_BOUNDED_READ",
) -> RetainedInventory:
    return RetainedInventory(
        provider="openai_ads",
        account_ref=ACCOUNT,
        observed_at=OBSERVED,
        completeness=completeness,  # type: ignore[arg-type]
        records=records,
    )


def _sections(
    *,
    report: NormalizedReport | None = None,
    evidence: ProviderAuditEvidence | None = None,
) -> ProviderAuditSections:
    return evaluate(
        ProviderAuditContext(
            provider="openai_ads",
            report=report if report is not None else _report(),
            source_record_digest=REPORT_DIGEST,
            policy=AuditPolicy(
                maximum_observation_age_seconds=604_800, require_conversion_evidence=False
            ),
            as_of=AS_OF,
            evidence=evidence if evidence is not None else ProviderAuditEvidence(),
        )
    )


def _finding_by_id(sections: ProviderAuditSections, finding_id: str) -> Any:
    for item in sections.findings:
        if item.finding.finding_id == finding_id:
            return item
    raise AssertionError("recommendation names a finding this audit did not produce")


# --- Finding 1: the shared transport can raise an unsanitized exception -------


def test_a_hostile_retry_after_header_still_fails_as_a_sanitized_adhelm_error() -> None:
    """`transport.py:26` accepts `str.isdigit()` and then calls `int()` on it.

    Those two predicates are not the same set.  `U+00B2` (SUPERSCRIPT TWO) is
    byte `0xB2`, which is legal in an HTTP header field value and which httpx
    decodes as latin-1, so a provider or an intermediary can put it in
    `Retry-After`.  `"²".isdigit()` is `True` and `int("²")` raises
    `ValueError`.

    `_retry_after_seconds` is called at `transport.py:182` for *every*
    response, before the status is classified, so this reaches a 200 as easily
    as a 429.  The resulting `ValueError` is not an `AdhelmError`, so
    `SafeHttpClient.request` does not fail closed and `adhelm.cli.guarded`
    (which catches only `AdhelmError`, `ValidationError` and `OSError`) does not
    convert it: the CLI exits 1 with a Python traceback whose text contains the
    provider-controlled header value.  That breaks the sanitized diagnostic
    contract (AGENTS.md rules 15 and 20) for *every* provider that uses this
    transport, not only OpenAI Ads.

    Expected: an unparsable `Retry-After` is treated exactly like an absent one
    -- `retry_after_seconds is None` -- and the request outcome is an
    `AdhelmError` carrying no header text.  Use `str.isascii() and
    str.isdecimal()`, or wrap the parse.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []}, headers=[(b"retry-after", b"\xb2")])

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        {"api.ads.openai.com"},
        sleep=lambda _seconds: None,
    )
    # Either a clean response or a sanitized AdhelmError is acceptable; a raw
    # ValueError escaping the transport is not.
    try:
        response = client.request("GET", "https://api.ads.openai.com/v1/ad_account")
    except AdhelmError:
        return
    assert response.status_code == 200


def test_a_hostile_retry_after_header_on_a_refusal_is_a_sanitized_adhelm_error() -> None:
    """The same defect on the path that actually reads the hint.

    A 429 with `Retry-After: \\xb2` must produce the typed OpenAI rate-limit
    diagnostic with no disclosed hint, never a raw `ValueError`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={}, headers=[(b"retry-after", b"\xb2")])

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        {"api.ads.openai.com"},
        read_attempts=1,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(AdhelmError) as caught:
        client.request("GET", "https://api.ads.openai.com/v1/ad_account")
    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds is None
    assert "²" not in caught.value.message + caught.value.next_action


# --- Finding 2: `auth openai-key status` reports a stale verification ---------


@dataclass
class MemoryStore:
    """A fake OS credential store."""

    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


PROFILE_ID = "openai-read"
KEY_REF = "openai-read-key"
NATIVE_ACCOUNT = "account_fixture"
GENERIC_SERVICE = "adhelm"


def _key_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    save_configuration(
        directory,
        Configuration(
            profiles=[
                OpenAIAccountKeyProfile.model_validate(
                    {
                        "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
                        "profile_id": PROFILE_ID,
                        "account_ref": NATIVE_ACCOUNT,
                        "purpose": "READ",
                        "currency": "USD",
                        "timezone": "UTC",
                        "credential_source": "OS_STORE",
                        "credential_ref": KEY_REF,
                    }
                )
            ],
            output=OutputPreferences(max_output_bytes=16_384),
        ),
    )
    return directory


def _install_key_fakes(monkeypatch: pytest.MonkeyPatch, store: MemoryStore) -> None:
    @contextmanager
    def no_lock(_reference: str) -> Iterator[None]:
        yield

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": NATIVE_ACCOUNT,
                "name": "Fixture account",
                "url": "https://example.invalid",
                "preview_url": None,
                "status": "active",
                "timezone": "UTC",
                "currency_code": "USD",
                "review": {"status": "approved"},
            },
        )

    monkeypatch.setattr(lifecycle, "_backend", lambda: store)
    monkeypatch.setattr(lifecycle, "credential_mutation_lock", no_lock)
    monkeypatch.setattr(lifecycle, "_prompt_key", lambda: "openai-fixture-new-key")
    monkeypatch.setattr(lifecycle, "_require_interactive", lambda _value: None)
    monkeypatch.setattr(
        lifecycle, "_open_read_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_status_does_not_claim_a_verification_for_a_key_it_never_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verification record is not bound to the key it verified.

    `openai_account_key_lifecycle.py:210` derives `local_state` from two
    independent facts -- "the generic key slot holds something" and "the
    private state slot holds a record" -- and then republishes that record's
    `identity_pseudonym` and `last_verified_at` verbatim.  Nothing in the state
    record identifies *which* key was verified.

    `OpenAIAccountKeyProfile` is not excluded by
    `credential_setup._require_generic_credential_profile` (which excludes only
    Meta bundles), so the generic `adhelm auth remove-local`, `adhelm auth store`
    and `adhelm auth rotate` commands write and delete that same generic slot
    without ever touching the private state slot -- and
    `OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE` explicitly directs the user to
    `adhelm auth remove-local`.

    So after `auth remove-local` + `auth store` of a key for a different ad
    account, `auth openai-key status` reports `local_state: READY`, the *old*
    account's masked identity and the *old* verification time.  The published
    contract for that field (`openai_account_key_cli.py:52`) says it is "a
    salted local label for the ad account the stored key last resolved to";
    here it is a label for an account the stored key was never shown to reach.
    A positive verification claim about an unverified credential is exactly
    what AGENTS.md rule 20 forbids.

    Expected: the state record binds to the key it verified (for example a
    salted fingerprint of the stored value), and `status` reports `UNVERIFIED`
    with no pseudonym and no `last_verified_at` when the stored key is not that
    key.
    """
    store = MemoryStore()
    store.values[(GENERIC_SERVICE, KEY_REF)] = "openai-fixture-old-key"
    _install_key_fakes(monkeypatch, store)
    directory = _key_directory(tmp_path)

    verified = lifecycle.rotate_openai_account_key(directory, PROFILE_ID, interactive=True)

    # `adhelm auth remove-local --profile openai-read`, then `adhelm auth store`
    # with a key issued for some other ad account.  Neither generic command
    # knows about the private OpenAI state slot.
    store.delete_password(GENERIC_SERVICE, KEY_REF)
    store.set_password(GENERIC_SERVICE, KEY_REF, "an-unrelated-key-for-another-account")

    status = lifecycle.openai_account_key_status(directory, PROFILE_ID)
    assert status.key_slot_present is True
    assert status.local_state == "UNVERIFIED"
    assert status.identity_pseudonym is None
    assert status.last_verified_at is None
    assert status.last_verified_api_version is None
    assert verified.identity_pseudonym is not None


# --- Finding 3: a PAUSE recommendation for campaigns that are not active ------


def test_a_pause_recommendation_is_not_produced_for_an_already_paused_campaign() -> None:
    """The PAUSE hypothesis asserts a fact none of its findings establish.

    `audits/openai_ads.py:1626` proposes the only mutation kind this build can
    compile for an existing OpenAI campaign, and its hypothesis opens with "An
    active OpenAI campaign".  Its membership is
    `{OA_SPEND_LIMIT_NOT_RETURNED, OA_SPEND_WITHOUT_IMPRESSIONS,
    OA_TARGETING_NOT_DECLARED}`.

    Only `OA_SPEND_LIMIT_NOT_RETURNED` is guarded by
    `openai_ads_declares_serving_intent`.  `OA_TARGETING_NOT_DECLARED`
    (`audits/openai_ads.py:1291`) fires for every retained campaign whatever its
    declared status, so a retained inventory whose only campaign is *paused*
    produces a PAUSE recommendation whose stated hypothesis is false of every
    finding it rests on -- it proposes pausing something already paused, and it
    tells the reading agent the campaign is active.

    Expected: either the PAUSE proposal is restricted to findings whose
    resources declare the documented serving intent, or the hypothesis stops
    asserting that they do.
    """
    sections = _sections(
        report=_report(rows=[]),
        evidence=ProviderAuditEvidence(
            inventory=_inventory(
                _records(
                    status="paused",
                    campaign_quality=(
                        OPENAI_ADS_TARGETING_NOT_DECLARED,
                        OPENAI_ADS_CONVERSION_SETTINGS_ASSOCIATED,
                    ),
                )
            )
        ),
    )
    pause = [item for item in sections.recommendations if item.operation_kind == "PAUSE"]
    assert pause == []


def test_a_pause_recommendation_only_rests_on_findings_the_named_route_can_act_on() -> None:
    """The PAUSE recommendation names ad-grain findings for a campaign-only route.

    `OA_SPEND_WITHOUT_IMPRESSIONS` is a report-row rule
    (`audits/openai_ads.py:1020`); its `resource_refs` is the row's
    `resource_ref`, which for the fixed AD-grain OpenAI insights read is an
    `openai_ads.ad.*` reference.  The recommendation names
    ``plan openai-pause``, and `pause_workflow.OpenAIPauseRequest` accepts a
    `campaign_id` only -- there is no ad-level pause route in this build.  The
    recommendation therefore points an agent at a route that cannot act on the
    resources it names, while calling them campaigns.

    Expected: every finding a PAUSE recommendation rests on names a resource the
    named route can accept.
    """
    ad_row = _row(
        resource_ref=AD,
        resource_level="AD",
        metric_grain="AD",
        resource_type="OPENAI_ADS_AD",
        impressions=0,
        clicks=0,
        minor_units=500,
    )
    sections = _sections(report=_report(rows=[ad_row]))
    pause = [item for item in sections.recommendations if item.operation_kind == "PAUSE"]
    named: list[str] = []
    for recommendation in pause:
        for finding_id in recommendation.finding_ids:
            named.extend(_finding_by_id(sections, finding_id).finding.resource_refs)
    assert [ref for ref in named if not ref.startswith("openai_ads.campaign.")] == []


# --- Finding 4: an absence claim with no proven read boundary -----------------


def test_a_local_binding_absence_is_not_claimed_without_a_retained_inventory() -> None:
    """`evaluate` reports a resource "absent from the retained inventory" when there is none.

    `audits/openai_ads.py:1740` adds an `elif context.evidence.creative_catalog
    is not None:` branch that the LinkedIn precedent
    (`audits/linkedin_ads.py:1345`) deliberately does not have.  With no
    retained inventory at all, `_ad_findings` still walks the catalog, finds an
    empty `retained` map, and emits `OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE`
    at `HIGH` confidence with the observation "A local creative version declares
    an OpenAI ad reference that is absent from the retained inventory for this
    account."

    Nothing was read, so nothing is absent.  This is the exact failure mode this
    package's own `InventoryCompleteness` documentation
    (`audits/__init__.py:74`) names: a record set with no proven read boundary
    "can fabricate both deletions and creations", so absence rules are
    suppressed rather than reported at low confidence.  `_absence_is_evidence`
    already exists in this module and is applied to drift; it is not applied
    here.

    Expected: with no retained inventory the rule does not fire at all.
    """
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:one",
                record_digest=_digest(11),
                provider="openai_ads",
                provider_resource_ref=AD,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    sections = _sections(evidence=ProviderAuditEvidence(creative_catalog=catalog))
    fired = {item.rule_id for item in sections.findings}
    assert "OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in fired


def test_a_local_binding_absence_is_suppressed_when_the_read_boundary_is_unproven() -> None:
    """The same absence rule ignores `READ_BOUNDARY_UNVERIFIED`.

    `_drift` suppresses absence findings entirely for a `READ_BOUNDARY_UNVERIFIED`
    record set, and `_structure_findings` drops its absence rules to `LOW`
    confidence for any non-complete read.  The catalog absence rule at
    `audits/openai_ads.py:1155` does neither: it reports at the rule's declared
    `HIGH` confidence against a record set whose omissions are documented to
    carry no information.

    Expected: the rule is suppressed for `READ_BOUNDARY_UNVERIFIED` and, at
    most, reported at reduced confidence for a declared subset.
    """
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:two",
                record_digest=_digest(12),
                provider="openai_ads",
                provider_resource_ref="openai_ads.ad.ad_missing",
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_records(), completeness="READ_BOUNDARY_UNVERIFIED"),
            creative_catalog=catalog,
        )
    )
    matched = [
        item
        for item in sections.findings
        if item.rule_id == "OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"
    ]
    assert matched == []


# --- Finding 5: the typed feature-gate outcome is unreachable in the product --


def test_the_account_enabled_conversion_surface_is_read_through_its_typed_outcome() -> None:
    """`read_event_settings` is never called by the product.

    `providers/openai_conversions.py:332` adds `read_event_settings`, which
    classifies a `403`/`404` from the account-enabled conversion surface as the
    typed `NOT_ENABLED_FOR_ACCOUNT` fact rather than a credential failure, and
    `OpenAIAdsConversionSettingsAvailability` exists only to carry that outcome.

    The one production caller,
    `openai_measurement_workflow.py:229`, still calls `list_event_settings`.
    A `403` on that fixed route is therefore classified by
    `classify_openai_ads_transport_error` into
    `OPENAI_ADS_ACCESS_NOT_PERMITTED` with exit code 4 -- an authorization
    diagnostic telling the customer to check the key's access -- for what the
    ticket requires be reported as a feature fact about the ad account.  The
    typed outcome is exercised by tests only, which is the shape AGENTS.md rule
    20 forbids: a declared capability that is not evidenced on the path a
    customer reaches.

    Expected: the workflow reads the surface through `read_event_settings`.
    """
    import inspect

    from adhelm import openai_measurement_workflow

    source = inspect.getsource(openai_measurement_workflow)
    assert "read_event_settings" in source


# --- Finding 6: two declined rows share one `BoundExceeded` subject -----------


def test_each_bound_exceeded_row_has_a_distinct_subject() -> None:
    """`_bound`'s stated identification invariant does not hold for OpenAI.

    The comment at `audits/openai_ads.py:1568` -- copied from LinkedIn, where it
    is true -- says "The reason alone repeats across declined groups; naming the
    considered operation identifies the exact declined row."  OpenAI declines
    two *different* groups with the same `(reason, considered_operation_kind)`
    pair: the childless-structure group (`audits/openai_ads.py:1668`) and the
    conversion-bidding group (`audits/openai_ads.py:1675`) both use
    `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` with `considered="CREATE"`.

    When both exceed `MAXIMUM_INLINE_FINDING_IDS`, the audit emits two
    `BoundExceeded` rows with an identical `scope` and `subject`, so an agent
    paging the output cannot tell which declined row each one qualifies.

    Expected: the subject identifies the declined row uniquely (for example by
    including the rule identifiers, as the recommendation scope already does).
    """
    records: list[RetainedSnapshotRecord] = [
        _record(seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT)
    ]
    for index in range(40):
        records.append(
            _record(
                seed=100 + index,
                resource_type="CAMPAIGN",
                resource_ref=f"openai_ads.campaign.c{index}",
                parents=(ACCOUNT,),
                state={
                    "status": "active",
                    "currency": "USD",
                    "bid_strategy": "conversions",
                    "openai_ads_lifetime_spend_limit_micros": "1000000",
                    "schedule_end": "2026-09-30T00:00:00+00:00",
                },
                quality=HEALTHY_CAMPAIGN_QUALITY,
            )
        )
    sections = _sections(
        report=_report(rows=[]),
        evidence=ProviderAuditEvidence(inventory=_inventory(tuple(records))),
    )
    subjects = [(item.scope, item.subject) for item in sections.bound_exceeded]
    assert len(subjects) > 1
    assert len(set(subjects)) == len(subjects)


# --- Finding 7: one read presence fact is stated in neither direction ---------


def test_every_read_creative_presence_fact_is_recorded_in_both_directions() -> None:
    """`creative_image_url_present` is parsed and then dropped.

    `providers/openai_ads.py:477` reads and retains
    `creative_image_url_present`, but `openai_ads_ad_quality_statuses`
    (`providers/openai_ads.py:571`) emits nothing for it, there is no
    `OPENAI_ADS_CREATIVE_IMAGE_URL_PRESENT`/`_ABSENT` pair, and
    `OPENAI_ADS_READ_SURFACES` has no entry for it.  The declared invariant
    directly above those constants (`providers/openai_ads.py:525`) is "Every
    fact is stated in both directions so that an absent status never has to be
    read as either 'present' or 'absent'", and `OA_CREATIVE_COPY_INCOMPLETE`
    considers only title and body.

    Expected: either the flag is surfaced in both directions like every other
    presence fact, or it is not read into the model at all.
    """
    absent = OpenAIAdsAd(
        account_ref=ACCOUNT,
        campaign_ref=CAMPAIGN,
        ad_group_ref=AD_GROUP,
        ad_ref=AD,
        ad_client_ref="ad_1",
        campaign_client_ref="camp_1",
        ad_group_client_ref="grp_1",
        status="active",
        review_status="approved",
        creative_type="chat_card",
        creative_file_client_ref="file_1",
        creative_title_present=True,
        creative_body_present=True,
        creative_target_url_present=True,
        creative_image_url_present=False,
    )
    present = absent.model_copy(update={"creative_image_url_present": True})
    assert openai_ads_ad_quality_statuses(absent) != openai_ads_ad_quality_statuses(present)


# --- Finding 8: the capability-matrix row names a recommendation kind that ----
# --- is never recommended, and omits the one that always is ------------------


_REPOSITORY = Path(__file__).resolve().parents[1]


def _capability_matrix_row() -> str:
    text = (_REPOSITORY / "docs" / "capability-matrix.md").read_text(encoding="utf-8")
    rows = [
        line for line in text.splitlines() if line.startswith("| OpenAI offline provider audit")
    ]
    assert len(rows) == 1, "exactly one OpenAI offline provider audit row"
    return rows[0]


def test_the_capability_matrix_names_the_recommendation_kinds_the_audit_emits() -> None:
    """`docs/capability-matrix.md:94` restates a compilability set as a recommendation set.

    The row says the audit produces "non-writing recommendations bounded to
    ``CREATE``/``PAUSE``".  `_recommend` never proposes `CREATE`: its four
    `_propose` calls are three `OBSERVE` (`audits/openai_ads.py:1583`, `:1595`,
    `:1611`) and one `PAUSE` (`:1630`).  `CREATE` appears only as a *declined*
    `considered_operation_kind` on two `_no_recommendation` rows (`:1676`,
    `:1682`).

    The phrase is `COMPILABLE_OPERATION_KINDS["openai_ads"]`
    (`audits/__init__.py:133`), which is what the product could compile, not
    what the audit recommends.  So the row advertises a recommendation kind that
    cannot be produced and omits `OBSERVE`, the kind every OpenAI audit with any
    finding actually emits.  `docs/implementation/openai-audit-recommendations.md:172`
    gets this right, so the matrix contradicts the document it cites.

    Expected: the row names `OBSERVE` and `PAUSE`, or stops enumerating kinds.
    """
    row = _capability_matrix_row()
    named = {kind for kind in ("OBSERVE", "CREATE", "PAUSE") if f"`{kind}`" in row}
    assert named == {"OBSERVE", "PAUSE"}


# --- Finding 9: an unmarked assertion of documented OpenAI behaviour ---------


def test_the_account_enabled_feature_claim_carries_its_unverified_marker() -> None:
    """The lifecycle doc asserts OpenAI documentation it also records as ABSENT.

    `docs/implementation/openai-account-key-lifecycle.md:141` justifies the
    403/404 feature classification with "because OpenAI documents conversion
    setup and pixel behaviour as account-enabled features" -- an unmarked,
    uncited claim about what OpenAI documents.  Line 29 of the same file says
    the opposite for exactly those statuses: "`429` semantics, `Retry-After`, or
    the meaning of `401`/`403`/`404` | **ABSENT** -- the classification below is
    ordinary HTTP behaviour, not documented OpenAI behaviour, and is recorded as
    `UNVERIFIED`".

    `docs/research/openai-clean-room-reference.md` agrees: the meaning of a
    `401`, `403` or `404` is listed among the facts "Absent from every page
    reviewed on 2026-09-09".  The same unmarked claim is repeated in shipped
    code at `providers/openai_conversions.py:200`.  Line 145 then reasons about
    "an expired key" although line 27 of the same table records key expiry as
    `ABSENT from every reviewed page`.

    Expected: the paragraph carries an explicit `UNVERIFIED` marker like every
    other unreviewed OpenAI behaviour in this document.
    """
    text = (_REPOSITORY / "docs" / "implementation" / "openai-account-key-lifecycle.md").read_text(
        encoding="utf-8"
    )
    marker = "OpenAI\ndocuments conversion setup and pixel behaviour as account-enabled features"
    assert marker in text, "the claim under review moved; re-locate it before editing this test"
    start = text.index(marker)
    paragraph = text[text.rindex("\n\n", 0, start) : text.index("\n\n", start)]
    assert "UNVERIFIED" in paragraph


# --- Finding 10: every command result asserts an unverified key lifetime -----


def test_the_shipped_revocation_guidance_asserts_no_unverified_key_lifetime() -> None:
    """`OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE` claims the key does not expire.

    This constant (`openai_account_key_lifecycle.py:83`) is repeated verbatim in
    every `auth openai-key status` and `auth openai-key rotate` result, so it is
    shipped output, not commentary.  It reads in part: "the key stays valid at
    OpenAI until the provider retires it".

    That is a positive assertion about provider-side credential lifetime.  The
    `UNVERIFIED:` marker earlier in the same string is scoped to the preceding
    sentence -- "no reviewed OpenAI Ads page documents a revocation screen or a
    programmatic revocation route" -- and does not cover it.  The feature's own
    research table records the opposite state of knowledge
    (`docs/implementation/openai-account-key-lifecycle.md:27`: "A key expiry, a
    rotation endpoint, or a programmatic revocation route | **ABSENT** from
    every reviewed page"), and `docs/research/openai-clean-room-reference.md`
    lists "any key expiry" among the facts absent from every reviewed page.

    Telling every caller that a locally removed key remains valid at the
    provider is exactly the kind of unevidenced provider claim AGENTS.md rule 20
    forbids -- and it errs toward reassurance, which is the wrong direction for
    a credential.

    Expected: the sentence states that this product cannot determine whether the
    key remains valid at OpenAI, or carries its own `UNVERIFIED` marker.
    """
    from adhelm.openai_account_key_lifecycle import OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE

    assert "the key stays valid at OpenAI" not in OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE
