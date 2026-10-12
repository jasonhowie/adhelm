"""Second independent review of ticket M00.2, over the repair at `ab6e0a1`.

The thirteen findings of `tests/test_ga4_campaign_joins_review.py` are fixed
and that file now passes.  These two tests fail on `ab6e0a1` and state the
behaviour still owed.  No product code is changed here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_ga4_campaign_joins import (
    _aggregate_report,
    _cli_app,
    _contract,
    _entry,
    _write_contract,
    installed_join_retention,  # noqa: F401
)
from typer.testing import CliRunner

from adhelm import join_cli as join_cli_module
from adhelm.config import update_configuration
from adhelm.ledger import Ledger
from adhelm.workflows import persist_report

runner = CliRunner()


def _bound(root: Path, value: int) -> None:
    update_configuration(
        root,
        lambda current: current.model_copy(
            update={"output": current.output.model_copy(update={"max_output_bytes": value})}
        ),
    )


def _retained_report(ledger: Ledger, names: tuple[str, ...]) -> str:
    report = _aggregate_report(tuple((name, "google", "cpc", 10, 1) for name in names), ledger)
    return str(persist_report(ledger, report.to_normalized_report())["record_digest"])


# ---------------------------------------------------------------------------
# 1. Every retained heuristic proposal must be reviewable
# ---------------------------------------------------------------------------


def test_every_retained_heuristic_proposal_is_reachable_through_join_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """The repair added proposals to `show` behind a hard, unpageable cap.

    `src/adhelm/join_cli.py` builds
    ``"heuristic_proposals": _proposal_rows(join_map)[: preferences.max_rows]``.
    That slice ignores both ``--cursor`` and ``--limit``: the resolution rows
    beside it are paged with ``next_cursor``/``total_count``, but the proposal
    list is always the first ``max_rows`` (default 50) entries.  A join map may
    hold up to ``_MAX_ROWS`` proposals, so proposal 51 and beyond cannot be
    reviewed through any invocation of any command once the map is retained.

    ``heuristic_proposal_count`` makes the truncation visible rather than
    silent, so this is a reviewability gap and not a fabricated result -- but
    it is the same gap the previous round closed for proposals in general.
    """
    monkeypatch.setattr(join_cli_module, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    names = tuple(f"camp-{index:03d}" for index in range(60))
    report_digest = _retained_report(ledger, names)
    contract = _contract(
        entries=tuple(
            _entry(
                provider_campaign_ref=f"google_ads.campaign.1234567890.{index:03d}",
                ga4_keys=(),
                provider_campaign_name=name,
            )
            for index, name in enumerate(names)
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

    seen: set[str] = set()
    cursor: str | None = "0"
    while cursor is not None:
        page = runner.invoke(_cli_app(), [*base, "show", record_digest, "--cursor", cursor])
        assert page.exit_code == 0, page.stdout
        envelope = json.loads(page.stdout)
        seen.update(
            item["session_campaign_name"] for item in envelope["data"]["heuristic_proposals"]
        )
        cursor = envelope["next_cursor"]
        total = envelope["data"]["heuristic_proposal_count"]

    assert total == len(names)
    assert seen == set(names), f"{len(names) - len(seen)} retained proposals are unreachable"


# ---------------------------------------------------------------------------
# 2. The placeholder preflight must bound the receipt exactly
# ---------------------------------------------------------------------------


def test_the_retain_preflight_bounds_the_receipt_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_join_retention: None,  # noqa: F811
) -> None:
    """The `"0" * 64` placeholder matches the digest, but `persisted` does not.

    `create` preflights the receipt with ``"persisted": False`` and emits it
    with ``"persisted": True``, so the measured envelope is exactly one byte
    larger than the emitted one.  A receipt that would fit the configured bound
    precisely is therefore refused with ``OUTPUT_LIMIT_EXCEEDED``.

    The direction is fail-safe -- nothing can fail after the durable append --
    so this is a NOTE, not a correctness defect.  Preflighting with
    ``"persisted": retain`` makes the bound exact.
    """
    monkeypatch.setattr(join_cli_module, "export_key", lambda: b"p" * 32)
    measured = tmp_path / "measure"
    measured.mkdir()
    reference = Ledger(measured)
    report_digest = _retained_report(reference, ("brand-search",))
    path = _write_contract(measured, _contract())
    first = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(measured),
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
    assert first.exit_code == 0, first.stdout
    emitted = len(first.stdout)

    exact = tmp_path / "exact"
    exact.mkdir()
    ledger = Ledger(exact)
    exact_report = _retained_report(ledger, ("brand-search",))
    exact_path = _write_contract(exact, _contract())
    _bound(exact, emitted)

    result = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(exact),
            "measurement",
            "join",
            "create",
            "--input",
            str(exact_path),
            "--ga4-report",
            exact_report,
            "--retain",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(result.stdout) <= emitted
