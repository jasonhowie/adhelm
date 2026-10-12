"""Offline vectors and adversarial tests for the provider-neutral approval kinds.

Every key here is ephemeral and in-memory. Nothing in this module establishes an
installed host, enrollment, provider access, or an execution capability.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from generate_composite_execution_plan_approval_vectors import (
    _CHALLENGE_EXPIRES,
    _NOW,
    _RECEIPT_ISSUED_AT,
    _STARTED_RECORD_DIGEST,
    FIXTURE,
    build,
    composite_plan,
    configuration,
)
from test_approval_protocol import _config as _protocol_config
from test_approval_protocol import _private_key, _public_x963, _signed_receipt
from test_approval_protocol_ultra_review import _MATERIAL as _SWIFT_MATERIAL
from test_ga4_approval_protocol import _SWIFT_INTEROPERABILITY_VECTOR

from adhelm._installed_native_approval_bridge import (
    _require_compiled_installed_approval_bridge_ready,
    _require_installed_bridge_kind,
)
from adhelm._native_approval_bridge_protocol import build_native_approval_bridge_request_v1
from adhelm.approval_protocol import (
    COMPOSITE_APPROVAL_KINDS,
    ApprovalChallenge,
    ApprovalChallengeKind,
    approval_review_facts,
    build_attempt_recovery_challenge,
    build_composite_execution_plan_challenge,
    compiler_review_lines,
    composite_subplan_review_lines,
    decode_approval_challenge,
    parse_approval_challenge_wire,
    validate_review_lines,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.safety import Money, OperationKind, attempt_recovery_subject

_GA4_MATERIAL = json.loads(Path(_SWIFT_INTEROPERABILITY_VECTOR).read_bytes())
# Supplied lines never make a spend or authorization claim: those are derived
# from the structured subject and lead the list.
_LINES = ("Provider google ads operation CREATE", "Planned provider requests 1")
_REFUSED_LINES = (
    "Contact marketer@example.test about this plan",
    "Call the account owner on 555-123-4567 first",
    "Use sk-abcdefghijklmnopqrstuvwx to continue",
    "Authorization: Bearer abcdefghijklmnop",
    "Plan " + "a" * 64,
    "Plan " + "A" * 64,
    "See https://example.test/plan for detail",
    " leading space is refused",
    "trailing space is refused ",
    "double  space is refused",
    "control\x01character",
    "‮right to left override",
    "",
    "x" * 121,
)


def _composite(**overrides: object) -> ApprovalChallenge:
    values: dict[str, object] = {
        "grantor_ref": "customer-grantor-1",
        "review_lines": _LINES,
        "expires_at": _CHALLENGE_EXPIRES,
        "configuration": configuration(),
        "nonce": bytes(range(32)),
        "issued_at": _NOW,
        "now": _NOW,
    }
    values.update(overrides)
    plan = values.pop("plan", composite_plan())
    return build_composite_execution_plan_challenge(plan, **values)  # type: ignore[arg-type]


def _recovery(**overrides: object) -> ApprovalChallenge:
    plan = composite_plan()
    values: dict[str, object] = {
        "grantor_ref": "customer-grantor-1",
        "review_lines": _LINES,
        "expires_at": _CHALLENGE_EXPIRES,
        "configuration": configuration(),
        "nonce": bytes(range(32, 64)),
        "issued_at": _NOW,
        "now": _NOW,
    }
    values.update(overrides)
    attempts = values.pop("attempts", ((plan.subplans[1], _STARTED_RECORD_DIGEST),))
    return build_attempt_recovery_challenge(plan, attempts, **values)  # type: ignore[arg-type]


def _rebuilt(challenge: ApprovalChallenge, subject: dict[str, object]) -> ApprovalChallenge:
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    data["subject"] = subject
    return parse_approval_challenge_wire(canonical_bytes(data), configuration=configuration())


def test_checked_in_vectors_regenerate_byte_for_byte() -> None:
    generated = json.dumps(build(), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    assert generated == Path(FIXTURE).read_bytes()


def test_the_vector_binds_the_plan_the_grant_will_bind() -> None:
    material = json.loads(Path(FIXTURE).read_bytes())
    plan = composite_plan()
    composite, recovery = material["vectors"]

    assert composite["challenge"]["subject"]["plan_digest"] == plan.plan_digest
    assert composite["challenge"]["subject"]["subplan_digests"] == list(plan.subplan_digests)
    assert composite["unsigned_receipt"]["plan_digest"] == plan.plan_digest
    assert composite["unsigned_receipt"]["subplan_digests"] == list(plan.subplan_digests)
    # A zero-cap PAUSE is the only NOT_SPEND_CAPABLE entry, and the summary
    # restates the maximum of each remaining currency rather than a total.
    bounds = composite["challenge"]["subject"]["spend_bounds_by_currency"]
    assert bounds == [
        {"currency": "EUR", "maximum_minor_units": 1_000},
        {"currency": "USD", "maximum_minor_units": 25_000},
    ]
    entries = composite["challenge"]["subject"]["subplans"]
    assert [entry["spend_bound"] for entry in entries][-1] == {
        "spend_capability": "NOT_SPEND_CAPABLE"
    }
    assert [entry["expected_child_egress_count"] for entry in entries] == [3, 1, 1, 1]

    subject = attempt_recovery_subject(
        plan, plan.subplans[1], started_record_digest=_STARTED_RECORD_DIGEST
    )
    assert recovery["challenge"]["subject"]["subplan_digests"] == [subject]
    assert recovery["challenge"]["subject"]["plan_digest"] == digest([subject])
    assert recovery["challenge"]["subject"]["source_plan_digest"] == plan.plan_digest
    assert recovery["challenge"]["subject"]["spend_bounds_by_currency"] == []


def test_the_review_facts_carry_no_provider_schema() -> None:
    material = json.loads(Path(FIXTURE).read_bytes())
    facts = material["vectors"][0]["review_facts"]
    assert set(facts) == {
        "approval_kind",
        "challenge_digest",
        "challenge_expires_at",
        "challenge_nonce",
        "composite_execution",
        "grantor_ref",
    }
    assert set(facts["composite_execution"]) == {"binding_kind", "subject", "subject_digest"}
    rendered = json.dumps(facts, sort_keys=True)
    # The declared egress allowlist is a review fact the marketer needs and the
    # ledger already records. The planned resource and account names are not.
    assert "googleads.googleapis.com" in rendered
    for leaked in ("google_ads.stable.1", "google_ads-create", "google_ads.principal"):
        assert leaked not in rendered


@pytest.mark.parametrize("kind", sorted(COMPOSITE_APPROVAL_KINDS))
def test_a_receipt_verifies_and_binds_exactly_its_own_subject(
    kind: ApprovalChallengeKind,
) -> None:
    challenge = (
        _composite() if kind is ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN else _recovery()
    )
    key = _private_key()
    receipt = _signed_receipt(
        challenge, key=key, issued_at=_RECEIPT_ISSUED_AT, configuration=configuration()
    )

    verified = verify_approval_receipt(
        receipt,
        expected_challenge=challenge,
        configuration=configuration(),
        trusted_public_key_x963=_public_x963(key),
        now=_RECEIPT_ISSUED_AT,
    )

    subject = verified.composite_subject
    assert subject is not None and subject.binding_kind is kind
    assert verified.plan_digest == subject.plan_digest
    assert verified.subplan_digests == subject.subplan_digests
    assert verified.grantor_ref == "customer-grantor-1"


def test_a_receipt_of_one_kind_cannot_be_replayed_as_the_sibling_kind() -> None:
    """The signing domain is per kind, which is why these are sibling kinds."""

    composite, recovery = _composite(), _recovery()
    key = _private_key()
    receipt = _signed_receipt(
        composite, key=key, issued_at=_RECEIPT_ISSUED_AT, configuration=configuration()
    )

    with pytest.raises(AdhelmError) as failed:
        verify_approval_receipt(
            receipt,
            expected_challenge=recovery,
            configuration=configuration(),
            trusted_public_key_x963=_public_x963(key),
            now=_RECEIPT_ISSUED_AT,
        )
    assert failed.value.code == "APPROVAL_RECEIPT_BINDING_MISMATCH"


def test_review_lines_tampered_after_issue_break_the_receipt_binding() -> None:
    challenge = _composite()
    key = _private_key()
    receipt = _signed_receipt(
        challenge, key=key, issued_at=_RECEIPT_ISSUED_AT, configuration=configuration()
    )
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    data["subject"]["review_lines"] = [
        *data["subject"]["review_lines"][:-1],
        "Provider google ads operation CREATE and something else entirely",
    ]
    tampered = parse_approval_challenge_wire(canonical_bytes(data), configuration=configuration())

    assert tampered.digest != challenge.digest
    with pytest.raises(AdhelmError) as failed:
        verify_approval_receipt(
            receipt,
            expected_challenge=tampered,
            configuration=configuration(),
            trusted_public_key_x963=_public_x963(key),
            now=_RECEIPT_ISSUED_AT,
        )
    assert failed.value.code == "APPROVAL_RECEIPT_BINDING_MISMATCH"


def test_an_expired_challenge_refuses_a_receipt() -> None:
    challenge = _composite()
    key = _private_key()
    receipt = _signed_receipt(
        challenge, key=key, issued_at=_RECEIPT_ISSUED_AT, configuration=configuration()
    )

    with pytest.raises(AdhelmError) as failed:
        verify_approval_receipt(
            receipt,
            expected_challenge=challenge,
            configuration=configuration(),
            trusted_public_key_x963=_public_x963(key),
            now=_CHALLENGE_EXPIRES,
        )
    assert failed.value.code == "APPROVAL_RECEIPT_EXPIRED"

    short = _composite(expires_at=_NOW + timedelta(seconds=30))
    with pytest.raises(AdhelmError) as decoded:
        decode_approval_challenge(
            short.canonical_bytes,
            configuration=configuration(),
            now=_NOW + timedelta(seconds=30),
        )
    assert decoded.value.code == "APPROVAL_CHALLENGE_EXPIRED"


def test_a_challenge_cannot_outlive_the_plan_it_summarizes() -> None:
    plan = composite_plan()
    with pytest.raises(AdhelmError):
        _composite(expires_at=plan.expires_at + timedelta(seconds=1))


@pytest.mark.parametrize("index", range(len(_REFUSED_LINES)))
def test_a_review_line_outside_the_closed_boundary_is_refused(index: int) -> None:
    line = _REFUSED_LINES[index]
    with pytest.raises(ValueError):
        validate_review_lines([line])
    with pytest.raises(AdhelmError):
        _composite(review_lines=[line])


def test_the_line_count_is_bounded() -> None:
    with pytest.raises(ValueError):
        validate_review_lines([f"Line number {index}" for index in range(25)])
    with pytest.raises(ValueError):
        validate_review_lines([])
    assert len(validate_review_lines([f"Line number {index}" for index in range(24)])) == 24


def test_review_lines_derive_from_a_subplan_and_from_any_compiler_review() -> None:
    plan = composite_plan()
    lines = composite_subplan_review_lines(plan.subplans[0])
    assert lines[0] == "Provider google_ads operation CREATE"
    # The subplan helper makes no spend claim of its own; the authoritative one
    # is derived from the signed per-currency summary.
    assert not [line for line in lines if "incremental spend" in line.lower()]

    derived = compiler_review_lines(
        "reddit_ads",
        {
            "account_ref": "reddit_ads.account.1",
            "maximum_spend": {"minor_units": 2_500, "currency": "USD"},
            "spec_digest": "c" * 64,
            "creative_version_id": "creative:sha256:" + "c" * 64,
            "creative_content_digests": ["c" * 64],
            "activation_required": True,
            "default_value": None,
        },
    )
    assert derived == (
        "Provider reddit_ads compiled candidate review",
        "account ref: reddit_ads.account.1",
        "activation required: true",
        "creative content digests: 1 values",
        "default value: NOT_SUPPLIED",
        "maximum spend: 2500 minor units USD",
    )


def test_a_subplan_digest_list_that_does_not_restate_the_entries_is_refused() -> None:
    challenge = _composite()
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    subject = dict(data["subject"])
    subject["subplan_digests"] = list(reversed(subject["subplan_digests"]))
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, subject)
    swapped = dict(data["subject"])
    swapped["subplan_digests"] = [*swapped["subplan_digests"][:-1], "d" * 64]
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, swapped)


def test_a_spend_summary_that_disagrees_with_a_subplan_is_refused() -> None:
    challenge = _composite()
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    for replacement in (
        [{"currency": "USD", "maximum_minor_units": 25_000}],
        [
            {"currency": "EUR", "maximum_minor_units": 1_000},
            {"currency": "GBP", "maximum_minor_units": 10},
            {"currency": "USD", "maximum_minor_units": 25_000},
        ],
        [
            {"currency": "EUR", "maximum_minor_units": 1_000},
            {"currency": "USD", "maximum_minor_units": 1},
        ],
        [
            {"currency": "USD", "maximum_minor_units": 25_000},
            {"currency": "EUR", "maximum_minor_units": 1_000},
        ],
    ):
        subject = dict(data["subject"])
        subject["spend_bounds_by_currency"] = replacement
        with pytest.raises(AdhelmError):
            _rebuilt(challenge, subject)


def test_a_recovery_subject_cannot_present_a_spend_allowance_or_another_operation() -> None:
    challenge = _recovery()
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    spending = dict(data["subject"])
    spending["subplans"] = [
        {**spending["subplans"][0], "spend_bound": {"currency": "USD", "minor_units": 1}}
    ]
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, spending)
    escalated = dict(data["subject"])
    escalated["authorized_operation"] = OperationKind.CREATE.value
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, escalated)


def test_a_recovery_subject_digest_must_bind_its_exact_attempt_list() -> None:
    challenge = _recovery()
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    subject = dict(data["subject"])
    subject["plan_digest"] = "f" * 64
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, subject)


def test_an_attempt_must_name_a_subplan_of_the_supplied_plan() -> None:
    plan = composite_plan()
    other = plan.subplans[0].model_copy(update={"subplan_id": "unrelated"})
    with pytest.raises(AdhelmError):
        _recovery(attempts=((other, _STARTED_RECORD_DIGEST),))
    with pytest.raises(AdhelmError):
        _recovery(attempts=((plan.subplans[1], "not-a-digest"),))
    with pytest.raises(AdhelmError):
        _recovery(attempts=())


def test_the_subject_is_a_closed_object() -> None:
    challenge = _composite()
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    extra = dict(data["subject"])
    extra["operator_note"] = "please approve"
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, extra)
    missing = {
        key: value for key, value in data["subject"].items() if key != "spend_bounds_by_currency"
    }
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, missing)
    entry_extra = dict(data["subject"])
    entry_extra["subplans"] = [
        {**entry_extra["subplans"][0], "resource_name": "Launch campaign"},
        *entry_extra["subplans"][1:],
    ]
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, entry_extra)


def test_a_composite_subject_never_carries_a_recovery_field() -> None:
    challenge = _composite()
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    smuggled = dict(data["subject"])
    smuggled["authorized_operation"] = OperationKind.RECOVER.value
    smuggled["source_plan_digest"] = "a" * 64
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, smuggled)


def test_the_resource_reference_is_a_pseudonym_rather_than_the_planned_name() -> None:
    plan = composite_plan()
    challenge = _composite()
    subject = challenge.composite_subject
    assert subject is not None
    for entry, subplan in zip(subject.subplans, plan.subplans, strict=True):
        assert entry.resource_ref.startswith("opaque:sha256:")
        assert subplan.subplan_id not in entry.resource_ref
        assert subplan.account.stable_account_id not in entry.resource_ref


def test_a_bigger_composite_than_the_configured_bound_is_refused() -> None:
    plan = composite_plan()
    subplans = list(plan.subplans)
    for index in range(3):
        subplans.append(
            subplans[0].model_copy(
                update={
                    "subplan_id": f"extra-{index}",
                    "provider": f"extra_provider_{index}",
                    "max_spend": Money(minor_units=1, currency="USD"),
                }
            )
        )
    with pytest.raises(AdhelmError):
        _composite(plan=plan.model_copy(update={"subplans": tuple(subplans)}))


def test_the_v1_bridge_codec_still_refuses_the_new_kinds() -> None:
    """Adding the Python kind adds no bridge bytes and no installed capability."""

    for challenge in (_composite(), _recovery()):
        with pytest.raises(AdhelmError) as failed:
            build_native_approval_bridge_request_v1(
                challenge,
                configuration=configuration(),
                request_id=bytes(range(32)),
                source_context_digest="a" * 64,
                expected_metadata_digest="b" * 64,
            )
        assert failed.value.code == "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID"


@pytest.mark.parametrize("kind", sorted(COMPOSITE_APPROVAL_KINDS))
def test_the_installed_bridge_refuses_the_new_kinds_before_any_credential(
    kind: ApprovalChallengeKind,
) -> None:
    with pytest.raises(AdhelmError) as failed:
        _require_installed_bridge_kind(kind)
    assert failed.value.code == "NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED"
    assert failed.value.exit_code == ExitCode.PRECONDITION
    for supported in (
        ApprovalChallengeKind.OPENAI_PAUSE_PLAN,
        ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN,
    ):
        assert _require_installed_bridge_kind(supported) is supported


def test_no_compiled_binding_is_installed_for_any_kind() -> None:
    with pytest.raises(AdhelmError) as failed:
        _require_compiled_installed_approval_bridge_ready()
    assert failed.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


@pytest.mark.parametrize(
    "vector",
    [
        *_SWIFT_MATERIAL["vectors"],
        _GA4_MATERIAL,
    ],
    ids=lambda vector: str(vector["fixture_name"]),
)
def test_every_pre_adr_0012_receipt_vector_still_verifies_unchanged(vector: dict) -> None:
    """Adding two kinds and retiring the time-zone pair changed no earlier byte.

    The checked-in Swift receipts were signed before this ticket. They still
    reconstruct the same challenge digest, the same review facts, and the same
    verified receipt digest, so no existing kind's wire form moved.
    """

    key_hex = vector.get("public_key_x963_hex", _SWIFT_MATERIAL["public_key_x963_hex"])
    challenge = parse_approval_challenge_wire(
        canonical_bytes(vector["challenge"]), configuration=_protocol_config()
    )
    facts = approval_review_facts(challenge, configuration=_protocol_config())
    assert facts.canonical_bytes == canonical_bytes(vector["review_facts"])
    assert facts.digest == vector["review_digest"]

    verified = verify_approval_receipt(
        canonical_bytes(vector["receipt"]),
        expected_challenge=challenge,
        configuration=_protocol_config(),
        trusted_public_key_x963=bytes.fromhex(str(key_hex)),
        now=datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC),
    )

    assert verified.digest == vector["receipt_digest"]
    assert verified.review_digest == facts.digest
    assert verified.challenge_digest == challenge.digest
    assert verified.composite_subject is None


def test_the_spend_statement_a_person_reads_is_derived_from_the_signed_summary() -> None:
    """A rendered spend claim cannot drift from the structured per-currency cap."""

    challenge = _composite()
    subject = challenge.composite_subject
    assert subject is not None
    assert subject.review_lines[: len(_LINES) + 2] == (
        "Maximum incremental spend 1000 minor units EUR",
        "Maximum incremental spend 25000 minor units USD",
        *_LINES,
    )

    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    for replacement in (
        # A derived line silently restated with a smaller cap.
        [
            "Maximum incremental spend 1000 minor units EUR",
            "Maximum incremental spend 1 minor units USD",
            *_LINES,
        ],
        # A derived line dropped.
        ["Maximum incremental spend 25000 minor units USD", *_LINES],
        # The derived lines reordered.
        [
            "Maximum incremental spend 25000 minor units USD",
            "Maximum incremental spend 1000 minor units EUR",
            *_LINES,
        ],
        # A second, contradicting spend claim appended after the derived lines.
        [
            "Maximum incremental spend 1000 minor units EUR",
            "Maximum incremental spend 25000 minor units USD",
            *_LINES,
            "Maximum incremental spend 0 minor units USD",
        ],
    ):
        subject_data = dict(data["subject"])
        subject_data["review_lines"] = replacement
        with pytest.raises(AdhelmError):
            _rebuilt(challenge, subject_data)


def test_a_supplied_line_cannot_make_a_reserved_claim() -> None:
    for line in (
        "Maximum incremental spend 999 minor units USD",
        "maximum INCREMENTAL SPEND is unbounded",
        "Authorized operation CREATE",
    ):
        with pytest.raises(ValueError):
            validate_review_lines([line])
        with pytest.raises(AdhelmError):
            _composite(review_lines=[line])


def test_a_recovery_review_leads_with_the_authorized_operation() -> None:
    """The review must not read like an execution approval for the attempt."""

    subject = _recovery().composite_subject
    assert subject is not None
    assert subject.review_lines[:3] == (
        "Authorized operation RECOVER (attempt of CREATE)",
        "This records the observed state and applies no provider change",
        "Maximum incremental spend NOT_SPEND_CAPABLE",
    )
    assert subject.authorized_operation is OperationKind.RECOVER
    assert subject.operations == (OperationKind.CREATE,)


def test_a_zero_cap_non_spend_operation_has_one_canonical_encoding() -> None:
    """Python and a native port must not disagree on the bytes for one plan."""

    challenge = _composite()
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    entries = data["subject"]["subplans"]
    assert entries[3]["operation"] == "PAUSE"
    subject = dict(data["subject"])
    subject["subplans"] = [
        *entries[:3],
        {**entries[3], "spend_bound": {"currency": "USD", "minor_units": 0}},
    ]
    with pytest.raises(AdhelmError):
        _rebuilt(challenge, subject)


def test_an_unrenderable_review_field_is_refused_rather_than_dropped() -> None:
    """A future nested compiler field must fail loudly, not vanish silently."""

    with pytest.raises(ValueError):
        compiler_review_lines(
            "google_ads",
            {"account_ref": "google_ads.account.1", "targeting": {"locations": ["US"]}},
        )
    with pytest.raises(ValueError):
        compiler_review_lines("google_ads", {"budget": {"amount": 1, "currency": "USD"}})


def test_a_recovery_subject_names_its_source_plan_expiry_by_a_different_field() -> None:
    """The field name differs because the expiry rule differs."""

    recovery = parse_json(_recovery().canonical_bytes)
    composite = parse_json(_composite().canonical_bytes)
    assert isinstance(recovery, dict) and isinstance(composite, dict)
    assert "expires_at" not in recovery["subject"]
    assert recovery["subject"]["source_plan_expires_at"] == composite["subject"]["expires_at"]
    assert "source_plan_expires_at" not in composite["subject"]


def test_a_recovery_approval_is_still_bounded_by_the_challenge_lifetime() -> None:
    """Outliving the plan is permitted; an unbounded review window is not."""

    plan = composite_plan()
    discovered = plan.expires_at + timedelta(days=1)
    lifetime = configuration().maximum_challenge_lifetime_seconds
    with pytest.raises(AdhelmError):
        _recovery(
            expires_at=discovered + timedelta(seconds=lifetime + 1),
            issued_at=discovered,
            now=discovered,
        )
