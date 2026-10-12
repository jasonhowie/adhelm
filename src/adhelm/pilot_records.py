"""The redacted live read pilot record and the step plan it must satisfy.

`docs/release-0.1.0.md` ("Live read pilot") defines what a provider's pilot must
show before the provider may carry the `LIVE_PILOTED` release label. This module
owns the closed schema of the record the `pilot run` command writes and the
release label loader reads. The record contains pseudonyms, closed-vocabulary
tokens, dates and digests only: it has no field that could hold an account
identifier, a name, a token or a provider payload, and :class:`PilotRecordContent`
inherits the same data screening as every other strict model.

A record exists only for a pilot whose every required step succeeded, so the
record carries no per-step failure state. Its digest makes an edited record
detectable; it does not prove that the pilot ran live, which is the owner's
assertion by committing it (`docs/pilots/README.md`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Final, Literal, get_args

from pydantic import Field, model_validator

from adhelm.canonical import digest
from adhelm.models import StrictModel

ReleaseProvider = Literal[
    "google_ads",
    "meta_ads",
    "linkedin_ads",
    "tiktok_ads",
    "reddit_ads",
    "openai_ads",
    "ga4",
    "gtm",
]
#: The providers `docs/release-0.1.0.md` puts in scope, each carrying one label.
RELEASE_PROVIDERS: Final[tuple[str, ...]] = get_args(ReleaseProvider)

PilotStepName = Literal["ENROLLMENT", "DOCTOR", "INVENTORY", "REPORT", "AUDIT", "HISTORY_VERIFY"]
PilotUiComparison = Literal["MATCH", "MISMATCH"]
PilotPlatform = Literal["linux", "macos", "windows"]
CoverageStatus = Literal["COVERED", "PARTIAL", "NOT_READ", "NOT_APPLICABLE"]

#: The pilot report window: seven inclusive days, so the end is six days on.
PILOT_WINDOW_DAYS: Final = 7

_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_Token = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]
_REPORT_COMMANDS: Final[Mapping[str, str]] = {
    "google_ads": "report google-ads",
    "meta_ads": "report meta-ads",
    "linkedin_ads": "report linkedin-ads",
    "tiktok_ads": "report tiktok-ads",
    "reddit_ads": "report reddit-ads",
    "openai_ads": "report openai-ads",
    "ga4": "report ga4",
}


def pilot_steps(provider: str) -> tuple[tuple[PilotStepName, str], ...]:
    """Return the ordered (step, `discover` command path) plan for one provider.

    Every provider enrolls a read profile, passes `doctor`, reads a fixed
    inventory, audits, and verifies local history. GTM has no performance report,
    so its plan has no `REPORT` step and its audit is the read-only dependency
    graph audit over the retained full inventory.
    """
    if provider not in RELEASE_PROVIDERS:
        raise ValueError("Provider has no 0.1.0 pilot plan")
    if provider == "gtm":
        inventory, audit = "measurement gtm", "measurement gtm-graph"
    elif provider == "ga4":
        inventory, audit = "measurement ga4-admin", "audit --provider-rules"
    else:
        inventory, audit = "inventory", "audit --provider-rules"
    plan: list[tuple[PilotStepName, str]] = [
        ("ENROLLMENT", "config show"),
        ("DOCTOR", "doctor"),
        ("INVENTORY", inventory),
    ]
    if provider != "gtm":
        plan.append(("REPORT", _REPORT_COMMANDS[provider]))
    plan += [("AUDIT", audit), ("HISTORY_VERIFY", "history verify")]
    return tuple(plan)


class PilotStep(StrictModel):
    """One successful step: the command run, its outcome token and its evidence digest."""

    step: PilotStepName
    command: Annotated[str, Field(min_length=1, max_length=40)]
    outcome: _Token
    evidence_digest: _Sha256


class PilotCoverage(StrictModel):
    """One audit dimension's coverage status, copied from the audit's coverage table."""

    dimension: _Token
    status: CoverageStatus


class PilotWindow(StrictModel):
    window_start: date
    window_end: date

    @model_validator(mode="after")
    def seven_inclusive_days(self) -> PilotWindow:
        if (self.window_end - self.window_start).days != PILOT_WINDOW_DAYS - 1:
            raise ValueError("A pilot report window is exactly seven inclusive days")
        return self


class PilotRecordContent(StrictModel):
    """Everything a pilot record states except the digest that seals it."""

    schema_version: Literal[1] = 1
    record_kind: Literal["LIVE_READ_PILOT"] = "LIVE_READ_PILOT"
    provider: ReleaseProvider
    cli_version: Annotated[str, Field(pattern=r"^[0-9A-Za-z.+-]{1,40}$")]
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    platform: PilotPlatform
    python_version: Annotated[str, Field(pattern=r"^3\.[0-9]{1,2}\.[0-9]{1,3}$")]
    pilot_date: date
    account_pseudonym: Annotated[str, Field(pattern=r"^opaque:[0-9a-f]{64}$")]
    report_window: PilotWindow | None
    steps: list[PilotStep] = Field(min_length=1, max_length=len(get_args(PilotStepName)))
    audit_coverage: list[PilotCoverage] = Field(min_length=1, max_length=32)
    ui_comparison: PilotUiComparison

    @model_validator(mode="after")
    def follows_the_provider_plan(self) -> PilotRecordContent:
        plan = pilot_steps(self.provider)
        if [(item.step, item.command) for item in self.steps] != list(plan):
            raise ValueError("A pilot record carries exactly its provider's steps, in order")
        if (self.report_window is None) != (self.provider == "gtm"):
            raise ValueError("A pilot report window exists exactly when the provider reports")
        dimensions = [item.dimension for item in self.audit_coverage]
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("Each audit dimension has exactly one coverage status")
        return self


class PilotRecord(PilotRecordContent):
    """A sealed pilot record, as committed under `docs/pilots/`."""

    record_digest: _Sha256


def pilot_record_digest(record: PilotRecordContent) -> str:
    """Digest every field of a record except its own digest."""
    return digest(record.model_dump(mode="json", exclude={"record_digest"}))


def seal_pilot_record(content: PilotRecordContent) -> PilotRecord:
    return PilotRecord(**content.model_dump(), record_digest=pilot_record_digest(content))


def pilot_record_bytes(record: PilotRecord) -> bytes:
    """Render a record as the stable, reviewable text committed under `docs/pilots/`."""
    return (json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
