"""Provider release labels come from committed pilot records and nothing else."""

from __future__ import annotations

import json
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_pilot_records import sealed_record
from typer.testing import CliRunner

import adhelm.release_labels as release_labels
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import Configuration, Profile, ResultEnvelope
from adhelm.pilot_records import RELEASE_PROVIDERS, PilotRecord, pilot_record_bytes
from adhelm.release_labels import (
    ACCEPTED,
    EXPERIMENTAL,
    LABEL_MEANINGS,
    LIVE_PILOTED,
    labels_for_output,
    pilot_record_inventory,
    provider_labels,
    release_label,
)
from adhelm.release_labels import pilot_records_directory as shipped_directory

_RUNNER = CliRunner()
_REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def records(tmp_pilot_records: Path) -> Path:
    """A pilot record directory the loader reads in place of the shipped one."""
    return tmp_pilot_records


def _write(directory: Path, name: str, record: PilotRecord) -> Path:
    path = directory / name
    path.write_bytes(pilot_record_bytes(record))
    return path


def _labels() -> dict[str, str]:
    return {item.provider: item.label for item in provider_labels()}


def _states() -> dict[str, str]:
    return {item.file: item.state for item in pilot_record_inventory().statuses}


def _invoke(*arguments: str, directory: Path) -> dict[str, Any]:
    result = _RUNNER.invoke(app, ["--data-dir", str(directory), *arguments])
    envelope: dict[str, Any] = json.loads(result.output)
    return envelope


# --- no records ---------------------------------------------------------------


def test_with_no_records_every_released_provider_is_experimental() -> None:
    labels = provider_labels()

    assert [item.provider for item in labels] == list(RELEASE_PROVIDERS)
    assert len(labels) == 8
    assert {item.label for item in labels} == {EXPERIMENTAL}
    assert {item.meaning for item in labels} == {LABEL_MEANINGS[EXPERIMENTAL]}
    assert pilot_record_inventory().statuses == ()


def test_an_empty_directory_is_the_same_as_no_records(records: Path) -> None:
    (records / "README.md").write_text("Procedure, not a record.\n")

    assert set(_labels().values()) == {EXPERIMENTAL}
    assert pilot_record_inventory().statuses == ()


# --- a valid record -----------------------------------------------------------


def test_a_valid_record_for_this_build_labels_only_its_provider_live_piloted(
    records: Path,
) -> None:
    record = sealed_record("linkedin_ads")
    _write(records, "linkedin_ads.json", record)

    labels = _labels()

    assert labels["linkedin_ads"] == LIVE_PILOTED
    assert [name for name, label in labels.items() if label == LIVE_PILOTED] == ["linkedin_ads"]
    assert _states() == {"linkedin_ads.json": ACCEPTED}
    entry = release_label("linkedin_ads")
    assert entry is not None
    assert entry.record == record
    assert entry.meaning == LABEL_MEANINGS[LIVE_PILOTED]


@pytest.mark.parametrize("provider", RELEASE_PROVIDERS)
def test_each_released_provider_can_be_piloted_independently(provider: str, records: Path) -> None:
    _write(records, f"{provider}.json", sealed_record(provider))

    assert {name for name, label in _labels().items() if label == LIVE_PILOTED} == {provider}


def test_a_mismatch_with_the_provider_ui_still_qualifies_and_is_recorded(records: Path) -> None:
    """The release definition accepts "an explicit recorded mismatch" as pilot evidence."""
    _write(records, "reddit_ads.json", sealed_record("reddit_ads", ui_comparison="MISMATCH"))

    entry = release_label("reddit_ads")

    assert entry is not None
    assert entry.label == LIVE_PILOTED
    assert entry.record is not None
    assert entry.record.ui_comparison == "MISMATCH"


def test_the_most_recent_valid_record_labels_the_provider(records: Path) -> None:
    older = sealed_record("ga4", pilot_date=date(2026, 10, 11))
    newer = sealed_record("ga4", pilot_date=date(2026, 10, 14))
    _write(records, "ga4-a.json", newer)
    _write(records, "ga4-b.json", older)

    entry = release_label("ga4")

    assert entry is not None
    assert entry.record == newer


