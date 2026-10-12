"""Provider release labels, sourced from committed live read pilot records.

`docs/release-0.1.0.md` ("Provider status labels") gives every provider exactly
one label. A provider is `LIVE_PILOTED` only when a committed pilot record
under `docs/pilots/` is schema-valid, digest-consistent, for that provider and
for this exact build version. Every other provider, and every provider when no
record exists, is `EXPERIMENTAL`: fixture-verified, with no live evidence.

The label is a statement about evidence, never a capability grant. It moves no
`docs/capability-matrix.md` status, enables nothing, and cannot be set by
configuration, an environment variable or a flag: the only input is a record
file that ships with the installation, in the same way the bundled guidance
does. A record that is present but unusable is never ignored silently. It
leaves its provider `EXPERIMENTAL`, and `doctor` reports why.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import ValidationError

from adhelm import __version__
from adhelm.errors import AdhelmError
from adhelm.models import ProviderReleaseLabel
from adhelm.pilot_records import RELEASE_PROVIDERS, PilotRecord, pilot_record_digest
from adhelm.strict_json import validate_json_input

ReleaseLabel = Literal["LIVE_PILOTED", "EXPERIMENTAL"]
LIVE_PILOTED: Final = "LIVE_PILOTED"
EXPERIMENTAL: Final = "EXPERIMENTAL"
LABEL_MEANINGS: Final[Mapping[str, str]] = {
    LIVE_PILOTED: (
        "Fixture-verified and read-piloted on a live account; a redacted pilot record is committed."
    ),
    EXPERIMENTAL: "Fixture-verified only; no live pilot evidence.",
}
ACCEPTED: Final = "ACCEPTED"

#: Hatch force-includes `docs/pilots` here in wheels; a source checkout reads it in place.
_INSTALLED_DIRECTORY: Final = "pilot_record_files"
_MAXIMUM_RECORD_BYTES: Final = 65_536
#: Provider spellings that are one released provider for labelling purposes: the GA4
#: Admin read is part of the GA4 pilot, not a separate released provider.
_RELEASE_PROVIDER_BY_SPELLING: Final[Mapping[str, str]] = {
    **{provider: provider for provider in RELEASE_PROVIDERS},
    "ga4_admin": "ga4",
}
#: Short provider names that command paths use (`auth meta-token`, `execute openai-pause`).
_RELEASE_PROVIDER_BY_COMMAND_WORD: Final[Mapping[str, str]] = {
    "meta": "meta_ads",
    "openai": "openai_ads",
    "linkedin": "linkedin_ads",
    "reddit": "reddit_ads",
    "tiktok": "tiktok_ads",
}
#: Outcomes of the closed success receipts that are sized byte for byte to the 1 KiB
#: minimum output bound, so a label cannot be added without changing what they retain.
#: Each is an unreleased write's result or a retained plan's receipt; the same commands'
#: refusals and every other output of the provider carry the label.
BOUND_SIZED_RECEIPT_OUTCOMES: Final = frozenset(
    {
        "EXECUTION_RESULT",
        "GA4_KEY_EVENT_CREATE_PLAN_RETAINED_NO_EXECUTION_AUTHORITY",
    }
)


@dataclass(frozen=True)
class PilotRecordStatus:
    """What the loader decided about one record file: accepted, or why it was not."""

    file: str
    state: str
    provider: str | None = None


@dataclass(frozen=True)
class PilotRecordInventory:
    statuses: tuple[PilotRecordStatus, ...]
    #: The most recent accepted record for each provider that has one.
    accepted: Mapping[str, PilotRecord]


@dataclass(frozen=True)
class ProviderLabel:
    provider: str
    label: ReleaseLabel
    meaning: str
    record: PilotRecord | None


def pilot_records_directory() -> Path | None:
    """Return the shipped pilot record directory, or ``None`` when there is none."""
    package = Path(__file__).resolve().parent
    installed = package / _INSTALLED_DIRECTORY
    if installed.is_dir():
        return installed
    source_root = package.parent.parent
    source = source_root / "docs" / "pilots"
    if package.parent.name == "src" and (source_root / "pyproject.toml").is_file():
        return source if source.is_dir() else None
    return None


def _inspect(path: Path) -> tuple[PilotRecordStatus, PilotRecord | None]:
    def rejected(code: str, provider: str | None = None) -> tuple[PilotRecordStatus, None]:
        return PilotRecordStatus(path.name, code, provider), None

    try:
        if path.is_symlink() or not path.is_file():
            return rejected("PILOT_RECORD_UNREADABLE")
        if path.stat().st_size > _MAXIMUM_RECORD_BYTES:
            return rejected("PILOT_RECORD_TOO_LARGE")
        raw = path.read_bytes()
    except OSError:
        return rejected("PILOT_RECORD_UNREADABLE")
    try:
        record = validate_json_input(PilotRecord, raw)
    except (AdhelmError, ValidationError):
        return rejected("PILOT_RECORD_INVALID")
    if record.record_digest != pilot_record_digest(record):
        return rejected("PILOT_RECORD_DIGEST_MISMATCH", record.provider)
    if record.cli_version != __version__:
        return rejected("PILOT_RECORD_VERSION_MISMATCH", record.provider)
    return PilotRecordStatus(path.name, ACCEPTED, record.provider), record


@functools.cache
def pilot_record_inventory() -> PilotRecordInventory:
    """Load and judge every shipped pilot record once per process."""
    directory = pilot_records_directory()
    statuses: list[PilotRecordStatus] = []
    latest: dict[str, PilotRecord] = {}
    for path in sorted(directory.glob("*.json")) if directory is not None else ():
        status, record = _inspect(path)
        statuses.append(status)
        if record is not None:
            held = latest.get(record.provider)
            if held is None or (record.pilot_date, record.record_digest) > (
                held.pilot_date,
                held.record_digest,
            ):
                latest[record.provider] = record
    return PilotRecordInventory(tuple(statuses), latest)


def provider_labels() -> tuple[ProviderLabel, ...]:
    """Return exactly one label for each released provider."""
    accepted = pilot_record_inventory().accepted
    labels: list[ProviderLabel] = []
    for provider in RELEASE_PROVIDERS:
        record = accepted.get(provider)
        label: ReleaseLabel = LIVE_PILOTED if record is not None else EXPERIMENTAL
        labels.append(ProviderLabel(provider, label, LABEL_MEANINGS[label], record))
    return tuple(labels)


def release_label(provider: str) -> ProviderLabel | None:
    """Return the label of a released provider, or of the provider it is part of.

    A provider outside the 0.1.0 release (Salesforce, HubSpot) has no label.
    """
    released = _RELEASE_PROVIDER_BY_SPELLING.get(provider)
    return next((label for label in provider_labels() if label.provider == released), None)


def is_released_provider(provider: str) -> bool:
    """Whether the 0.1.0 release includes this provider spelling (GA4 Admin counts as GA4)."""
    return provider in _RELEASE_PROVIDER_BY_SPELLING


def _released_providers_named_by(text: str) -> set[str]:
    """Match a command token or provider value by its leading provider slug.

    ``google-ads-campaign-pause`` and ``google_ads`` both name Google Ads;
    ``gtm-graph`` and ``gtm_web_inventory`` both name GTM; ``ga4-admin`` names GA4.
    """
    parts = text.replace("_", "-").split("-")
    return {
        _RELEASE_PROVIDER_BY_SPELLING[slug]
        for size in range(1, len(parts) + 1)
        if (slug := "_".join(parts[:size])) in _RELEASE_PROVIDER_BY_SPELLING
    }


def _released_providers_named_by_command_token(token: str) -> set[str]:
    """Like :func:`_released_providers_named_by`, plus the short names commands use.

    ``meta-campaign-pause`` and ``openai-pause`` name Meta Ads and OpenAI Ads. A
    short name is only matched in a command path, never in a data value, and
    ``google`` is not one: ``google-oauth`` is shared by three providers.
    """
    named = _released_providers_named_by(token)
    short = _RELEASE_PROVIDER_BY_COMMAND_WORD.get(token.replace("_", "-").split("-")[0])
    return named | {short} if short is not None else named


def labels_for_output(
    command: str, data: Mapping[str, Any], outcome: str | None = None
) -> list[ProviderReleaseLabel]:
    """Label every released provider an output names.

    An output names a provider when its command path does, or when any
    ``provider`` field in its data does, whatever the command group: the release
    definition has the label and its meaning in every output that names the
    provider, so refusals from the plan, execute and recover groups carry it too.
    Hubspot, Salesforce and other values that are not released providers name
    nothing. The label never grants a capability, so it says nothing about writes.

    The one exception is a closed receipt whose outcome is in
    :data:`BOUND_SIZED_RECEIPT_OUTCOMES`: those are sized to the 1 KiB minimum output
    bound with no room for a label, and no output that can refuse or explain
    carries that outcome.
    """
    if outcome in BOUND_SIZED_RECEIPT_OUTCOMES:
        return []
    named: set[str] = set()
    for token in command.split():
        named |= _released_providers_named_by_command_token(token)
    pending: list[Any] = [data]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            value = current.get("provider")
            if isinstance(value, str):
                named |= _released_providers_named_by(value)
            pending.extend(current.values())
        elif isinstance(current, list | tuple):
            pending.extend(current)
    return [
        ProviderReleaseLabel(
            provider=label.provider,
            label=label.label,
            meaning=label.meaning,
            pilot_record_digest=label.record.record_digest if label.record else None,
        )
        for label in provider_labels()
        if label.provider in named
    ]
