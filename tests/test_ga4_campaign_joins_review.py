"""Independent review of ticket M00.2 (deterministic GA4 <-> provider joins).

Every test here fails on `claude/w3-m00-2-joins` at 3f792ae and states the
behaviour the contract requires.  Nothing in this file changes product code.

Fixtures are reused from `tests/test_ga4_campaign_joins.py` so the review
exercises the exact shapes the implementer evidenced, not a parallel model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_ga4_campaign_joins import (
    _CAMPAIGN,
    _OTHER_CAMPAIGN,
    _PROPERTY,
    _aggregate_report,
    _cli_app,
    _contract,
    _entry,
    _joined_ga4_normalized,
    _key,
    _provider_report,
    _row,
    _write_contract,
    installed_join_retention,  # noqa: F401
)
from typer.testing import CliRunner

import adhelm.join_cli as join_cli_module
from adhelm.campaign_joins import (
    CampaignJoinContract,
    apply_join_map,
    resolve_campaign_joins,
    validate_predecessor,
)
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import build_side_by_side_comparison
from adhelm.projections import export_verified
from adhelm.saved_campaign_joins import (
    JOIN_MAP_RECORD_KIND,
    JOIN_MAP_STREAM,
    join_map_catalog,
    latest_join_map,
    retain_join_map,
)
from adhelm.workflows import persist_report

runner = CliRunner()

# An ordinary advertising campaign name.  GA4 returns it verbatim, the GA4 read
# boundary retains it verbatim, and no part of the reporting contract forbids
# it.  It happens to contain the substring "lift".
_ORDINARY_NAME = "brand-lift-q4"


def _retain_ga4_report(ledger: Ledger, name: str) -> str:
    report = _aggregate_report(((name, "google", "cpc", 40, 3),), ledger)
    return str(persist_report(ledger, report.to_normalized_report())["record_digest"])


# ---------------------------------------------------------------------------
# 1. The derived-measure guard must not censor customer and GA4 data values
# ---------------------------------------------------------------------------


def test_a_retained_join_map_with_an_ordinary_campaign_name_stays_reviewable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """`measurement join show` must render a map it was allowed to retain.

    `refuse_cross_source_derivation` (`src/adhelm/campaign_joins.py:121-162`)
    substring-scans every *string value* in the emitted payload, not only the
    field names the product itself chooses.  `measurement join show`
    (`src/adhelm/join_cli.py:266`) puts the GA4 `session_campaign_name` of every
    resolution into that payload, so a campaign literally named
    "brand-lift-q4" makes the retained record permanently unreadable with
    `CROSS_SOURCE_DERIVATION_PROHIBITED`.  The same holds for "cpa", "ratio",
    "blended", and "conversion_rate" inside customer text.

    DoD section 6 forbids the *product* from presenting a derived or causal
    measure.  It does not authorise refusing a customer's own campaign name,
    and the GA4 read boundary retains that name without complaint.
    """
    monkeypatch.setattr(join_cli_module, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    report_digest = _retain_ga4_report(ledger, _ORDINARY_NAME)
    contract = _contract(entries=(_entry(ga4_keys=(_key(name=_ORDINARY_NAME),)),))
    path = _write_contract(tmp_path, contract)
    base = ["--data-dir", str(tmp_path), "measurement", "join"]

    created = runner.invoke(
        _cli_app(),
        [*base, "create", "--input", str(path), "--ga4-report", report_digest, "--retain"],
    )
    assert created.exit_code == 0, created.stdout
    record_digest = json.loads(created.stdout)["data"]["record_digest"]

    shown = runner.invoke(_cli_app(), [*base, "show", record_digest])

    assert shown.exit_code == 0, shown.stdout
    data = json.loads(shown.stdout)["data"]
    assert data["rows"][0]["join_status"] == "EXPLICIT_MAPPING"
    assert data["causal_claim"] is False


def test_join_create_reports_an_ordinary_campaign_name_instead_of_refusing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflict on an ordinarily named campaign must be reportable.

    The same substring scan runs over the `conflicts` and
    `heuristic_proposals` blocks of the `measurement join create` receipt
    (`src/adhelm/join_cli.py:139-150`), so the command cannot report the very
    conflict the customer needs to resolve.
    """
    monkeypatch.setattr(join_cli_module, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    report_digest = _retain_ga4_report(ledger, "q4-cpa-test")
    contract = _contract(
        entries=(
            _entry(ga4_keys=(_key(name="q4-cpa-test"),)),
            _entry(provider_campaign_ref=_OTHER_CAMPAIGN, ga4_keys=(_key(name="q4-cpa-test"),)),
        )
    )
    path = _write_contract(tmp_path, contract)

    result = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "join",
            "create",
            "--input",
            str(path),
            "--ga4-report",
            report_digest,
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["status_counts"]["CONFLICT"] == 1
    assert len(data["conflicts"]) == 1


# ---------------------------------------------------------------------------
# 2. Retention must not happen before the command decides it will succeed
# ---------------------------------------------------------------------------


def test_join_create_never_retains_a_record_it_then_fails_closed_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """AGENTS rule 20: a capability fails explicitly *before* side effects.

    `src/adhelm/join_cli.py:146-150` appends the `CAMPAIGN_JOIN_MAP_RETAINED`
    record and only afterwards runs `refuse_cross_source_derivation` on the
    receipt.  The command therefore emits `FAILED_CLOSED` while the ledger has
    already grown a durable record the receipt never named.
    """
    monkeypatch.setattr(join_cli_module, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    report_digest = _retain_ga4_report(ledger, "q4-cpa-test")
    contract = _contract(
        entries=(
            _entry(ga4_keys=(_key(name="q4-cpa-test"),)),
            _entry(provider_campaign_ref=_OTHER_CAMPAIGN, ga4_keys=(_key(name="q4-cpa-test"),)),
        )
    )
    path = _write_contract(tmp_path, contract)

    result = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "join",
            "create",
            "--input",
            str(path),
            "--ga4-report",
            report_digest,
            "--retain",
        ],
    )

    if result.exit_code != 0:
        assert json.loads(result.stdout)["outcome"] == "FAILED_CLOSED"
        assert join_map_catalog(ledger)["total_count"] == 0, (
            "a failed-closed join create left a retained record behind"
        )