def test_an_unusable_newer_record_does_not_hide_an_older_valid_one(records: Path) -> None:
    older = sealed_record("ga4", pilot_date=date(2026, 10, 11))
    _write(records, "a-older.json", older)
    stale = sealed_record("ga4", pilot_date=date(2026, 10, 14), cli_version="0.0.9")
    _write(records, "b-newer.json", stale)

    entry = release_label("ga4")

    assert entry is not None
    assert entry.record == older
    assert _states() == {
        "a-older.json": ACCEPTED,
        "b-newer.json": "PILOT_RECORD_VERSION_MISMATCH",
    }


# --- records that must not label a provider -------------------------------------


def _tampered(record: PilotRecord, **changes: Any) -> bytes:
    raw = json.loads(pilot_record_bytes(record))
    raw.update(changes)
    return json.dumps(raw).encode()


def test_a_record_for_another_build_version_leaves_the_provider_experimental(
    records: Path,
) -> None:
    _write(records, "old.json", sealed_record("gtm", cli_version="0.0.9"))

    assert set(_labels().values()) == {EXPERIMENTAL}
    assert _states() == {"old.json": "PILOT_RECORD_VERSION_MISMATCH"}


def test_an_edited_record_fails_its_digest(records: Path) -> None:
    record = sealed_record("meta_ads")
    (records / "meta.json").write_bytes(_tampered(record, ui_comparison="MISMATCH"))

    assert set(_labels().values()) == {EXPERIMENTAL}
    assert _states() == {"meta.json": "PILOT_RECORD_DIGEST_MISMATCH"}


def test_a_record_moved_to_another_provider_fails_its_digest_and_its_plan(
    records: Path,
) -> None:
    record = sealed_record("reddit_ads")
    (records / "moved.json").write_bytes(_tampered(record, provider="gtm"))

    assert set(_labels().values()) == {EXPERIMENTAL}
    assert _states() == {"moved.json": "PILOT_RECORD_INVALID"}


@pytest.mark.parametrize(
    "mutation",
    [
        {"account_name": "Example Shop"},
        {"source_commit": "main"},
        {"schema_version": 2},
        {"schema_version": 1.0},
        {"record_kind": "PILOT"},
        {"report_window": None},
        {"steps": []},
        {"audit_coverage": []},
        {"record_digest": "not-a-digest"},
    ],
)
def test_a_record_outside_the_closed_schema_is_invalid(
    mutation: dict[str, Any], records: Path
) -> None:
    record = sealed_record("tiktok_ads")
    (records / "bad.json").write_bytes(_tampered(record, **mutation))

    assert set(_labels().values()) == {EXPERIMENTAL}
    assert _states() == {"bad.json": "PILOT_RECORD_INVALID"}


def test_a_record_missing_a_field_is_invalid(records: Path) -> None:
    raw = json.loads(pilot_record_bytes(sealed_record("openai_ads")))
    del raw["ui_comparison"]
    (records / "missing.json").write_text(json.dumps(raw))

    assert _states() == {"missing.json": "PILOT_RECORD_INVALID"}
    assert set(_labels().values()) == {EXPERIMENTAL}


@pytest.mark.parametrize(
    "content",
    [b"", b"not json", b"[]", b'{"provider": "ga4", "provider": "gtm"}', b"\xff\xfe"],
)
def test_text_that_is_not_one_json_record_is_invalid(content: bytes, records: Path) -> None:
    (records / "junk.json").write_bytes(content)

    assert _states() == {"junk.json": "PILOT_RECORD_INVALID"}
    assert set(_labels().values()) == {EXPERIMENTAL}


def test_an_oversized_record_is_refused_unread(records: Path) -> None:
    (records / "big.json").write_bytes(b" " * 70_000)

    assert _states() == {"big.json": "PILOT_RECORD_TOO_LARGE"}


