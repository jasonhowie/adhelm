"""The pilot record schema is closed: it can hold a pseudonym and digests, nothing else."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm import __version__
from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.pilot_records import (
    PILOT_WINDOW_DAYS,
    RELEASE_PROVIDERS,
    PilotRecord,
    PilotRecordContent,
    pilot_record_bytes,
    pilot_record_digest,
    pilot_steps,
    seal_pilot_record,
)
from adhelm.strict_json import validate_json_input

_COVERAGE = [
    {"dimension": "STRUCTURE", "status": "COVERED"},
    {"dimension": "TRACKING", "status": "NOT_READ"},
]


def content_fields(provider: str = "reddit_ads", **overrides: Any) -> dict[str, Any]:
    """The fields of a valid pilot record for one provider, with overrides applied."""
    fields: dict[str, Any] = {
        "provider": provider,
        "cli_version": __version__,
        "source_commit": "a" * 40,
        "platform": "linux",
        "python_version": "3.12.13",
        "pilot_date": date(2026, 10, 12),
        "account_pseudonym": "opaque:" + "b" * 64,
        "report_window": (
            None
            if provider == "gtm"
            else {"window_start": date(2026, 10, 1), "window_end": date(2026, 10, 7)}
        ),
        "steps": [
            {
                "step": step,
                "command": command,
                "outcome": "OK",
                "evidence_digest": digest([provider, step]),
            }
            for step, command in pilot_steps(provider)
        ],
        "audit_coverage": _COVERAGE,
        "ui_comparison": "MATCH",
    }
    fields.update(overrides)
    return fields


def sealed_record(provider: str = "reddit_ads", **overrides: Any) -> PilotRecord:
    return seal_pilot_record(PilotRecordContent(**content_fields(provider, **overrides)))


@pytest.mark.parametrize("provider", RELEASE_PROVIDERS)
def test_every_released_provider_has_a_plan_that_starts_with_enrollment_and_ends_verified(
    provider: str,
) -> None:
    steps = [step for step, _ in pilot_steps(provider)]

    assert steps[:2] == ["ENROLLMENT", "DOCTOR"]
    assert steps[-1] == "HISTORY_VERIFY"
    assert steps[-2] == "AUDIT"
    assert ("REPORT" in steps) is (provider != "gtm")
    assert sealed_record(provider).provider == provider


def test_a_provider_outside_the_release_has_no_plan() -> None:
    for provider in ("hubspot", "salesforce", "ga4_admin", "apple_ads"):
        with pytest.raises(ValueError, match="no 0.1.0 pilot plan"):
            pilot_steps(provider)
        with pytest.raises(ValidationError):
            PilotRecordContent(**{**content_fields("reddit_ads"), "provider": provider})


def test_a_record_round_trips_through_its_reviewable_text() -> None:
    record = sealed_record()

    parsed = validate_json_input(PilotRecord, pilot_record_bytes(record))

    assert parsed == record
    assert pilot_record_digest(parsed) == parsed.record_digest
    assert pilot_record_bytes(record) == pilot_record_bytes(parsed)


def test_the_digest_covers_every_field_but_itself() -> None:
    record = sealed_record()
    changed = sealed_record(ui_comparison="MISMATCH")

    assert pilot_record_digest(record) == record.record_digest
    assert changed.record_digest != record.record_digest
    assert pilot_record_digest(changed) == changed.record_digest


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 10, 1), date(2026, 10, 6)),
        (date(2026, 10, 1), date(2026, 10, 8)),
        (date(2026, 10, 1), date(2026, 10, 1)),
        (date(2026, 10, 7), date(2026, 10, 1)),
    ],
)
def test_the_report_window_is_exactly_seven_inclusive_days(start: date, end: date) -> None:
    assert PILOT_WINDOW_DAYS == 7
    with pytest.raises(ValidationError):
        PilotRecordContent(
            **content_fields(report_window={"window_start": start, "window_end": end})
        )


def test_a_gtm_record_has_no_report_window_and_every_other_provider_must() -> None:
    with pytest.raises(ValidationError):
        PilotRecordContent(
            **content_fields(
                "gtm",
                report_window={"window_start": date(2026, 10, 1), "window_end": date(2026, 10, 7)},
            )
        )
    with pytest.raises(ValidationError):
        PilotRecordContent(**content_fields("reddit_ads", report_window=None))


def test_the_steps_must_be_the_providers_plan_in_order() -> None:
    steps = content_fields()["steps"]
    for bad in (
        steps[:-1],
        steps[::-1],
        [*steps, steps[-1]],
        [{**steps[0], "command": "doctor"}, *steps[1:]],
    ):
        with pytest.raises(ValidationError):
            PilotRecordContent(**content_fields(steps=bad))
    # A GTM record cannot carry a report step, nor a report-shaped provider omit it.
    with pytest.raises(ValidationError):
        PilotRecordContent(**content_fields("gtm", steps=content_fields("reddit_ads")["steps"]))


def test_each_audit_dimension_has_exactly_one_status() -> None:
    duplicated = [*_COVERAGE, {"dimension": "STRUCTURE", "status": "PARTIAL"}]
    with pytest.raises(ValidationError):
        PilotRecordContent(**content_fields(audit_coverage=duplicated))
    with pytest.raises(ValidationError):
        PilotRecordContent(**content_fields(audit_coverage=[]))


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_commit": "main"},
        {"source_commit": "A" * 40},
        {"source_commit": "a" * 39},
        {"account_pseudonym": "act_123456789"},
        {"account_pseudonym": "opaque:" + "b" * 63},
        {"python_version": "Python 3.12 /opt/someone"},
        {"platform": "freebsd"},
        {"ui_comparison": "NOT_COMPARED"},
        {"cli_version": "0.1.0 /opt/someone"},
    ],
)
def test_free_form_values_are_rejected_where_an_identifier_could_hide(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        PilotRecordContent(**content_fields(**overrides))


def test_no_text_field_of_the_schema_is_free_form() -> None:
    """Every string the schema accepts is a pattern, an enumeration or a date."""
    schema = PilotRecord.model_json_schema()
    free: list[str] = []
    for owner, spec in [("PilotRecord", schema), *schema["$defs"].items()]:
        for name, prop in spec["properties"].items():
            for option in prop.get("anyOf", [prop]):
                node = option.get("items", option)
                closed = {"pattern", "enum", "const"} & node.keys() or node.get("format") == "date"
                if node.get("type") == "string" and not closed:
                    free.append(f"{owner}.{name}")

    # The one unconstrained string is a step's command path, which
    # `follows_the_provider_plan` pins to the plan rather than to caller text.
    assert free == ["PilotStep.command"]


def test_a_record_with_an_unknown_field_is_not_a_record() -> None:
    raw = json.loads(pilot_record_bytes(sealed_record()))
    raw["account_name"] = "Example Shop"

    with pytest.raises((AdhelmError, ValidationError)):
        validate_json_input(PilotRecord, json.dumps(raw))