# ---------------------------------------------------------------------------
# 3. The retained chain must not fork
# ---------------------------------------------------------------------------


def test_a_second_version_one_join_map_for_one_contract_is_refused_not_forked(
    tmp_path: Path,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """One contract reference must have exactly one version-one declaration.

    `join_cli.create` only checks a predecessor when `version > 1`
    (`src/adhelm/join_cli.py:122`) and `retain_join_map`
    (`src/adhelm/saved_campaign_joins.py:47-58`) appends without any chain
    validation, despite its docstring claiming "the ledger validates the
    version/supersedes chain while holding its stream lock, so a competing
    registration cannot fork it".  Two divergent version-one maps are
    therefore both retained, `latest_join_map` silently returns whichever the
    scan met first, and the second declaration is invisible to every later
    `validate_predecessor` call.
    """
    ledger = Ledger(tmp_path)
    first = resolve_campaign_joins(_contract(), (_row(),))
    second = resolve_campaign_joins(
        _contract(entries=(_entry(provider_campaign_ref=_OTHER_CAMPAIGN),)), (_row(),)
    )
    assert first.content_digest != second.content_digest
    retain_join_map(ledger, first)

    with pytest.raises(AdhelmError, match="JOIN_MAP_CHAIN_INVALID|already"):
        retain_join_map(ledger, second)


def test_a_forked_chain_never_hides_a_retained_declaration(
    tmp_path: Path,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """Whatever the fork policy is, selection may not be scan-order defined."""
    ledger = Ledger(tmp_path)
    first = resolve_campaign_joins(_contract(), (_row(),))
    second = resolve_campaign_joins(
        _contract(entries=(_entry(provider_campaign_ref=_OTHER_CAMPAIGN),)), (_row(),)
    )
    retain_join_map(ledger, first)
    try:
        retain_join_map(ledger, second)
    except AdhelmError:
        pytest.skip("the fork is refused; covered by the sibling test")

    latest = latest_join_map(ledger, first.contract.contract_id)
    assert latest is not None
    assert latest.content_digest == second.content_digest, (
        "a shadowed version-one declaration is unreachable through the chain selector"
    )


# ---------------------------------------------------------------------------
# 4. A restatement must stay bound to the same GA4 property
# ---------------------------------------------------------------------------


def test_a_superseding_contract_cannot_rebind_the_join_to_another_ga4_property(
    tmp_path: Path,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """`validate_predecessor` checks id/version/digest only.

    `src/adhelm/campaign_joins.py:862-885` never compares
    `ga4_property.property_ref`, its timezone, or the bound provider accounts
    with the predecessor's, so version two of a retained contract can move the
    whole declared join to a different GA4 property while still presenting an
    unbroken content-addressed chain.
    """
    ledger = Ledger(tmp_path)
    first = resolve_campaign_joins(_contract(), (_row(),))
    retain_join_map(ledger, first)
    successor = CampaignJoinContract.model_validate_json(
        canonical_bytes(
            {
                **_contract(version=2, supersedes=first.content_digest).model_dump(mode="json"),
                "ga4_property": {
                    "schema_version": 1,
                    "property_ref": "ga4.property.999",
                    "property_timezone": "Europe/Berlin",
                },
            }
        )
    )

    # The rendered diagnostic carries the code; the message states the exact
    # invariant.  Asserting both is stricter than matching either alone.
    with pytest.raises(AdhelmError) as refusal:
        validate_predecessor(first, successor)
    assert refusal.value.code == "JOIN_MAP_CHAIN_INVALID"
    assert "keep its GA4 property reference and timezone" in refusal.value.message


# ---------------------------------------------------------------------------
# 5. A real timezone disagreement between the two retained reports
# ---------------------------------------------------------------------------


def test_reports_with_different_timezones_are_not_presented_as_aligned() -> None:
    """The side-by-side boundary status must describe the reports being shown.

    `build_side_by_side_comparison`
    (`src/adhelm/measurement_comparison.py:1592,1605`) copies
    `join_map.boundary_status`, which is derived only from the *declared*
    account timezones in the contract.  When the retained provider report was
    actually read in another timezone than the retained GA4 report, the row
    still says `ALIGNED` and carries no `TIMEZONE_MISMATCH` quality status,
    even though it prints two different timezone fields.  DoD section 2
    requires timezone and date boundaries to be explicit.
    """
    join_map = resolve_campaign_joins(_contract(), (_row(),))
    provider = _provider_report()
    shifted = provider.model_copy(
        update={
            "envelope": provider.envelope.model_copy(
                update={"report_timezone": "Europe/Berlin", "source_timezone": "Europe/Berlin"}
            )
        }
    )

    comparison = build_side_by_side_comparison(
        join_map=join_map, provider_report=shifted, ga4_report=_joined_ga4_normalized(join_map)
    )

    row = comparison.rows[0]
    assert row.provider_timezone != row.ga4_timezone
    assert comparison.boundary_status == "TIMEZONE_MISMATCH"
    assert "TIMEZONE_MISMATCH" in row.data_quality_statuses


# ---------------------------------------------------------------------------
# 6. Exports must not ship a customer-supplied provider campaign name
# ---------------------------------------------------------------------------


def test_history_export_does_not_ship_a_customer_supplied_campaign_name_in_cleartext(
    tmp_path: Path,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """No connector retains a provider campaign name; the export now emits one.

    The join contract is the first record in this product to retain an
    advertising campaign *name*, and `projections._is_reference_field`
    (`src/adhelm/projections.py:165-173`) pseudonymizes only `*_ref` / `*_id`
    style fields.  `provider_campaign_name` therefore leaves the local
    boundary verbatim in `records.jsonl` and `record-nodes.csv`.
    """
    ledger = Ledger(tmp_path / "data")
    private_name = "Acme Q4 Brand Push - Private"
    contract = _contract(entries=(_entry(ga4_keys=(_key(),), provider_campaign_name=private_name),))
    retain_join_map(ledger, resolve_campaign_joins(contract, (_row(),)))

    export_verified(ledger, tmp_path / "out", opaque_key=b"k" * 32, include_marketing_text=False)

    assert private_name not in (tmp_path / "out" / "records.jsonl").read_text()
    assert private_name not in (tmp_path / "out" / "record-nodes.csv").read_text()


# ---------------------------------------------------------------------------
# 7. The retained record must be bound to its property and accounts
# ---------------------------------------------------------------------------


def test_a_retained_join_map_binds_its_ga4_property_in_the_ledger_envelope(
    tmp_path: Path,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """Every comparable retained kind pins its envelope; this one does not.

    `Ledger._validate_measurement_binding` (`src/adhelm/ledger.py:2375-2411`)
    and `_validate_candidate_binding` refuse a record whose envelope provider
    and `account_ref` disagree with the payload.  `CAMPAIGN_JOIN_MAP_RETAINED`
    has no such validator, and `retain_join_map`
    (`src/adhelm/saved_campaign_joins.py:56-58`) passes no `account_ref` at all,
    so a join map for one GA4 property is accepted under an unrelated Meta Ads
    account envelope and is indistinguishable in history from a bound one.
    """
    ledger = Ledger(tmp_path)
    join_map = resolve_campaign_joins(_contract(), (_row(),))

    with pytest.raises(AdhelmError):
        ledger.append(
            JOIN_MAP_STREAM,
            JOIN_MAP_RECORD_KIND,
            join_map.model_dump(mode="json"),
            provider="meta_ads",
            account_ref="meta_ads.account.unrelated-private",
        )

    record = retain_join_map(ledger, join_map)
    stored = next(
        item
        for item in ledger.checkpoint()["records_by_stream"][JOIN_MAP_STREAM]
        if item["record_digest"] == record["record_digest"]
    )
    assert stored["account_ref"] == _PROPERTY


# ---------------------------------------------------------------------------
# 8. Proposal provenance must survive into the retained-record review
# ---------------------------------------------------------------------------


def test_join_show_surfaces_the_customer_supplied_name_provenance_of_a_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """`measurement join show` is the review surface for a retained map.

    It emits `join_map.summary()` plus the resolution rows
    (`src/adhelm/join_cli.py:231-255`) and drops `join_map.proposals` entirely.
    A `HEURISTIC` row is therefore reviewed with a candidate campaign
    reference and no statement that the only basis is a name the *customer*
    supplied, which local history does not retain
    (`CUSTOMER_SUPPLIED_PROVIDER_CAMPAIGN_NAME_NOT_RETAINED_HISTORY`).
    """
    monkeypatch.setattr(join_cli_module, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    report_digest = _retain_ga4_report(ledger, "winback")
    contract = _contract(
        entries=(
            _entry(provider_campaign_ref=_CAMPAIGN, ga4_keys=(), provider_campaign_name="winback"),
        )
    )
    path = _write_contract(tmp_path, contract)
    base = ["--data-dir", str(tmp_path), "measurement", "join"]

    created = runner.invoke(
        _cli_app(),
        [*base, "create", "--input", str(path), "--ga4-report", report_digest, "--retain"],
    )
    assert created.exit_code == 0, created.stdout
    record_digest = json.loads(created.stdout)["data"]["record_digest"]

    shown = runner.invoke(_cli_app(), [*base, "show", record_digest])
    assert shown.exit_code == 0, shown.stdout

    assert "CUSTOMER_SUPPLIED_PROVIDER_CAMPAIGN_NAME_NOT_RETAINED_HISTORY" in shown.stdout


# ---------------------------------------------------------------------------
# 9. The shared input-file hardening battery must cover the new boundary
# ---------------------------------------------------------------------------


def test_the_input_file_hardening_battery_covers_measurement_join_create() -> None:
    """`tests/test_command_input_files.py` is the regression net for `--input`.

    `measurement join create --input` behaves correctly today, but it is not
    in that hand-maintained list, so no test would notice if it stopped
    rejecting a symlink, a FIFO, an oversized file, or duplicate JSON keys.
    """
    from test_command_input_files import _COMMANDS

    assert "measurement join create" in {command.name for command in _COMMANDS}


# ---------------------------------------------------------------------------
# 10. Shipped documents must not claim a capability the branch already has
# ---------------------------------------------------------------------------


def test_the_shipped_docs_do_not_claim_the_ledger_lacks_the_join_record_kind() -> None:
    """AGENTS rule 20: no fabricated or stale status statements.

    Commit 3f792ae registered `CAMPAIGN_JOIN_MAP_RETAINED` in
    `src/adhelm/ledger.py`, but `docs/implementation/ga4-campaign-joins.md`,
    `docs/capability-matrix.md` and `docs/implementation/traceability.json`
    still tell a reader that retention is unavailable and that the additive
    hunk has to be applied.
    """
    root = Path(__file__).resolve().parents[1]
    contract = (root / "docs" / "implementation" / "ga4-campaign-joins.md").read_text()
    matrix = (root / "docs" / "capability-matrix.md").read_text()
    traceability = (root / "docs" / "implementation" / "traceability.json").read_text()

    assert "does not yet accept it" not in contract
    assert "Retention needs the additive `CAMPAIGN_JOIN_MAP_RETAINED` ledger" not in matrix
    assert "still needs the additive CAMPAIGN_JOIN_MAP_RETAINED ledger record kind" not in (
        traceability
    )


# ---------------------------------------------------------------------------
# 11. A second join map over one report is a typed refusal, not a raw error
# ---------------------------------------------------------------------------


def test_resolving_one_report_with_a_second_join_map_is_a_typed_refusal() -> None:
    """`apply_join_map` is exported; a mixed report must fail as `AdhelmError`.

    Re-applying a different map to an already-resolved report leaves rows from
    both maps, which `Ga4AggregateReport` refuses with a raw pydantic
    `ValidationError` out of `src/adhelm/campaign_joins.py:920-935` instead of
    the typed, coded refusal AGENTS rule 20 requires.
    """
    report = _aggregate_report(
        (("brand-search", "google", "cpc", 40, 3), ("winback", "bing", "organic", 5, 0))
    )
    first = resolve_campaign_joins(_contract(), (_row(), _row("winback", "bing", "organic")))
    joined = apply_join_map(report, first)
    second = resolve_campaign_joins(
        _contract(entries=(_entry(ga4_keys=(_key(name="winback"),)),)),
        (_row("winback", "bing", "organic"),),
    )

    try:
        apply_join_map(joined, second)
    except AdhelmError:
        return
    except ValidationError as exc:  # pragma: no cover - the defect being pinned
        raise AssertionError("a mixed join map raised a raw ValidationError") from exc
    raise AssertionError("a report resolved by two join maps was accepted")