def test_a_link_or_directory_named_like_a_record_is_not_followed(
    records: Path, tmp_path: Path
) -> None:
    valid = _write(tmp_path, "outside.json", sealed_record("google_ads"))
    (records / "link.json").symlink_to(valid)
    (records / "directory.json").mkdir()

    assert _states() == {
        "directory.json": "PILOT_RECORD_UNREADABLE",
        "link.json": "PILOT_RECORD_UNREADABLE",
    }
    assert set(_labels().values()) == {EXPERIMENTAL}


# --- only a record file can change a label ---------------------------------------


def test_a_label_cannot_be_supplied_to_an_output(tmp_path: Path) -> None:
    forged = {
        "provider": "google_ads",
        "label": "LIVE_PILOTED",
        "meaning": LABEL_MEANINGS[LIVE_PILOTED],
        "pilot_record_digest": "c" * 64,
    }

    envelope = ResultEnvelope(
        tool_version="x",
        command="report google-ads",
        outcome="OK",
        provider_labels=[forged],  # type: ignore[arg-type]
    )

    assert [(item.provider, item.label) for item in envelope.provider_labels] == [
        ("google_ads", EXPERIMENTAL)
    ]
    assert envelope.provider_labels[0].pilot_record_digest is None


def test_configuration_cannot_declare_a_label(tmp_path: Path) -> None:
    save_configuration(tmp_path, Configuration())
    path = tmp_path / "config.json"
    document = json.loads(path.read_text())
    document["provider_labels"] = {"google_ads": "LIVE_PILOTED"}
    path.write_text(json.dumps(document))

    envelope = _invoke("doctor", directory=tmp_path)

    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "CONFIGURATION_INVALID"
    assert "provider_labels" not in envelope


def test_the_label_source_is_the_pilot_record_directory_the_wheel_ships() -> None:
    """The wheel carries `docs/pilots` where an installation reads its records."""
    force_include = tomllib.loads((_REPOSITORY / "pyproject.toml").read_text())["tool"]["hatch"][
        "build"
    ]["targets"]["wheel"]["force-include"]

    assert force_include["docs/pilots"] == f"adhelm/{release_labels._INSTALLED_DIRECTORY}"


def _package_at(monkeypatch: pytest.MonkeyPatch, package: Path) -> None:
    package.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(release_labels, "__file__", str(package / "release_labels.py"))


