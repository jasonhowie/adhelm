"""Fixture-only coverage for the generic retained execution plan.

Nothing here reaches a provider, a credential store, a network socket, or an
approval host.  Every plan is built in memory from fixed values, so the pinned
digests below are stable and a Swift or future Python port can cross-check
them.  A retained plan is local history and never an execution authority.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest

from adhelm.approval_protocol import (
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    composite_subplan_review_lines,
    expected_composite_approval_subject,
)
from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_plan_history import validate_execution_plan_history
from adhelm.execution_plan_records import (
    ExecutionPlanCurrencyBound,
    ExecutionPlanRecord,
    ExecutionPlanSourceContinuity,
    derived_execution_review_lines,
    execution_plan_identity_fingerprint,
    execution_plan_record,
    execution_plan_spend_bounds,
    revalidate_execution_plan_record,
)
from adhelm.execution_plan_views import review_execution_plan
from adhelm.execution_planning_session import (
    CONFIGURED_EXECUTION_PLANNERS,
    BoundedExecutionRead,
    CompiledExecutionCandidate,
    build_execution_plan_record,
    compilable_execution_operations,
    compile_execution_plan,
    plan_execution,
    register_execution_planner,
    retain_execution_plan,
)
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.safety import (
    AccountIdentity,
    CompositePlan,
    EgressTarget,
    Money,
    OperationKind,
    StateRef,
    SubPlan,
)
from adhelm.saved_execution_plans import (
    execution_plan_catalog,
    load_execution_plan,
    load_execution_plan_record,
    retain_execution_plan_record,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_EXPIRES = _T0 + timedelta(hours=1)
_KEY = b"\x11" * 32

#: Pinned goldens for the fixed fixture below.  A change to the record shape,
#: the plan serialization, or the review derivation must land here explicitly.
_GOLDEN_PAUSE_PLAN_DIGEST = "a9cd5006c7b98e1519f8cb495c5fb22ccf46e7dac75c54840576eafca9c854ec"
_GOLDEN_PAUSE_SUBPLAN_DIGEST = "64b5ba29e00278a38bca594f421faa2af1e781703e2995fbfc0809a321c0db18"
_GOLDEN_IDENTITY_FINGERPRINT = "041b1a0ea98eea29ae4b56c1398975def8146172bdc44ef402942ef2617dc8ea"

_COMPILER_REVIEW = {
    "account_currency": "USD",
    "campaign_duration_days": 31,
    "campaign_total_budget": {"minor_units": 25_000, "currency": "USD"},
    "creative_content_digest": "e" * 64,
    "initial_state": "PAUSED",
    "objective": "SEARCH_TRAFFIC",
}


def approval_configuration() -> ApprovalHostConfiguration:
    """A verifier configuration used only to rebuild an S1 subject in tests."""
    return ApprovalHostConfiguration(
        host_ref="host:adhelm-macos-approval-v1", signing_key_ref="key:approval-test-v1"
    )


def account(provider: str = "google_ads") -> AccountIdentity:
    return AccountIdentity(
        account_ref=f"{provider}.alias",
        stable_account_id=f"{provider}.stable.1",
        auth_profile_ref=f"{provider}.edit-profile",
        principal_ref=f"{provider}.principal",
        client_application_ref=f"{provider}.client",
        scopes=("ads.write",),
    )


def read(**overrides: object) -> BoundedExecutionRead:
    values: dict[str, object] = {
        "observed_state": StateRef(fingerprint="a" * 64, revision="r1"),
        "read_started_at": _T0,
        "read_finished_at": _T0 + timedelta(seconds=2),
        "connector_version": "0.1.0.dev0",
    }
    values.update(overrides)
    return BoundedExecutionRead(**values)  # type: ignore[arg-type]


def candidate(**overrides: object) -> CompiledExecutionCandidate:
    values: dict[str, object] = {
        "provider": "google_ads",
        "operation": OperationKind.PAUSE,
        "subplan_id": "google-ads-pause",
        "account": account(),
        "provider_api_version": "v25",
        "capability_version": "cap-v1",
        "after": StateRef(fingerprint="b" * 64, revision="r2"),
        "max_spend": Money(minor_units=0, currency="USD"),
        "egress": (
            EgressTarget(host="googleads.googleapis.com", purpose="fixed v25 campaign pause"),
        ),
        "recovery_actions": (OperationKind.PAUSE,),
    }
    values.update(overrides)
    return CompiledExecutionCandidate(**values)  # type: ignore[arg-type]


def plan_for(item: CompiledExecutionCandidate, source: BoundedExecutionRead) -> CompositePlan:
    return compile_execution_plan(
        item,
        plan_id="plan-1",
        profile_ref="default",
        expires_at=_EXPIRES,
        read=source,
    )


def record_for(
    item: CompiledExecutionCandidate | None = None,
    source: BoundedExecutionRead | None = None,
    **overrides: object,
) -> ExecutionPlanRecord:
    selected = candidate() if item is None else item
    origin = read() if source is None else source
    return build_execution_plan_record(
        plan_for(selected, origin),
        candidate=selected,
        read=origin,
        **overrides,  # type: ignore[arg-type]
    )


def create_candidate() -> CompiledExecutionCandidate:
    return candidate(
        operation=OperationKind.CREATE,
        subplan_id="google-ads-create",
        max_spend=Money(minor_units=25_000, currency="USD"),
        redacted_review=_COMPILER_REVIEW,
        egress=(
            EgressTarget(host="googleads.googleapis.com", purpose="fixed v25 campaign create"),
        ),
        recovery_actions=(OperationKind.PAUSE,),
        expected_child_egress_count=3,
    )


# --------------------------------------------------------------------------
# Record shape, digests, and the S1 review derivation
# --------------------------------------------------------------------------


def test_record_round_trips_and_pins_its_digests() -> None:
    record = record_for()

    assert record.plan_digest == _GOLDEN_PAUSE_PLAN_DIGEST
    assert record.subplan_digests == (_GOLDEN_PAUSE_SUBPLAN_DIGEST,)
    assert record.source_continuity.identity_fingerprint == _GOLDEN_IDENTITY_FINGERPRINT
    assert record.source_continuity.identity_fingerprint == execution_plan_identity_fingerprint(
        record.plan.subplans[0]
    )
    reloaded = revalidate_execution_plan_record(record.model_dump(mode="json"))
    assert reloaded == record
    assert canonical_bytes(reloaded.model_dump(mode="json")) == canonical_bytes(
        record.model_dump(mode="json")
    )
    assert record.execution_authority is False
    assert record.requires_current_kernel_checks is True
    assert record.provenance.live_validated is False


def test_review_lines_equal_the_s1_derivation_byte_for_byte() -> None:
    """The retained review is exactly what the approval host will render."""
    for item in (candidate(), create_candidate()):
        source = read()
        plan = plan_for(item, source)
        record = build_execution_plan_record(plan, candidate=item, read=source)
        subject = expected_composite_approval_subject(
            plan,
            kind=ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN,
            grantor_ref="customer-grantor-1",
            review_lines=record.review_lines,
            configuration=approval_configuration(),
        )

        assert subject.review_lines == record.review_lines
        assert canonical_bytes(list(subject.review_lines)) == canonical_bytes(
            list(record.review_lines)
        )
        assert [
            {"currency": bound.currency, "maximum_minor_units": bound.maximum_minor_units}
            for bound in subject.spend_bounds_by_currency
        ] == [bound.model_dump(mode="json") for bound in record.spend_bounds_by_currency]


def test_derived_spend_lines_and_bounds_match_the_approval_subject() -> None:
    pause = record_for()
    create = record_for(create_candidate())

    assert pause.spend_bounds_by_currency == ()
    assert pause.review_lines[0] == "Maximum incremental spend NOT_SPEND_CAPABLE"
    assert create.spend_bounds_by_currency == (
        ExecutionPlanCurrencyBound(currency="USD", maximum_minor_units=25_000),
    )
    assert create.review_lines[0] == "Maximum incremental spend 25000 minor units USD"
    assert derived_execution_review_lines(create.spend_bounds_by_currency) == (
        create.review_lines[0],
    )
    assert execution_plan_spend_bounds(create.plan) == create.spend_bounds_by_currency


def test_subplan_intent_review_is_the_shared_helper_unchanged() -> None:
    record = record_for()

    assert record.review_source == "COMPOSITE_SUBPLAN_INTENT"
    assert record.review_lines[1:] == composite_subplan_review_lines(record.plan.subplans[0])


def test_compiler_review_source_is_recorded_and_not_rederived_from_intent() -> None:
    record = record_for(create_candidate())

    assert record.review_source == "COMPILER_CANDIDATE_REVIEW"
    assert "Provider google_ads compiled candidate review" in record.review_lines
    # A digest-shaped compiler field is omitted rather than rendered.
    assert not any("e" * 64 in line for line in record.review_lines)


def test_a_record_cannot_restate_review_lines_the_plan_does_not_produce() -> None:
    record = record_for()
    tampered = record.model_dump(mode="json")
    tampered["review_lines"] = [
        tampered["review_lines"][0],
        "Provider google_ads operation CREATE",
        *tampered["review_lines"][2:],
    ]

    with pytest.raises(AdhelmError) as refused:
        revalidate_execution_plan_record(tampered)

    assert refused.value.code == "INVALID_EXECUTION_PLAN_RECORD"


def test_a_record_cannot_understate_its_own_spend_summary() -> None:
    record = record_for(create_candidate())
    tampered = record.model_dump(mode="json")
    tampered["spend_bounds_by_currency"] = []
    tampered["review_lines"] = ["Maximum incremental spend NOT_SPEND_CAPABLE"] + list(
        record.review_lines[1:]
    )

    with pytest.raises(AdhelmError):
        revalidate_execution_plan_record(tampered)


def test_recover_is_refused_as_a_plan_operation() -> None:
    with pytest.raises(AdhelmError) as refused:
        candidate_with_recover = candidate(operation=OperationKind.RECOVER)
        plan_for(candidate_with_recover, read())

    assert refused.value.code == "INVALID_EXECUTION_PLAN_CANDIDATE"

    record = record_for()
    tampered = record.model_dump(mode="json")
    tampered["operation"] = "RECOVER"
    with pytest.raises(AdhelmError):
        revalidate_execution_plan_record(tampered)


def test_privacy_validator_refuses_a_secret_shaped_review_line() -> None:
    record = record_for()
    tampered = record.model_dump(mode="json")
    tampered["review_source"] = "COMPILER_CANDIDATE_REVIEW"
    tampered["review_lines"] = [
        tampered["review_lines"][0],
        "Contact marketer at operator@example.test before pausing",
    ]

    with pytest.raises(AdhelmError):
        revalidate_execution_plan_record(tampered)


def test_an_incomplete_or_aliased_record_is_refused() -> None:
    record = record_for()
    partial = record.model_dump(mode="json")
    partial.pop("provenance")

    with pytest.raises(AdhelmError) as refused:
        revalidate_execution_plan_record(partial)

    assert refused.value.exit_code == ExitCode.INVALID_INPUT


# --------------------------------------------------------------------------
# Session: injected reads, source continuity, and expiry
# --------------------------------------------------------------------------


def test_the_session_takes_before_from_the_injected_read_only() -> None:
    source = read(observed_state=StateRef(fingerprint="c" * 64, revision="r9"))
    plan = plan_for(candidate(), source)

    assert plan.subplans[0].before == source.observed_state
    assert plan.subplans[0].after == StateRef(fingerprint="b" * 64, revision="r2")


def test_a_plan_with_no_transition_is_refused() -> None:
    observed = StateRef(fingerprint="b" * 64, revision="r2")

    with pytest.raises(AdhelmError) as refused:
        plan_for(candidate(), read(observed_state=observed))

    assert refused.value.code == "EXECUTION_PLAN_NO_TRANSITION"


def test_source_continuity_mismatch_is_refused_before_retention(tmp_path: Path) -> None:
    plan = plan_for(candidate(), read())
    other = read(observed_state=StateRef(fingerprint="d" * 64, revision="r5"))

    with pytest.raises(AdhelmError) as refused:
        build_execution_plan_record(plan, candidate=candidate(), read=other)

    assert refused.value.code == "EXECUTION_PLAN_SOURCE_CONTINUITY_MISMATCH"
    assert execution_plan_catalog(Ledger(tmp_path))["total_count"] == 0


def test_a_retained_record_must_describe_its_own_read() -> None:
    record = record_for()
    tampered = record.model_dump(mode="json")
    tampered["source_continuity"]["observed_state"] = {
        "fingerprint": "d" * 64,
        "revision": "r5",
    }

    with pytest.raises(AdhelmError):
        revalidate_execution_plan_record(tampered)


def test_an_expired_plan_is_refused_before_any_append(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)

    with pytest.raises(AdhelmError) as refused:
        retain_execution_plan(ledger, record_for(), now=_EXPIRES)

    assert refused.value.code == "EXECUTION_PLAN_EXPIRED"
    assert refused.value.exit_code == ExitCode.PRECONDITION
    assert execution_plan_catalog(ledger)["total_count"] == 0


def test_the_session_reloads_and_verifies_the_durable_receipt(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    retained = plan_execution(
        ledger,
        candidate(),
        read=read(),
        plan_id="plan-1",
        profile_ref="default",
        expires_at=_EXPIRES,
        now=_T0 + timedelta(seconds=3),
    )

    reloaded, provenance = load_execution_plan_record(ledger, retained.record_digest)
    assert reloaded == retained.record
    assert provenance["plan_digest"] == _GOLDEN_PAUSE_PLAN_DIGEST
    assert provenance["execution_authority"] is False
    assert provenance["checkpoint_digest"] == retained.checkpoint_digest


def test_a_reload_failure_after_the_append_reports_ambiguous_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.execution_planning_session as session

    ledger = Ledger(tmp_path)
    monkeypatch.setattr(
        session,
        "load_execution_plan_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("reload")),
    )

    with pytest.raises(AdhelmError) as refused:
        retain_execution_plan(ledger, record_for(), now=_T0 + timedelta(seconds=3))

    assert refused.value.code == "EXECUTION_PLAN_RECEIPT_UNVERIFIED"
    assert refused.value.exit_code == ExitCode.AMBIGUOUS
    # The append itself completed; local history is the only truthful source.
    assert execution_plan_catalog(ledger)["total_count"] == 1


# --------------------------------------------------------------------------
# Ledger envelope, predecessor chain, and verified-history selection
# --------------------------------------------------------------------------


def _retain(ledger: Ledger, record: ExecutionPlanRecord) -> str:
    return str(retain_execution_plan_record(ledger, record)["record_digest"])


def test_the_envelope_binds_the_workspace_provider_and_account(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    record = record_for()
    appended = retain_execution_plan_record(ledger, record)

    assert appended["provider"] == "google_ads"
    assert appended["account_ref"] == "google_ads.alias"
    assert appended["run_id"] is None
    assert appended["observed_at"] is None
    assert appended["occurred_at"] is None
    assert ledger.verify()["status"] == "VERIFIED"


def test_a_plan_for_another_workspace_is_refused(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, profile_ref="other")

    with pytest.raises(AdhelmError) as refused:
        retain_execution_plan_record(ledger, record_for())

    assert refused.value.code == "INVALID_EXECUTION_PLAN_PROVENANCE"


def test_a_successor_names_its_exact_predecessor(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    first = _retain(ledger, record_for())
    later = read(
        observed_state=StateRef(fingerprint="c" * 64, revision="r3"),
        read_started_at=_T0 + timedelta(minutes=1),
        read_finished_at=_T0 + timedelta(minutes=1, seconds=2),
    )
    second = _retain(ledger, record_for(candidate(), later, supersedes_record_digest=first))

    catalog = execution_plan_catalog(ledger)
    assert [row["record_digest"] for row in catalog["plans"]] == [second, first]
    assert catalog["plans"][0]["supersedes_record_digest"] == first
    assert ledger.verify()["status"] == "VERIFIED"


def test_two_plans_cannot_supersede_the_same_predecessor(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    first = _retain(ledger, record_for())
    for offset in (1, 2):
        later = read(
            observed_state=StateRef(fingerprint=str(offset) * 64, revision=f"r{offset}"),
            read_started_at=_T0 + timedelta(minutes=offset),
            read_finished_at=_T0 + timedelta(minutes=offset, seconds=2),
        )
        record = record_for(candidate(), later, supersedes_record_digest=first)
        if offset == 1:
            _retain(ledger, record)
            continue
        with pytest.raises(AdhelmError) as refused:
            _retain(ledger, record)

    assert refused.value.code == "EXECUTION_PLAN_PREDECESSOR_CONFLICT"


def test_an_unretained_predecessor_is_refused(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)

    with pytest.raises(AdhelmError) as refused:
        _retain(ledger, record_for(supersedes_record_digest="f" * 64))

    assert refused.value.code == "EXECUTION_PLAN_PREDECESSOR_CONFLICT"


def test_a_predecessor_for_another_provider_is_refused() -> None:
    first = record_for()
    later = read(observed_state=StateRef(fingerprint="c" * 64, revision="r3"))
    other = candidate(
        provider="meta_ads",
        subplan_id="meta-pause",
        account=account("meta_ads"),
        egress=(EgressTarget(host="graph.facebook.com", purpose="fixed v26 campaign pause"),),
    )
    second = record_for(other, later, supersedes_record_digest="1" * 64)

    with pytest.raises(AdhelmError) as refused:
        validate_execution_plan_history(
            [
                {
                    "record_kind": "EXECUTION_PLAN_RETAINED",
                    "record_digest": "1" * 64,
                    "payload": first.model_dump(mode="json"),
                },
                {
                    "record_kind": "EXECUTION_PLAN_RETAINED",
                    "record_digest": "2" * 64,
                    "payload": second.model_dump(mode="json"),
                },
            ]
        )

    assert refused.value.code == "EXECUTION_PLAN_PREDECESSOR_CONFLICT"


def test_selection_refuses_an_unverified_tail(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    selected = _retain(ledger, record_for())
    segment = next((tmp_path / "ledger/changes").glob("*.jsonl"))
    lines = segment.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[-1])
    tampered["payload"]["provenance"]["capability_version"] = "cap-v2"
    lines[-1] = json.dumps(tampered, separators=(",", ":"), sort_keys=True)
    segment.write_bytes(("\n".join(lines) + "\n").encode())

    with pytest.raises(AdhelmError):
        load_execution_plan(ledger, selected)
    with pytest.raises(AdhelmError):
        execution_plan_catalog(ledger)


def test_catalog_filters_and_pagination_are_bounded(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    _retain(ledger, record_for())
    other = candidate(
        provider="meta_ads",
        subplan_id="meta-pause",
        account=account("meta_ads"),
        egress=(EgressTarget(host="graph.facebook.com", purpose="fixed v26 campaign pause"),),
    )
    _retain(ledger, record_for(other))

    assert execution_plan_catalog(ledger)["total_count"] == 2
    assert execution_plan_catalog(ledger, provider="meta_ads")["total_count"] == 1
    assert execution_plan_catalog(ledger, operation="CREATE")["total_count"] == 0
    assert execution_plan_catalog(ledger, account_ref="google_ads.alias")["total_count"] == 1
    page = execution_plan_catalog(ledger, limit=1)
    assert page["next_cursor"] == "1" and len(page["plans"]) == 1
    assert execution_plan_catalog(ledger, cursor=1, limit=1)["next_cursor"] is None
    for arguments in (
        {"cursor": -1},
        {"limit": 0},
        {"limit": 501},
        {"cursor": 99},
        {"provider": "not_a_provider"},
        {"operation": "RECOVER "},
        {"account_ref": "not a ref"},
    ):
        with pytest.raises(AdhelmError) as refused:
            execution_plan_catalog(ledger, **arguments)  # type: ignore[arg-type]
        assert refused.value.code == "INVALID_EXECUTION_PLAN_SELECTION"


def test_an_unretained_digest_is_refused(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)

    with pytest.raises(AdhelmError) as missing:
        load_execution_plan(ledger, "a" * 64)
    assert missing.value.code == "EXECUTION_PLAN_NOT_RETAINED"

    with pytest.raises(AdhelmError) as invalid:
        load_execution_plan(ledger, "not-a-digest")
    assert invalid.value.code == "INVALID_EXECUTION_PLAN_SELECTION"


def test_verify_checkpoint_and_export_accept_the_new_kind(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    selected = _retain(ledger, record_for())

    assert ledger.verify()["status"] == "VERIFIED"
    checkpoint = ledger.checkpoint()
    assert checkpoint["status"] == "VERIFIED_CHECKPOINT"
    kinds = {item["record_kind"] for item in checkpoint["records_by_stream"]["changes"]}
    assert "EXECUTION_PLAN_RETAINED" in kinds

    exported = export_verified(ledger, tmp_path / "export", opaque_key=_KEY)
    assert exported["projection_as_of"]["changes"]["high_watermark"] >= 1
    body = (tmp_path / "export").joinpath("events.csv").read_text(encoding="utf-8")
    assert "EXECUTION_PLAN_RETAINED" in body and selected in body
    nodes = (tmp_path / "export").joinpath("record-nodes.csv").read_text(encoding="utf-8")
    # Every local reference is pseudonymized; no raw workspace, account, or
    # credential reference survives the export boundary.
    assert "google_ads.alias" not in nodes
    assert "google_ads.principal" not in nodes
    assert "google_ads.edit-profile" not in nodes


def test_the_indexed_projection_rebuilds_with_the_new_kind(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    _retain(ledger, record_for())

    rebuilt = ledger.rebuild_index()

    assert rebuilt["checkpoint_digest"] == ledger.checkpoint()["checkpoint_digest"]


# --------------------------------------------------------------------------
# Bounded redacted review
# --------------------------------------------------------------------------


def test_the_review_is_bounded_redacted_and_expiry_aware() -> None:
    record = record_for()

    review = review_execution_plan(record, opaque_key=_KEY, reviewed_at=_T0 + timedelta(minutes=1))

    assert review.expiry_status == "NOT_EXPIRED_AT_REVIEW"
    assert review.execution_authority is False
    assert review.review_authorizes_execution is False
    assert review.requires_current_kernel_checks is True
    assert review.review_lines == record.review_lines
    body = json.dumps(review.model_dump(mode="json"))
    assert "google_ads.alias" not in body
    assert "google_ads.principal" not in body
    assert review.account_ref.startswith("opaque:")

    expired = review_execution_plan(
        record, opaque_key=_KEY, reviewed_at=_EXPIRES + timedelta(seconds=1)
    )
    assert expired.expiry_status == "EXPIRED_AT_REVIEW"
    assert expired.review_digest != review.review_digest


def test_the_review_refuses_an_undersized_output_bound() -> None:
    with pytest.raises(AdhelmError) as refused:
        review_execution_plan(
            record_for(),
            opaque_key=_KEY,
            reviewed_at=_T0 + timedelta(minutes=1),
            max_output_bytes=256,
        )

    assert refused.value.code == "OUTPUT_LIMIT_EXCEEDED"


def test_the_review_requires_a_private_local_sharing_key() -> None:
    with pytest.raises(AdhelmError) as refused:
        review_execution_plan(record_for(), opaque_key=b"short", reviewed_at=_T0)

    assert refused.value.code == "EXPORT_KEY_REQUIRED"


def test_a_multi_provider_composite_is_not_retained_by_this_record() -> None:
    """One retained record binds one provider, because its envelope does."""
    first = plan_for(candidate(), read()).subplans[0]
    second = SubPlan(
        subplan_id="meta-pause",
        provider="meta_ads",
        operation=OperationKind.PAUSE,
        account=account("meta_ads"),
        provider_api_version="v26",
        capability_version="cap-v1",
        before=StateRef(fingerprint="a" * 64, revision="r1"),
        after=StateRef(fingerprint="b" * 64, revision="r2"),
        max_spend=Money(minor_units=0, currency="USD"),
        egress=(EgressTarget(host="graph.facebook.com", purpose="fixed v26 campaign pause"),),
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=_EXPIRES,
    )
    composite = CompositePlan(
        plan_id="plan-2",
        profile_ref="default",
        subplans=(first, second),
        expires_at=_EXPIRES,
    )

    with pytest.raises(AdhelmError):
        execution_plan_record(
            composite,
            review_lines=("Provider google_ads operation PAUSE",),
            review_source="COMPOSITE_SUBPLAN_INTENT",
            source_continuity=ExecutionPlanSourceContinuity(
                identity_fingerprint=digest({"x": 1}),
                observed_state=StateRef(fingerprint="a" * 64, revision="r1"),
                read_started_at=_T0,
                read_finished_at=_T0 + timedelta(seconds=2),
                connector_version="0.1.0.dev0",
            ),
        )


# --------------------------------------------------------------------------
# The retention clock window
# --------------------------------------------------------------------------


def test_a_read_that_finishes_after_retention_is_refused(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    now = _T0 + timedelta(minutes=1)
    ahead = read(
        read_started_at=now + timedelta(minutes=29),
        read_finished_at=now + timedelta(minutes=30),
    )

    with pytest.raises(AdhelmError) as refused:
        retain_execution_plan(ledger, record_for(candidate(), ahead), now=now)

    assert refused.value.code == "CLOCK_INVALID"
    assert refused.value.exit_code == ExitCode.INVALID_INPUT
    assert execution_plan_catalog(ledger)["total_count"] == 0


def test_a_read_older_than_the_plan_lifetime_is_refused(tmp_path: Path) -> None:
    """The plan declares how long its picture is worth authorizing."""
    ledger = Ledger(tmp_path)
    # The plan expires one hour after _T0, so at this instant its remaining
    # lifetime is ten minutes and a read older than ten minutes is refused.
    now = _EXPIRES - timedelta(minutes=10)
    stale = read(
        read_started_at=now - timedelta(minutes=11),
        read_finished_at=now - timedelta(minutes=10, seconds=1),
    )

    with pytest.raises(AdhelmError) as refused:
        retain_execution_plan(ledger, record_for(candidate(), stale), now=now)

    assert refused.value.code == "EXECUTION_PLAN_READ_TOO_STALE"
    assert refused.value.exit_code == ExitCode.PRECONDITION
    assert execution_plan_catalog(ledger)["total_count"] == 0


def test_a_stale_but_in_window_read_is_retained_honestly(tmp_path: Path) -> None:
    """Local retention cannot prove currency, and must not pretend to.

    The kernel's drift check against a fresh read is what refuses a plan whose
    baseline has moved; retention only refuses a read the plan's own declared
    lifetime never covered.
    """
    ledger = Ledger(tmp_path)
    now = _EXPIRES - timedelta(minutes=10)
    stale = read(
        read_started_at=now - timedelta(minutes=10),
        read_finished_at=now - timedelta(minutes=9, seconds=59),
    )

    retained = retain_execution_plan(ledger, record_for(candidate(), stale), now=now)

    assert retained.record.source_continuity.read_finished_at == stale.read_finished_at
    assert retained.record.requires_current_kernel_checks is True
    assert execution_plan_catalog(ledger)["total_count"] == 1


# --------------------------------------------------------------------------
# The configured-planner registry
# --------------------------------------------------------------------------


def test_exactly_the_reviewed_planners_ship_and_the_registry_is_read_only() -> None:
    """P03 registered ``PAUSE``, P04 ``CREATE``, P05 the other two Google
    pairs, P04-OA ``("openai_ads", CREATE)`` and P04-MA
    ``("meta_ads", CREATE)``.

    Every other pair is still empty.  The registry becoming larger is exactly
    the change that must not happen silently, so this pins the exact set.
    Google ``UPDATE`` is the one registered maintenance pair.  ``ARCHIVE`` is
    delete-class and stays unregistered until a cited reference read can supply
    AGENTS rule 11's dependency proof, so ``discover`` never advertises it.
    """

    from adhelm import (
        google_ads_pause_workflow,  # noqa: F401  (its import registers P03)
        plan_cli,  # noqa: F401  (its import registers P04, P04-OA, P04-MA and P05)
    )
    from adhelm.google_ads_activate_execution import GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER
    from adhelm.google_ads_create_execution import GOOGLE_ADS_CREATE_EXECUTION_PLANNER
    from adhelm.google_ads_maintain_execution import GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER
    from adhelm.google_ads_pause_execution import GOOGLE_ADS_PAUSE_EXECUTION_PLANNER
    from adhelm.meta_create_execution import META_CREATE_EXECUTION_PLANNER
    from adhelm.openai_create_execution import OPENAI_CREATE_EXECUTION_PLANNER

    assert dict(CONFIGURED_EXECUTION_PLANNERS) == {
        ("google_ads", OperationKind.CREATE): GOOGLE_ADS_CREATE_EXECUTION_PLANNER,
        ("google_ads", OperationKind.PAUSE): GOOGLE_ADS_PAUSE_EXECUTION_PLANNER,
        ("google_ads", OperationKind.ACTIVATE): GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER,
        ("google_ads", OperationKind.UPDATE): GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER,
        ("meta_ads", OperationKind.CREATE): META_CREATE_EXECUTION_PLANNER,
        ("openai_ads", OperationKind.CREATE): OPENAI_CREATE_EXECUTION_PLANNER,
    }
    assert compilable_execution_operations() == (
        {"provider": "google_ads", "operation": "ACTIVATE"},
        {"provider": "google_ads", "operation": "CREATE"},
        {"provider": "google_ads", "operation": "PAUSE"},
        {"provider": "google_ads", "operation": "UPDATE"},
        {"provider": "meta_ads", "operation": "CREATE"},
        {"provider": "openai_ads", "operation": "CREATE"},
    )
    assert isinstance(CONFIGURED_EXECUTION_PLANNERS, MappingProxyType)
    with pytest.raises(TypeError):
        cast(Any, CONFIGURED_EXECUTION_PLANNERS)["google_ads", OperationKind.DELETE] = object()


def _isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register into a fresh private dict so no global registration leaks."""
    import adhelm.execution_planning_session as module

    private: dict[tuple[str, OperationKind], Any] = {}
    monkeypatch.setattr(module, "_CONFIGURED_EXECUTION_PLANNERS", private)
    monkeypatch.setattr(module, "CONFIGURED_EXECUTION_PLANNERS", MappingProxyType(private))


