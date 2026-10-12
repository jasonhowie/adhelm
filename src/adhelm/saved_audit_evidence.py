"""Select retained provider audit evidence from one verified local checkpoint.

`adhelm.saved_reports` selects one retained normalized report.  This module
selects the rest of the evidence a provider audit rule table needs: the
resource snapshots one completed inventory read appended, the snapshots the
read before it appended, and the local creative deployments that declare a
provider resource for the same account.

Everything comes from a single verified ledger checkpoint.  This module makes
no provider request, reads no credential, and appends no record.

## How one inventory read is selected

`run_inventory` appends one `RESOURCE_SNAPSHOT` at a time and then appends an
`INVENTORY_READ_COMPLETED` marker naming exactly the snapshot records that read
produced.  A read is selected from its marker, never inferred from the shape of
the records, because the durable prefix an interrupted append loop leaves is
indistinguishable from a finished read by shape alone.

Three cases follow, and each is reported explicitly:

* **Complete.** Every snapshot named by a marker forms one whole read.  It is
  `COMPLETE_BOUNDED_READ`: what it omits is real evidence.
* **Interrupted.** Snapshots appended after this product had already recorded a
  marker for the same account, but not named by any marker, are the prefix of a
  read that never finished.  They are never selected as current and never used
  as a drift baseline; the provenance counts them.
* **Legacy.** Snapshots retained before this product recorded read completions
  at all have no marker and no prefix to distinguish.  They are split on a
  repeated resource reference, selected, and declared
  `READ_BOUNDARY_UNVERIFIED` with a `LEGACY_READ_WITHOUT_COMPLETION_MARKER`
  provenance entry.  Absence-based drift is suppressed for them and absence
  rules drop to low confidence, because the same records can fabricate both
  deletions and creations.

The selected read is checked for a complete hierarchy -- exactly one account,
and every declared parent present in the same read -- and refused if that does
not hold.  Nothing is truncated or filled in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from adhelm.audits import (
    ProviderAuditEvidence,
    ResourceSnapshotState,
    RetainedCreativeCatalog,
    RetainedCreativeCatalogEntry,
    RetainedInventory,
    RetainedSnapshotRecord,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import AD_PROVIDERS

_DIGEST = re.compile(r"[0-9a-f]{64}")
_RESOURCE_TYPES = frozenset({"ACCOUNT", "CAMPAIGN_GROUP", "CAMPAIGN", "AD_GROUP", "AD"})
_MAXIMUM_RECORDS = 5_000


def _observed_at(value: object) -> datetime:
    """Parse a retained record's observation instant; a naive one is refused."""
    if not isinstance(value, str):
        raise AdhelmError(
            "INVENTORY_READ_INCOMPLETE", "A retained snapshot has no observation time."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdhelmError(
            "INVENTORY_READ_INCOMPLETE", "A retained snapshot observation time is invalid."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AdhelmError(
            "INVENTORY_READ_INCOMPLETE", "A retained snapshot observation time has no offset."
        )
    return parsed


def _checkpoint_records(ledger: Ledger, stream: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checkpoint = ledger.checkpoint()
    if checkpoint.get("status") != "VERIFIED_CHECKPOINT":
        raise AdhelmError(
            "LEDGER_CHECKPOINT_UNVERIFIED",
            "Audit evidence requires a verified local checkpoint.",
        )
    records = checkpoint["records_by_stream"][stream]
    if not isinstance(records, list):
        raise AdhelmError(
            "LEDGER_CHECKPOINT_UNVERIFIED", "Verified checkpoint records are unavailable."
        )
    return checkpoint, records


@dataclass(frozen=True)
class _Read:
    """One selected inventory read and how much its boundary is proven."""

    records: tuple[dict[str, Any], ...]
    completeness: str
    marker_record_digest: str | None


def _legacy_reads(records: list[dict[str, Any]]) -> list[tuple[dict[str, Any], ...]]:
    """Split pre-marker history on a repeated resource reference.

    This is the only reconstruction available for records retained before this
    product recorded read completions.  It cannot tell a finished read from a
    prefix, which is exactly why every read it produces is
    `READ_BOUNDARY_UNVERIFIED`.
    """
    reads: list[tuple[dict[str, Any], ...]] = []
    current: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        resource_ref = record["payload"]["resource_ref"]
        if resource_ref in seen:
            reads.append(tuple(current))
            current, seen = [], set()
        current.append(record)
        seen.add(resource_ref)
    if current:
        reads.append(tuple(current))
    return reads


def _select_reads(
    snapshots: list[dict[str, Any]], markers: list[dict[str, Any]]
) -> tuple[list[_Read], int, int]:
    """Group one account's snapshots into reads, newest last.

    Returns the selectable reads, the number of snapshot records left over by an
    interrupted read, and the number of legacy records with no marker at all.
    """
    by_digest = {record["record_digest"]: record for record in snapshots}
    claimed: set[str] = set()
    complete: list[_Read] = []
    for marker in markers:
        named = [str(item) for item in marker["payload"]["snapshot_record_digests"]]
        selected = [by_digest[digest] for digest in named if digest in by_digest]
        if len(selected) != len(named):
            # A marker whose snapshots are not all retained at this checkpoint
            # proves nothing about what the account contained.
            continue
        claimed.update(named)
        complete.append(
            _Read(
                records=tuple(selected),
                completeness="COMPLETE_BOUNDED_READ",
                marker_record_digest=str(marker["record_digest"]),
            )
        )
    if not complete:
        legacy = [
            _Read(records=read, completeness="READ_BOUNDARY_UNVERIFIED", marker_record_digest=None)
            for read in _legacy_reads(snapshots)
        ]
        return legacy, 0, len(snapshots)
    # Once this product has recorded a completion for the account, any later
    # unclaimed snapshot is the prefix of a read that never finished.  It is
    # never current and never a baseline.
    first_marked = min(record["stream_sequence"] for read in complete for record in read.records)
    interrupted = sum(
        1
        for record in snapshots
        if record["record_digest"] not in claimed and record["stream_sequence"] > first_marked
    )
    legacy_records = [
        record
        for record in snapshots
        if record["record_digest"] not in claimed and record["stream_sequence"] < first_marked
    ]
    legacy = [
        _Read(records=read, completeness="READ_BOUNDARY_UNVERIFIED", marker_record_digest=None)
        for read in _legacy_reads(legacy_records)
    ]
    ordered = sorted(
        [*legacy, *complete], key=lambda read: max(item["stream_sequence"] for item in read.records)
    )
    return ordered, interrupted, len(legacy_records)


def _complete_hierarchy(read: tuple[dict[str, Any], ...]) -> bool:
    """Check that one reconstructed read is a whole, internally consistent hierarchy."""
    by_ref = {record["payload"]["resource_ref"]: record["payload"] for record in read}
    accounts = [item for item in by_ref.values() if item["resource_type"] == "ACCOUNT"]
    if len(accounts) != 1 or len(by_ref) != len(read) or len(read) > _MAXIMUM_RECORDS:
        return False
    for payload in by_ref.values():
        if payload["resource_type"] not in _RESOURCE_TYPES:
            return False
        if payload["resource_type"] == "ACCOUNT":
            if payload["parent_resource_refs"]:
                return False
            continue
        parents = payload["parent_resource_refs"]
        if not parents or any(parent not in by_ref for parent in parents):
            return False
    return True


def _inventory(provider: str, account_ref: str, read: _Read) -> RetainedInventory:
    records = tuple(
        RetainedSnapshotRecord(
            record_digest=record["record_digest"],
            observed_at=_observed_at(record["observed_at"]),
            snapshot=ResourceSnapshotState(
                provider_api_version=record["payload"]["provider_api_version"],
                resource_type=record["payload"]["resource_type"],
                resource_ref=record["payload"]["resource_ref"],
                parent_resource_refs=tuple(record["payload"]["parent_resource_refs"]),
                normalized_state=dict(record["payload"]["normalized_state"]),
                normalized_state_digest=record["payload"]["normalized_state_digest"],
                effective_status=record["payload"]["effective_status"],
                data_quality_statuses=tuple(record["payload"]["data_quality_statuses"]),
            ),
        )
        for record in read.records
    )
    return RetainedInventory(
        provider=provider,  # type: ignore[arg-type]
        account_ref=account_ref,
        # One read appends its records one at a time, so the read is observed
        # through to its last record's observation time.
        observed_at=max(item.observed_at for item in records),
        completeness=read.completeness,  # type: ignore[arg-type]
        records=records,
    )


def _creative_catalog(
    records: list[dict[str, Any]], provider: str, account_refs: list[str]
) -> tuple[RetainedCreativeCatalog, int]:
    """Build the *outstanding* local creative bindings for this provider account.

    A local deployment record is a *declaration*.  Local creative registration
    retains `deployment_status=NOT_CREATED` and `provider_currentness=
    NOT_EVALUATED`, so no entry here is a provider-verified association.

    A deployment identity is content-derived and includes its effective
    interval, so replacing a creative version retains both the closed record and
    its replacement.  Only a deployment whose own `association_status` is
    `ACTIVE` is an outstanding declaration; every other value, reviewed or not,
    is treated as closed rather than guessed at.
    """
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["record_kind"] != "CREATIVE_DEPLOYMENT":
            continue
        payload = record["payload"]
        # One account may be retained under more than one spelling; a
        # declaration under either of them belongs to it.
        if payload.get("provider") != provider or payload.get("account_ref") not in account_refs:
            continue
        if payload.get("association_status") != "ACTIVE":
            continue
        latest[str(payload["deployment_id"])] = record
    entries = tuple(
        RetainedCreativeCatalogEntry(
            creative_version_id=record["payload"]["creative_version_id"],
            record_digest=record["record_digest"],
            provider=provider,  # type: ignore[arg-type]
            provider_resource_ref=record["payload"]["ad_ref"],
            binding_status="LOCAL_DECLARED_UNVERIFIED",
        )
        for _, record in sorted(latest.items())
    )
    return RetainedCreativeCatalog(entries=entries), len(entries)


# TikTok has the same two-spelling shape as Meta below: `run_inventory` appends
# with `profile.account_ref`, which a TikTok profile declares as the bare
# decimal advertiser ID, while a `TikTokAdsClient` report envelope carries the
# canonical `tiktok_ads.advertiser.<id>` reference.  The mapping is exact and
# reversible -- one fixed prefix, nothing else -- so reducing both sides joins
# one advertiser's records and still refuses a genuinely different advertiser.
_TIKTOK_ADVERTISER_PREFIX: Final = "tiktok_ads.advertiser."

# Reddit has the same two-spelling shape as Meta below: `run_inventory` appends
# with `profile.account_ref`, the bare Reddit ad account ID, while a
# `RedditAdsClient` report envelope carries the canonical `reddit_ads.account.<id>`
# reference.  The mapping is exact and reversible -- one fixed prefix, and a
# Reddit resource ID contains no `.` -- so reducing both sides joins one account's
# records and still refuses a genuinely different account.
_REDDIT_ACCOUNT_PREFIX: Final = "reddit_ads.account."

# LinkedIn and OpenAI have the same two-spelling shape: `run_inventory` appends with
# `profile.account_ref`, the bare LinkedIn account number or OpenAI ad account
# identifier, while the report adapters return the canonical
# `linkedin_ads.account.<id>` / `openai_ads.account.<id>` reference (the adapters'
# `reference_for`) in the report envelope.  Without this mapping the audit of a report
# this product just retained finds no inventory for it, and a live read pilot of either
# provider cannot complete.  Each mapping is one fixed prefix and the identifiers
# contain no `.`, so it is exact and reversible, and a different account still differs.
_LINKEDIN_ACCOUNT_PREFIX: Final = "linkedin_ads.account."
_OPENAI_ACCOUNT_PREFIX: Final = "openai_ads.account."

# Meta keys its retained records by the bare numeric ad-account identifier,
# because `run_inventory` appends with `profile.account_ref` and
# `MetaAccountTokenProfile` constrains that to a `MetaNativeIdentifier`.  A
# `MetaAdsClient` report envelope instead carries the canonical
# `meta_ads.account.act_<id>` reference.  Both spellings name one account, so
# selection and the account-binding check compare this derived key rather than
# the raw string; a genuinely different account still differs and is still
# refused.  Meta reduces by two fixed prefixes in turn (the canonical one, then the
# provider's own `act_`).
_META_ACCOUNT_PREFIX: Final = "meta_ads.account."
_META_NATIVE_PREFIX: Final = "act_"


@dataclass(frozen=True)
class _AccountKeyRule:
    """How one provider's canonical report reference reduces to its retained key."""

    prefixes: tuple[str, ...]
    refusal: str
    next_action: str


#: The five providers whose retained records are keyed by the native identifier while
#: their report envelopes carry a canonical reference.  No other provider is
#: normalized: the mapping is exact and reversible only for these, and inventing one
#: elsewhere would risk joining two accounts that merely look alike.
_ACCOUNT_KEY_RULES: Final[dict[str, _AccountKeyRule]] = {
    "meta_ads": _AccountKeyRule(
        (_META_ACCOUNT_PREFIX, _META_NATIVE_PREFIX),
        "A Meta Ads account reference must name an ad account.",
        "Select the account by its numeric ad-account identifier.",
    ),
    "tiktok_ads": _AccountKeyRule(
        (_TIKTOK_ADVERTISER_PREFIX,),
        "A TikTok Ads account reference must name an advertiser.",
        "Select the account by its decimal advertiser identifier.",
    ),
    "reddit_ads": _AccountKeyRule(
        (_REDDIT_ACCOUNT_PREFIX,),
        "A Reddit Ads account reference must name an ad account.",
        "Select the account by its Reddit ad account identifier.",
    ),
    "linkedin_ads": _AccountKeyRule(
        (_LINKEDIN_ACCOUNT_PREFIX,),
        "A LinkedIn Ads account reference must name an ad account.",
        "Select the account by its numeric ad account identifier.",
    ),
    "openai_ads": _AccountKeyRule(
        (_OPENAI_ACCOUNT_PREFIX,),
        "An OpenAI Ads account reference must name an ad account.",
        "Select the account by its OpenAI ad account identifier.",
    ),
}


def retained_account_key(provider: str, account_ref: str) -> str:
    """The key retained records use for one provider account reference.

    The value is only ever compared with another key from the same function.
    It is never persisted, emitted, or presented as an account identifier.

    A reference that reduces to nothing -- `meta_ads.account.act_`,
    `tiktok_ads.advertiser.`, `reddit_ads.account.`, `linkedin_ads.account.`,
    `openai_ads.account.` and the prefixes alone do -- is refused rather than
    returned as an empty key that would compare equal to every other empty key and
    join two unrelated accounts.
    """
    rule = _ACCOUNT_KEY_RULES.get(provider)
    if rule is None:
        return account_ref
    derived = account_ref
    for prefix in rule.prefixes:
        derived = derived.removeprefix(prefix)
    if not derived:
        raise AdhelmError(
            "INVALID_AUDIT_EVIDENCE_SELECTION",
            rule.refusal,
            exit_code=2,
            next_action=rule.next_action,
        )
    return derived


def load_audit_evidence(
    ledger: Ledger,
    *,
    provider: str,
    account_ref: str | None = None,
    inventory_record_digest: str | None = None,
    include_earlier: bool = True,
    include_creative_catalog: bool = True,
) -> tuple[ProviderAuditEvidence, dict[str, Any]]:
    """Select retained inventory, drift, and creative-binding evidence.

    Returns the evidence and an explicit provenance record.  Every omission is
    named in that provenance; nothing is silently dropped.
    """
    if provider == "ga4":
        from adhelm.saved_ga4_audit_evidence import load_ga4_audit_evidence

        return load_ga4_audit_evidence(
            ledger, account_ref=account_ref, inventory_record_digest=inventory_record_digest
        )
    if provider not in AD_PROVIDERS:
        raise AdhelmError(
            "INVALID_AUDIT_EVIDENCE_SELECTION",
            "Provider audit evidence is available for advertising providers and GA4 only.",
            exit_code=2,
        )
    if inventory_record_digest is not None and _DIGEST.fullmatch(inventory_record_digest) is None:
        raise AdhelmError(
            "INVALID_AUDIT_EVIDENCE_SELECTION",
            "A retained inventory record digest is required.",
            exit_code=2,
        )
    checkpoint, records = _checkpoint_records(ledger, "changes")
    snapshots = [
        record
        for record in records
        if record["record_kind"] == "RESOURCE_SNAPSHOT" and record["provider"] == provider
    ]
    accounts = sorted({str(record["account_ref"]) for record in snapshots})
    if account_ref is None:
        if len({retained_account_key(provider, item) for item in accounts}) > 1:
            raise AdhelmError(
                "AUDIT_ACCOUNT_SELECTION_REQUIRED",
                "This provider has retained inventory for more than one account.",
                exit_code=2,
                next_action="Select one account with `--account`.",
            )
        account_ref = accounts[0] if accounts else None
    # One account can be retained under more than one spelling: `run_inventory`
    # keys by `profile.account_ref`, and for Meta a `MetaAccountTokenProfile`
    # constrains that to the bare numeric identifier while a generic `Profile`
    # accepts the `act_<id>` form.  Selection therefore matches on the derived
    # key, so **every** completed read for the account is a candidate and none
    # is silently excluded.  The caller may name the account in either
    # spelling, and every retained spelling is named in the provenance below.
    requested = retained_account_key(provider, account_ref) if account_ref is not None else None
    spellings = sorted(
        item for item in accounts if retained_account_key(provider, item) == requested
    )
    if spellings:
        # Report one retained spelling deterministically, whichever one the
        # caller happened to type, so the same ledger always produces the same
        # provenance and the same evidence.  Every retained spelling is listed
        # in `retained_account_spellings` below.
        account_ref = spellings[0]

    def _belongs(record: dict[str, Any]) -> bool:
        reference = record["account_ref"]
        return (
            requested is not None
            and isinstance(reference, str)
            and retained_account_key(provider, reference) == requested
        )

    selected = [record for record in snapshots if _belongs(record)]
    markers = [
        record
        for record in records
        if record["record_kind"] == "INVENTORY_READ_COMPLETED"
        and record["provider"] == provider
        and _belongs(record)
    ]
    reads, interrupted_records, legacy_records = _select_reads(selected, markers)
    if not reads:
        raise AdhelmError(
            "INVENTORY_NOT_RETAINED",
            "No retained inventory read is available for this provider account.",
            next_action="Run `adhelm inventory` for the configured read profile first.",
        )
    index = len(reads) - 1
    if inventory_record_digest is not None:
        matching = [
            position
            for position, read in enumerate(reads)
            if any(record["record_digest"] == inventory_record_digest for record in read.records)
        ]
        if len(matching) != 1:
            if any(record["record_digest"] == inventory_record_digest for record in selected):
                # The record is retained; it just belongs to a read that never
                # recorded its completion, so it is not a selectable read.
                raise AdhelmError(
                    "INVENTORY_READ_INCOMPLETE",
                    "The selected snapshot belongs to an inventory read that never completed.",
                    next_action=(
                        "Run `adhelm inventory` again; an unfinished read is never audited."
                    ),
                )
            raise AdhelmError(
                "INVENTORY_SNAPSHOT_NOT_RETAINED",
                "The selected inventory snapshot is unavailable at the verified checkpoint.",
            )
        index = matching[0]
    if not _complete_hierarchy(reads[index].records):
        raise AdhelmError(
            "INVENTORY_READ_INCOMPLETE",
            "The selected retained inventory read is not a complete resource hierarchy.",
            next_action=(
                "Run `adhelm inventory` again; a partial read is never audited as complete."
            ),
        )
    assert account_ref is not None
    inventory = _inventory(provider, account_ref, reads[index])
    earlier: RetainedInventory | None = None
    earlier_read: _Read | None = None
    if not include_earlier:
        earlier_status = "NOT_REQUESTED"
    elif index == 0:
        earlier_status = "NO_EARLIER_RETAINED_READ"
    elif not _complete_hierarchy(reads[index - 1].records):
        earlier_status = "OMITTED_INCOMPLETE_EARLIER_READ"
    else:
        earlier_read = reads[index - 1]
        earlier = _inventory(provider, account_ref, earlier_read)
        earlier_status = "SELECTED"
    catalog: RetainedCreativeCatalog | None = None
    catalog_count = 0
    if include_creative_catalog:
        _, creative_records = _checkpoint_records(ledger, "creative")
        catalog, catalog_count = _creative_catalog(creative_records, provider, spellings)
    provenance = {
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "provider": provider,
        "account_ref": account_ref,
        # Every retained spelling of this one account, so a reader can see that
        # two of them were joined rather than one of them dropped.
        "retained_account_spellings": spellings,
        "retained_inventory_reads": len(reads),
        "selected_inventory_read_index": index,
        "selected_inventory_completeness": inventory.completeness,
        "selected_inventory_read_boundary": (
            "INVENTORY_READ_COMPLETED_MARKER"
            if reads[index].marker_record_digest is not None
            else "LEGACY_READ_WITHOUT_COMPLETION_MARKER"
        ),
        "selected_inventory_completion_record_digest": reads[index].marker_record_digest,
        "interrupted_snapshot_record_count": interrupted_records,
        "legacy_snapshot_record_count": legacy_records,
        "selected_inventory_record_count": len(inventory.records),
        "selected_inventory_observed_at": inventory.observed_at.isoformat().replace("+00:00", "Z"),
        "earlier_inventory_status": earlier_status,
        "earlier_inventory_record_count": len(earlier.records) if earlier is not None else 0,
        # The baseline's own boundary decides whether an absence between the two
        # reads means anything, so it is reported beside the selected read's.
        "earlier_inventory_completeness": (earlier.completeness if earlier is not None else None),
        "earlier_inventory_read_boundary": (
            None
            if earlier_read is None
            else "INVENTORY_READ_COMPLETED_MARKER"
            if earlier_read.marker_record_digest is not None
            else "LEGACY_READ_WITHOUT_COMPLETION_MARKER"
        ),
        "earlier_inventory_completion_record_digest": (
            earlier_read.marker_record_digest if earlier_read is not None else None
        ),
        "creative_catalog_status": ("SELECTED" if include_creative_catalog else "NOT_REQUESTED"),
        "creative_catalog_entry_count": catalog_count,
        "live_provider_reads": "NONE_RETAINED_RECORDS_ONLY",
    }
    return (
        ProviderAuditEvidence(
            inventory=inventory, earlier_inventory=earlier, creative_catalog=catalog
        ),
        provenance,
    )