def test_an_installed_wheel_reads_the_records_it_ships_beside_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed layout: `site-packages/adhelm/pilot_record_files`, no repository around."""
    package = tmp_path / "site-packages" / "adhelm"
    (package / release_labels._INSTALLED_DIRECTORY).mkdir(parents=True)
    _package_at(monkeypatch, package)

    assert shipped_directory() == (package / release_labels._INSTALLED_DIRECTORY).resolve()


def test_a_source_checkout_reads_docs_pilots_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "pyproject.toml").parent.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\n")
    _package_at(monkeypatch, checkout / "src" / "adhelm")

    # A checkout without the directory has no records, not an error.
    assert shipped_directory() is None
    (checkout / "docs" / "pilots").mkdir(parents=True)
    assert shipped_directory() == (checkout / "docs" / "pilots").resolve()


def test_a_package_outside_a_checkout_and_without_shipped_records_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package_at(monkeypatch, tmp_path / "site-packages" / "adhelm")
    (tmp_path / "docs" / "pilots").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    # Not under `src/`, so the unrelated `docs/pilots` above is never mistaken for records.
    assert shipped_directory() is None


def test_a_record_that_cannot_be_read_is_reported_not_dropped(
    records: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = _write(records, "ga4.json", sealed_record("ga4"))
    real = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == unreadable:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    pilot_record_inventory.cache_clear()

    assert _states() == {"ga4.json": "PILOT_RECORD_UNREADABLE"}
    assert _labels()["ga4"] == EXPERIMENTAL


def test_the_shipped_directory_is_the_committed_pilots_directory(
    committed_pilot_records: Path,
) -> None:
    # `shipped_directory` is the loader's own function, bound at import before the autouse
    # fixture withholds it, so this pins the shared fixture's path to the real one.
    assert shipped_directory() == committed_pilot_records
    assert committed_pilot_records == _REPOSITORY / "docs" / "pilots"
    states = {item.state for item in pilot_record_inventory().statuses}
    # Every committed record is sound; one for an earlier build is stale, not forged.
    assert states <= {ACCEPTED, "PILOT_RECORD_VERSION_MISMATCH"}


# --- which outputs name a provider ---------------------------------------------------


@pytest.mark.parametrize(
    ("command", "data", "named"),
    [
        ("report google-ads", {}, {"google_ads"}),
        ("report ga4", {}, {"ga4"}),
        ("measurement gtm", {}, {"gtm"}),
        ("measurement gtm-graph", {}, {"gtm"}),
        ("measurement ga4-admin", {}, {"ga4"}),
        ("compile openai-ads", {}, {"openai_ads"}),
        ("compile google-ads", {}, {"google_ads"}),
        # Every command group names a provider the same way, including those about
        # remote changes this build refuses; the label states evidence, never a capability.
        ("plan google-ads-campaign-pause create", {}, {"google_ads"}),
        ("plan meta-campaign-pause show", {}, {"meta_ads"}),
        ("plan openai-pause", {}, {"openai_ads"}),
        ("execute plan", {"provider": "google_ads"}, {"google_ads"}),
        ("execute meta-campaign-pause", {}, {"meta_ads"}),
        ("execute openai-pause", {}, {"openai_ads"}),
        ("recover attempt", {"provider": "ga4"}, {"ga4"}),
        ("artifacts meta-campaign-pause", {}, {"meta_ads"}),
        ("auth meta-token status", {}, {"meta_ads"}),
        ("auth meta-token remove-local", {}, {"meta_ads"}),
        ("auth linkedin-token status", {}, {"linkedin_ads"}),
        ("auth reddit-token status", {}, {"reddit_ads"}),
        ("auth tiktok-token status", {}, {"tiktok_ads"}),
        ("auth openai-key status", {}, {"openai_ads"}),
        ("measurement openai-conversions", {}, {"openai_ads"}),
        # `google-oauth` is shared by three providers, so the command path names none.
        ("auth google-oauth status", {}, set()),
        ("history verify", {"provider": "meta_ads"}, {"meta_ads"}),
        ("history tail", {"records": [{"provider": "ga4_admin"}]}, {"ga4"}),
        ("history tail", {"a": {"b": [{"c": ({"provider": "reddit_ads"},)}]}}, {"reddit_ads"}),
        ("measurement show", {"provider": "gtm_web_inventory"}, {"gtm"}),
        (
            "doctor",
            {"providers": [{"provider": "ga4"}, {"provider": "tiktok_ads"}]},
            {"ga4", "tiktok_ads"},
        ),
        ("history verify", {}, set()),
        ("version", {"provider": "hubspot"}, set()),
        ("version", {"provider": "salesforce"}, set()),
        ("version", {"provider": 7}, set()),
        ("version", {"note": "google_ads"}, set()),
    ],
)
def test_an_output_is_labelled_for_the_released_providers_it_names(
    command: str, data: dict[str, Any], named: set[str]
) -> None:
    labels = labels_for_output(command, data)

    assert {item.provider for item in labels} == named
    assert len(labels) == len(named)
    assert {item.label for item in labels} <= {EXPERIMENTAL}
    assert all(item.meaning == LABEL_MEANINGS[EXPERIMENTAL] for item in labels)


def test_an_output_carries_the_label_and_its_meaning_in_its_envelope(tmp_path: Path) -> None:
    envelope = _invoke(
        "report",
        "reddit-ads",
        "missing-profile",
        "--input",
        str(tmp_path / "query.json"),
        directory=tmp_path,
    )

    # Even a refusal that names a provider states its label.
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["provider_labels"] == [
        {
            "provider": "reddit_ads",
            "label": EXPERIMENTAL,
            "meaning": LABEL_MEANINGS[EXPERIMENTAL],
        }
    ]
    # An output that names no released provider is unchanged by labels.
    assert "provider_labels" not in _invoke("version", directory=tmp_path)


@pytest.mark.parametrize(
    ("arguments", "provider"),
    [
        (("plan", "google-ads-campaign-pause", "create", "--input", "absent.json"), "google_ads"),
        (("plan", "meta-campaign-pause", "create", "--input", "absent.json"), "meta_ads"),
        (("execute", "meta-campaign-pause", "--input", "absent.json"), "meta_ads"),
        (("execute", "openai-pause", "--input", "absent.json"), "openai_ads"),
        (("execute", "ga4-key-event-create", "--input", "absent.json"), "ga4"),
        (("execute", "plan", "0" * 64), None),
        (("recover", "attempt", "0" * 64), None),
    ],
)
def test_refusals_from_the_plan_execute_and_recover_groups_carry_the_label(
    arguments: tuple[str, ...], provider: str | None, tmp_path: Path
) -> None:
    envelope = _invoke(*arguments, directory=tmp_path)

    assert envelope["outcome"] == "FAILED_CLOSED"
    named = {item["provider"] for item in envelope.get("provider_labels", [])}
    # The generic leaves name no provider until a retained plan does.
    assert named == ({provider} if provider is not None else set())
    for item in envelope.get("provider_labels", []):
        assert item["meaning"] == LABEL_MEANINGS[EXPERIMENTAL]


def test_only_two_closed_receipts_sized_to_the_minimum_bound_carry_no_label() -> None:
    assert release_labels.BOUND_SIZED_RECEIPT_OUTCOMES == {
        "EXECUTION_RESULT",
        "GA4_KEY_EVENT_CREATE_PLAN_RETAINED_NO_EXECUTION_AUTHORITY",
    }
    for outcome in release_labels.BOUND_SIZED_RECEIPT_OUTCOMES:
        # The exempt outcome is the receipt's own; the same command and data under any
        # other outcome (a refusal, a partial result, a catalog) is labelled.
        assert labels_for_output("execute openai-pause", {"provider": "openai_ads"}, outcome) == []
        assert {
            item.provider
            for item in labels_for_output("execute openai-pause", {"provider": "openai_ads"}, "OK")
        } == {"openai_ads"}
        assert labels_for_output("plan ga4-key-event create", {}, "FAILED_CLOSED")


def test_each_exempt_receipt_outcome_is_still_the_outcome_its_command_emits() -> None:
    source = "".join(path.read_text() for path in (_REPOSITORY / "src" / "adhelm").glob("*_cli.py"))
    for outcome in release_labels.BOUND_SIZED_RECEIPT_OUTCOMES:
        assert f'"{outcome}"' in source, outcome


# --- discover and doctor -----------------------------------------------------------------


def test_discover_labels_every_provider_row_and_explains_the_labels(tmp_path: Path) -> None:
    envelope = _invoke("discover", directory=tmp_path)
    rows = {row["provider"]: row["release_label"] for row in envelope["data"]["providers"]}

    assert {name: rows[name] for name in RELEASE_PROVIDERS} == dict.fromkeys(
        RELEASE_PROVIDERS, EXPERIMENTAL
    )
    assert rows["ga4_admin"] == EXPERIMENTAL
    # Providers outside the 0.1.0 release carry no label.
    assert rows["hubspot"] is None and rows["salesforce"] is None
    labels = envelope["data"]["release_labels"]
    assert labels["meanings"] == dict(LABEL_MEANINGS)
    assert labels["grants_capability"] is False
    assert {item["provider"] for item in envelope["provider_labels"]} == set(RELEASE_PROVIDERS)


def test_discover_requires_exactly_the_released_providers(tmp_path: Path) -> None:
    envelope = _invoke("discover", directory=tmp_path)
    rows = {row["provider"]: row for row in envelope["data"]["providers"]}

    required = {name for name, row in rows.items() if row["release_required"]}
    # The eight released providers, plus GA4 Admin, which is part of the GA4 release.
    assert required == {*RELEASE_PROVIDERS, "ga4_admin"}
    for name in ("hubspot", "salesforce"):
        assert rows[name]["release_required"] is False
        assert rows[name]["implementation"] == "NOT_IMPLEMENTED"
        assert rows[name]["release_label"] is None


@pytest.mark.parametrize("piloted", [False, True], ids=["no_record", "ga4_record"])
def test_discover_never_requires_an_unlabelled_or_unimplemented_provider(
    tmp_path: Path, records: Path, piloted: bool
) -> None:
    # Holds with no pilot record and with one: a label never gates `release_required`.
    # Each parameter gets a fresh `records` fixture, which clears the cached inventory,
    # so the record written here is the one `discover` reads.
    if piloted:
        _write(records, "ga4.json", sealed_record("ga4"))
    envelope = _invoke("discover", directory=tmp_path)
    rows = {row["provider"]: row for row in envelope["data"]["providers"]}

    expected = LIVE_PILOTED if piloted else EXPERIMENTAL
    # The record took effect: GA4 and GA4 Admin (one release) read LIVE_PILOTED with it.
    assert rows["ga4"]["release_label"] == expected
    assert rows["ga4_admin"]["release_label"] == expected
    assert rows["ga4"]["release_required"] is True
    assert rows["ga4_admin"]["release_required"] is True
    for name in ("hubspot", "salesforce"):
        assert rows[name]["release_required"] is False

    for name, row in rows.items():
        if row["release_required"]:
            assert row["release_label"] in {LIVE_PILOTED, EXPERIMENTAL}, name
            assert row["implementation"] != "NOT_IMPLEMENTED", name
        else:
            assert row["release_label"] is None, name


def test_discover_and_doctor_show_a_piloted_provider(tmp_path: Path, records: Path) -> None:
    record = sealed_record("ga4")
    _write(records, "ga4.json", record)
    save_configuration(
        tmp_path,
        Configuration(
            profiles=[
                Profile(
                    profile_id="ga4-read",
                    provider="ga4",
                    account_ref="123",
                    principal_ref="principal.fixture",
                    client_ref="client.fixture",
                    scopes=["read"],
                    purpose="READ",
                    credential_ref="oauth.fixture",
                )
            ]
        ),
    )

    discovered = _invoke("discover", directory=tmp_path)
    doctor = _invoke("doctor", directory=tmp_path)

    rows = {row["provider"]: row["release_label"] for row in discovered["data"]["providers"]}
    assert rows["ga4"] == LIVE_PILOTED and rows["gtm"] == EXPERIMENTAL
    assert doctor["data"]["providers"] == [
        {"provider": "ga4", "status": "CONFIGURED_UNVERIFIED", "release_label": LIVE_PILOTED}
    ]
    assert doctor["data"]["pilot_records"] == [
        {"file": "ga4.json", "state": ACCEPTED, "provider": "ga4"}
    ]
    assert doctor["provider_labels"] == [
        {
            "provider": "ga4",
            "label": LIVE_PILOTED,
            "meaning": LABEL_MEANINGS[LIVE_PILOTED],
            "pilot_record_digest": record.record_digest,
        }
    ]


def test_doctor_reports_a_record_it_could_not_use_instead_of_dropping_it(
    tmp_path: Path, records: Path
) -> None:
    (records / "stale.json").write_bytes(
        pilot_record_bytes(sealed_record("ga4", cli_version="0.0.9"))
    )
    (records / "forged.json").write_bytes(_tampered(sealed_record("gtm"), ui_comparison="MISMATCH"))

    doctor = _invoke("doctor", directory=tmp_path)

    assert doctor["data"]["pilot_records"] == [
        {"file": "forged.json", "state": "PILOT_RECORD_DIGEST_MISMATCH", "provider": "gtm"},
        {"file": "stale.json", "state": "PILOT_RECORD_VERSION_MISMATCH", "provider": "ga4"},
    ]
    assert {item["label"] for item in doctor["provider_labels"]} <= {EXPERIMENTAL}


def test_the_pilot_record_schema_is_installed() -> None:
    result = _RUNNER.invoke(app, ["schema", "pilot-record", "--root-only"])

    assert result.exit_code == 0, result.output
    properties = json.loads(result.output)["data"]["schema"]["properties"]
    assert {"provider", "cli_version", "steps", "ui_comparison", "record_digest"} <= set(properties)