class _StubPlanner:
    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> Any:
        raise AssertionError("registration tests never plan")


def test_one_operation_has_exactly_one_registered_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated_registry(monkeypatch)
    register_execution_planner("google_ads", OperationKind.PAUSE, _StubPlanner())

    assert compilable_execution_operations() == ({"provider": "google_ads", "operation": "PAUSE"},)
    with pytest.raises(AdhelmError) as refused:
        register_execution_planner("google_ads", OperationKind.PAUSE, _StubPlanner())

    assert refused.value.code == "EXECUTION_PLANNER_ALREADY_REGISTERED"


@pytest.mark.parametrize(
    ("provider", "operation", "planner"),
    [
        ("not_a_provider", OperationKind.PAUSE, _StubPlanner()),
        ("google_ads", OperationKind.RECOVER, _StubPlanner()),
        ("google_ads", OperationKind.PAUSE, object()),
    ],
)
def test_an_invalid_planner_registration_is_refused(
    monkeypatch: pytest.MonkeyPatch, provider: str, operation: OperationKind, planner: object
) -> None:
    _isolated_registry(monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        register_execution_planner(provider, operation, cast(Any, planner))

    assert refused.value.code == "INVALID_EXECUTION_PLANNER_REGISTRATION"
    assert compilable_execution_operations() == ()
