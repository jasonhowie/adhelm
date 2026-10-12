"""Independent adversarial review of the provider-neutral approval kinds (S1).

Written by a reviewer who did not implement the change.  Each test encodes a
behaviour the reviewed contract claims, so a failure here is a claim the code
does not keep.  Every key is ephemeral and in memory; nothing here establishes
an installed host, an enrollment, provider access, or an execution capability.
"""

from __future__ import annotations

import copy
import importlib
from datetime import timedelta

import pytest
from generate_composite_execution_plan_approval_vectors import (
    _NOW,
    _STARTED_RECORD_DIGEST,
    composite_plan,
    configuration,
)
from test_composite_execution_plan_approval import _composite, _rebuilt

from adhelm.approval_protocol import (
    build_attempt_recovery_challenge,
    compiler_review_lines,
)
from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError

_REVIEW_LINES = ("Provider google ads operation CREATE",)


# --- Finding 1 -------------------------------------------------------------


def test_a_spend_capable_operation_cannot_be_summarized_as_not_spend_capable() -> None:
    """``NOT_SPEND_CAPABLE`` is a claim about the operation, not a free choice.

    ``_spend_bound`` only produces it for ``PAUSE``, ``ARCHIVE`` or ``RECOVER``
    with an explicit zero cap; the module comment states that every other
    operation "always shows its integer minor-unit cap, including a zero cap".
    The wire decoder does not enforce that: ``_spend_bound_from_data`` accepts
    ``{"spend_capability": "NOT_SPEND_CAPABLE"}`` under any operation, and
    ``_composite_entry_data`` re-emits it, so the subject round-trips and its
    digest verifies.

    A ``CREATE`` subplan whose real cap is 25 000 minor units can therefore be
    presented to a reviewer as not spend capable, and dropped from
    ``spend_bounds_by_currency`` as well, while every internal restatement check
    still passes.  The decoder is the contract ADR 0012 requires the Swift host
    to mirror and the only internal-consistency gate before a person reviews;
    acceptance catches the divergence from the local plan only afterwards.
    """

    challenge = _composite()
    data = parse_json(challenge.canonical_bytes)
    assert isinstance(data, dict)
    subject = copy.deepcopy(data["subject"])
    assert subject["subplans"][0]["operation"] == "CREATE"
    assert subject["subplans"][0]["spend_bound"] == {"currency": "USD", "minor_units": 25_000}

    subject["subplans"][0]["spend_bound"] = {"spend_capability": "NOT_SPEND_CAPABLE"}
    maxima: dict[str, int] = {}
    for entry in subject["subplans"]:
        bound = entry["spend_bound"]
        if "currency" in bound:
            currency = bound["currency"]
            maxima[currency] = max(maxima.get(currency, 0), bound["minor_units"])
    subject["spend_bounds_by_currency"] = [
        {"currency": currency, "maximum_minor_units": maxima[currency]}
        for currency in sorted(maxima)
    ]

    with pytest.raises(AdhelmError):
        _rebuilt(challenge, subject)


# --- Finding 2 -------------------------------------------------------------


def test_an_attempt_recovery_can_be_approved_after_its_source_plan_expired() -> None:
    """The recovery kind must be obtainable when a recovery is actually needed.

    ``ATTEMPT_RECOVERY`` exists to give ``SafetyKernel.record_attempt_recovery``
    the host proof it was missing, and that path is for an attempt the kernel
    never finished journaling -- a process that died inside the write path.
    That is discovered on a later run, after the compiled plan's short expiry
    has passed; ``reconcile_run`` documents the same situation as one where
    "expired write grants are acceptable", and ``record_attempt_recovery``
    checks only the recovery grant's own expiry, never the plan's.

    ``_composite_subject_from_plan`` nevertheless sets the subject's
    ``expires_at`` to ``plan.expires_at`` for both kinds, and ``_decode_challenge``
    refuses a challenge whose expiry exceeds it.  A recovery challenge therefore
    cannot be issued once the plan expired -- which is exactly the state a
    workspace blocked by an unresolved ``APPLY_STARTED`` is in -- so the
    authorized exit the kernel provides has no obtainable approval and the
    barrier stays closed forever.
    """

    plan = composite_plan()
    attempts = ((plan.subplans[1], _STARTED_RECORD_DIGEST),)
    discovered = plan.expires_at + timedelta(days=1)

    challenge = build_attempt_recovery_challenge(
        plan,
        attempts,
        grantor_ref="customer-grantor-1",
        review_lines=_REVIEW_LINES,
        expires_at=discovered + timedelta(minutes=5),
        configuration=configuration(),
        nonce=bytes(range(32, 64)),
        issued_at=discovered,
        now=discovered,
    )

    assert challenge.expires_at > plan.expires_at


def test_a_composite_plan_approval_still_cannot_outlive_its_plan() -> None:
    """The companion rule that must survive any fix to the finding above.

    Approving a plan past its own expiry would authorize running something the
    kernel will refuse, so the clamp is right for ``COMPOSITE_EXECUTION_PLAN``.
    Only the recovery sibling needs to outlive the plan it observes.
    """

    from adhelm.approval_protocol import build_composite_execution_plan_challenge

    plan = composite_plan()
    with pytest.raises(AdhelmError):
        build_composite_execution_plan_challenge(
            plan,
            grantor_ref="customer-grantor-1",
            review_lines=_REVIEW_LINES,
            expires_at=plan.expires_at + timedelta(seconds=1),
            configuration=configuration(),
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )


# --- Contributed regression: the compiler line budget ----------------------


_COMPILERS = [
    ("google_ads", "test_google_ads_compiler", "compile_google_ads_search_campaign"),
    ("meta_ads", "test_meta_compiler", "compile_meta_ads_campaign"),
    ("linkedin_ads", "test_linkedin_compiler", "compile_linkedin_ads_campaign"),
    ("tiktok_ads", "test_tiktok_compiler", "compile_tiktok_ads_campaign"),
    ("reddit_ads", "test_reddit_compiler", "compile_reddit_ads_campaign"),
    ("openai_ads", "test_openai_compiler", "compile_openai_ads_campaign"),
]


@pytest.mark.parametrize(("provider", "module_name", "entry"), _COMPILERS)
def test_every_compiler_review_fits_the_bounded_line_budget(
    provider: str, module_name: str, entry: str
) -> None:
    """``compiler_review_lines`` refuses rather than truncates, so pin the budget.

    Only ``google_ads`` was exercised, and it already emits 19 of the 24
    permitted lines.  A compiler that grows a handful of review fields would
    make its approval unbuildable at runtime with no earlier signal, so every
    provider's real fixture review is checked here, together with the redaction
    properties the lines are supposed to keep: no scheme separator, no digest
    run, printable ASCII only, and determinism.
    """

    module = importlib.import_module(module_name)
    spec = getattr(module, entry)(module._brief())
    lines = compiler_review_lines(provider, spec.redacted_review)

    assert lines == compiler_review_lines(provider, spec.redacted_review)
    assert 1 <= len(lines) <= 24
    for line in lines:
        assert len(line.encode("utf-8")) <= 120
        assert line.isascii() and "://" not in line
    rendered = " ".join(lines)
    assert "creative_content_digest" not in rendered
    assert spec.redacted_review.spec_digest not in rendered
