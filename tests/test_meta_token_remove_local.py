"""`auth meta-token remove-local`: one profile's local Meta state, nothing else.

Every case is fake-only: an in-memory credential store and, where an inspection
is needed, a `MockTransport`.  No operating-system credential store is opened and
no request reaches Meta.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_credential_setup import FakeStore
from test_credential_setup import _profile as generic_profile
from typer.testing import CliRunner

from adhelm import credential_setup, meta_token_cli
from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_inspection as inspection
from adhelm import meta_token_observation_records as observation_records
from adhelm.canonical import canonical_bytes
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import (
    enroll_meta_token_profile,
    meta_token_credential_status,
    remove_meta_token_local,
    rotate_meta_token_credentials,
)
from adhelm.meta_token_observation_records import (
    META_TOKEN_REVOCATION_GUIDANCE,
    remove_meta_token_observation,
    retain_meta_token_observation,
)
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import Configuration, MetaAccountTokenProfile, OutputPreferences

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_SERVICE = "adhelm.meta-token-v1"
_FIRST = "meta-first-read"
_SECOND = "meta-second-read"
_OBSERVATIONS = "meta-token-observations.json"


def _subject(profile_id: str) -> str:
    return f"synthetic-subject-token-for-{profile_id}"


def _inspector(profile_id: str) -> str:
    return f"synthetic-inspector-token-for-{profile_id}"


@dataclass
class _Store:
    """An in-memory credential store whose failure modes a test can switch on."""

    values: dict[tuple[str, str], str] = field(default_factory=dict)
    deletes: list[tuple[str, str]] = field(default_factory=list)
    keep_deleted_entries: bool = False
    delete_raises: bool = False
    get_raises: bool = False
    #: Raise on the Nth `get_password` call (1-based); 0 disables.
    get_raises_on_call: int = 0
    get_calls: int = 0

    def get_password(self, service_name: str, username: str) -> str | None:
        self.get_calls += 1
        if self.get_raises or self.get_raises_on_call == self.get_calls:
            raise RuntimeError("store unavailable")
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.deletes.append((service_name, username))
        if self.delete_raises:
            raise RuntimeError("store refused the delete")
        if not self.keep_deleted_entries:
            del self.values[(service_name, username)]


def _draft(profile_id: str) -> MetaAccountTokenProfileDraft:
    return MetaAccountTokenProfileDraft.model_validate(
        {
            "schema_version": 1,
            "profile_id": profile_id,
            "account_ref": "12345",
            "client_application_ref": "67890",
            "currency": "USD",
            "timezone": "UTC",
            "purpose": "READ",
            "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
        }
    )


def _enroll(directory: Path, store: _Store, monkeypatch: pytest.MonkeyPatch, *ids: str) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    for profile_id in ids:
        enroll_meta_token_profile(
            directory,
            _draft(profile_id),
            subject_token_supplier=lambda profile_id=profile_id: _subject(profile_id),
            inspector_token_supplier=lambda profile_id=profile_id: _inspector(profile_id),
        )


def _configure(directory: Path, *, maximum: int = 4096, profiles: list | None = None) -> None:
    save_configuration(
        directory,
        Configuration(profiles=profiles or [], output=OutputPreferences(max_output_bytes=maximum)),
    )


def _inspect_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer `debug_token` for whichever profile's subject token is presented."""
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v26.0/debug_token"
        return httpx.Response(
            200,
            json={
                "data": {
                    "app_id": "67890",
                    "user_id": "54321",
                    "is_valid": True,
                    "type": "USER",
                    "scopes": ["ads_read"],
                    "issued_at": int((_NOW - timedelta(minutes=1)).timestamp()),
                    "expires_at": int((_NOW + timedelta(days=30)).timestamp()),
                    "data_access_expires_at": int((_NOW + timedelta(days=60)).timestamp()),
                }
            },
        )

    monkeypatch.setattr(
        inspection.httpx,
        "Client",
        lambda *, trust_env: real_client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    monkeypatch.setattr(inspection, "_now", lambda: _NOW)
    monkeypatch.setattr(inspection, "_monotonic", lambda: 100.0)


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("a local removal must not construct an HTTP client")

    monkeypatch.setattr(inspection.httpx, "Client", forbidden)


def _run(directory: Path, *arguments: str):
    return CliRunner().invoke(app, ["--data-dir", str(directory), "auth", "meta-token", *arguments])


def _envelope(result) -> dict:
    return json.loads(result.output)


def _data(result) -> dict:
    assert result.exit_code == 0, result.output
    return _envelope(result)["data"]


def _code(result, exit_code: int) -> str:
    assert result.exit_code == exit_code, result.output
    return _envelope(result)["diagnostics"][0]["code"]


def _bundle_keys(store: _Store) -> set[tuple[str, str]]:
    return {key for key in store.values if key[0] == _SERVICE}


def _bundle_key(directory: Path, profile_id: str) -> tuple[str, str]:
    profile = next(p for p in load_configuration(directory).profiles if p.profile_id == profile_id)
    assert isinstance(profile, MetaAccountTokenProfile)
    return (_SERVICE, profile.credential_ref)


def _observations(directory: Path) -> dict:
    return json.loads((directory / _OBSERVATIONS).read_text(encoding="utf-8"))


def _two_inspected_profiles(
    directory: Path, store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST, _SECOND)
    _inspect_transport(monkeypatch)
    for profile_id in (_FIRST, _SECOND):
        _data(_run(directory, "inspect", "--profile", profile_id))


# -------------------------------------------------------------- what is removed


def test_removing_one_of_two_profiles_deletes_only_its_bundle_and_its_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    first_bundle = _bundle_key(directory, _FIRST)
    second_bundle = _bundle_key(directory, _SECOND)
    second_value = store.values[second_bundle]
    assert _bundle_keys(store) == {first_bundle, second_bundle}
    assert len(_observations(directory)) == 2
    before = _data(_run(directory, "status", "--profile", _SECOND))
    assert before["retained_inspection"] == "MATCHES_INSTALLED_BUNDLE"
    _no_network(monkeypatch)

    data = _data(_run(directory, "remove-local", "--profile", _FIRST))

    # The selected profile's bundle is gone and the other profile's is the same
    # entry, byte for byte; the only delete the store saw was the selected entry.
    assert _bundle_keys(store) == {second_bundle}
    assert store.values[second_bundle] == second_value
    assert store.deletes == [first_bundle]
    assert data["bundle_removed"] is True
    assert data["retained_inspection_removed"] is True
    # The other profile keeps its retained inspection, and it still matches its bundle.
    assert len(_observations(directory)) == 1
    after = _data(_run(directory, "status", "--profile", _SECOND))
    assert after == before
    gone = _data(_run(directory, "status", "--profile", _FIRST))
    assert gone["local_state"] == "MISSING"
    assert gone["retained_inspection"] == "NOT_RETAINED"
    assert gone["token_validity"] == "NOT_INSPECTED_LOCALLY"
    assert gone["expires_at"] is None


def test_removal_touches_no_other_credential_store_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)
    unrelated = {
        ("adhelm", "meta-token-bundle-v1." + "0" * 40): "another service holds this name",
        ("adhelm", "an-ordinary-profile-secret"): "ordinary",
        (_SERVICE, "meta-token-bundle-v1." + "1" * 40): "a bundle with no configured profile",
    }
    store.values.update(unrelated)

    _data(_run(directory, "remove-local", "--profile", _FIRST))

    assert store.values == unrelated


def test_the_result_never_claims_revocation_and_carries_no_token_or_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    reference = _bundle_key(directory, _FIRST)[1]
    binding = meta_token_credential_status(directory, _FIRST).credential_binding_ref
    assert binding is not None

    result = _run(directory, "remove-local", "--profile", _FIRST)

    envelope = _envelope(result)
    assert envelope["command"] == "auth meta-token remove-local"
    assert envelope["outcome"] == "OK"
    data = envelope["data"]
    assert data["operation"] == "LOCAL_REMOVE_ONLY"
    assert data["local_only"] is True
    assert data["local_state"] == "MISSING"
    assert data["remote_revocation_verified"] is False
    assert data["remote_mutations"] is False
    assert data["execution_authority"] is False
    assert data["revocation_guidance"] == META_TOKEN_REVOCATION_GUIDANCE
    # The label states evidence, never a capability; Meta Ads has no pilot record.
    assert "EXPERIMENTAL" in result.output
    # Nothing that identifies a token, the profile, the entry or the inspection.
    for secret in (
        _subject(_FIRST),
        _inspector(_FIRST),
        _FIRST,
        reference,
        binding,
        "credential-binding",
    ):
        assert secret not in result.output


def test_removal_makes_no_request_and_prompts_for_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)
    _no_network(monkeypatch)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("a removal must not prompt for a secret")

    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)
    monkeypatch.setattr(meta_token_cli, "_prompt_subject_token", forbidden)
    monkeypatch.setattr(meta_token_cli, "_prompt_inspector_token", forbidden)

    assert _data(_run(directory, "remove-local", "--profile", _FIRST))["bundle_removed"] is True


# -------------------------------------------------------- missing is reported


def test_a_repeated_removal_reports_that_nothing_was_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    assert _data(_run(directory, "remove-local", "--profile", _FIRST))["bundle_removed"] is True
    deletes = list(store.deletes)

    again = _data(_run(directory, "remove-local", "--profile", _FIRST))

    assert again["bundle_removed"] is False
    assert again["retained_inspection_removed"] is False
    assert again["local_state"] == "MISSING"
    assert again["remote_revocation_verified"] is False
    # No delete was attempted against an entry that was not there.
    assert store.deletes == deletes
    assert len(_bundle_keys(store)) == 1
    assert len(_observations(directory)) == 1


def test_a_profile_that_was_never_inspected_reports_no_retained_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)

    data = _data(_run(directory, "remove-local", "--profile", _FIRST))

    assert data["bundle_removed"] is True
    assert data["retained_inspection_removed"] is False
    assert not (directory / _OBSERVATIONS).exists()


def test_a_missing_bundle_still_clears_the_profiles_retained_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    # Someone deleted the entry by hand, as the pre-release guide told them to.
    del store.values[_bundle_key(directory, _FIRST)]

    data = _data(_run(directory, "remove-local", "--profile", _FIRST))

    assert data["bundle_removed"] is False
    assert data["retained_inspection_removed"] is True
    assert store.deletes == []
    assert len(_observations(directory)) == 1


# --------------------------------------------------------------- refusals


def test_an_unconfigured_profile_is_refused_before_any_store_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)
    values = dict(store.values)

    def forbidden() -> object:
        pytest.fail("an unselected profile must stop before the credential store")

    monkeypatch.setattr(token_credentials, "_credential_backend", forbidden)
    result = _run(directory, "remove-local", "--profile", "never-configured")

    assert _code(result, 2) == "META_TOKEN_PROFILE_REQUIRED"
    assert store.values == values


def test_a_profile_of_another_provider_is_refused_and_keeps_its_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    generic = generic_profile()
    _configure(directory, profiles=[generic])
    store = _Store()
    _enroll(directory, store, monkeypatch, _FIRST)
    ordinary = FakeStore()
    ordinary.values[("adhelm", generic.credential_ref)] = "ordinary-secret"
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: ordinary)

    result = _run(directory, "remove-local", "--profile", generic.profile_id)

    assert _code(result, 2) == "META_TOKEN_PROFILE_REQUIRED"
    assert ordinary.values == {("adhelm", generic.credential_ref): "ordinary-secret"}
    assert len(_bundle_keys(store)) == 1


def test_the_generic_removal_still_refuses_a_meta_profile_and_names_the_dedicated_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)
    values = dict(store.values)

    result = CliRunner().invoke(
        app, ["--data-dir", str(directory), "auth", "remove-local", "--profile", _FIRST]
    )

    assert _code(result, 4) == "META_TOKEN_CREDENTIAL_LIFECYCLE_REQUIRED"
    assert (
        "`adhelm auth meta-token remove-local --profile <id>`"
        in _envelope(result)["diagnostics"][0]["next_action"]
    )
    assert store.values == values


def test_the_output_bound_is_enforced_before_any_credential_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)
    values = dict(store.values)

    def tight(*_args: object, **_kwargs: object) -> None:
        raise AdhelmError("OUTPUT_LIMIT_EXCEEDED", "fixed output failure", exit_code=3)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("the output preflight must precede every store and file access")

    monkeypatch.setattr(meta_token_cli, "_require_result_fit", tight)
    monkeypatch.setattr(meta_token_cli, "remove_meta_token_local", forbidden)
    result = _run(directory, "remove-local", "--profile", _FIRST)

    assert _code(result, 3) == "OUTPUT_LIMIT_EXCEEDED"
    assert store.values == values


def _configure_bound(directory: Path, maximum: int) -> None:
    configuration = load_configuration(directory)
    save_configuration(
        directory,
        configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=maximum)}),
        replace=True,
    )


def test_the_largest_removal_result_fits_the_smallest_configurable_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    _configure_bound(directory, 1024)

    result = _run(directory, "remove-local", "--profile", _FIRST)

    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) <= 1024


# ------------------------------------------------------- unverified outcomes


def test_a_delete_the_store_ignores_is_reported_as_unverified_and_keeps_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    store.keep_deleted_entries = True
    values = dict(store.values)

    result = _run(directory, "remove-local", "--profile", _FIRST)

    assert _code(result, 6) == "META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED"
    assert not _envelope(result).get("data")
    assert store.values == values
    # Nothing was claimed removed, so the inspection that describes the still
    # installed bundle was not touched either.
    assert len(_observations(directory)) == 2
    status = _data(_run(directory, "status", "--profile", _FIRST))
    assert status["local_state"] == "READY"
    assert status["retained_inspection"] == "MATCHES_INSTALLED_BUNDLE"


def test_a_delete_that_raises_is_judged_by_readback_not_by_the_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)
    store.delete_raises = True

    result = _run(directory, "remove-local", "--profile", _FIRST)

    # The entry is still there, so the removal is unverified; the exception text
    # (which a real store may fill with platform detail) is never surfaced.
    assert _code(result, 6) == "META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED"
    assert "store refused" not in result.output
    assert len(_bundle_keys(store)) == 1


def test_an_unavailable_store_is_reported_and_leaves_the_retained_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    store.get_raises = True

    result = _run(directory, "remove-local", "--profile", _FIRST)

    assert _code(result, 4) == "CREDENTIAL_STORE_UNAVAILABLE"
    assert "store unavailable" not in result.output
    assert len(_observations(directory)) == 2


def test_a_readback_that_raises_after_a_delete_is_reported_as_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    observations = (directory / _OBSERVATIONS).read_bytes()
    # Call 1 is the presence check and succeeds; call 2 is the readback.
    store.get_calls = 0
    store.get_raises_on_call = 2

    result = _run(directory, "remove-local", "--profile", _FIRST)

    assert _code(result, 6) == "META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED"
    assert not _envelope(result).get("data")
    assert "store unavailable" not in result.output
    assert (directory / _OBSERVATIONS).read_bytes() == observations


def test_a_bundle_that_cannot_be_decoded_is_still_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST, _SECOND)
    first = _bundle_key(directory, _FIRST)
    second = _bundle_key(directory, _SECOND)
    store.values[first] = "not a bundle"
    assert _data(_run(directory, "status", "--profile", _FIRST))["local_state"] == "UNVERIFIED"

    data = _data(_run(directory, "remove-local", "--profile", _FIRST))

    assert data["bundle_removed"] is True
    assert set(store.values) == {second}


def test_an_unreadable_inspection_file_leaves_the_bundle_removed_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    corrupt = b'{"truncated":'
    (directory / _OBSERVATIONS).write_bytes(corrupt)

    result = _run(directory, "remove-local", "--profile", _FIRST)

    # The credential is gone, which is what stops AdHelm using the token; the
    # unreadable file is reported, not papered over, and is not rewritten (a
    # rewrite would silently drop the other profile's record).
    assert _code(result, 6) == "META_TOKEN_INSPECTION_REMOVAL_UNVERIFIED"
    assert _bundle_keys(store) == {_bundle_key(directory, _SECOND)}
    assert (directory / _OBSERVATIONS).read_bytes() == corrupt
    assert "meta-token-observations.json" in _envelope(result)["diagnostics"][0]["next_action"]


# ------------------------------------------------------ after the removal


def test_after_a_removal_the_same_profile_cannot_be_rotated_or_enrolled_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guide says to enroll under a new profile ID; this pins why."""
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)
    _data(_run(directory, "remove-local", "--profile", _FIRST))

    with pytest.raises(AdhelmError) as rotated:
        rotate_meta_token_credentials(
            directory,
            _FIRST,
            subject_token_supplier=lambda: _subject(_FIRST),
            inspector_token_supplier=lambda: _inspector(_FIRST),
        )
    assert rotated.value.code == "META_TOKEN_CREDENTIAL_MISSING"
    with pytest.raises(AdhelmError) as enrolled:
        enroll_meta_token_profile(
            directory,
            _draft(_FIRST),
            subject_token_supplier=lambda: _subject(_FIRST),
            inspector_token_supplier=lambda: _inspector(_FIRST),
        )
    assert enrolled.value.code == "META_TOKEN_PROFILE_EXISTS"
    # A new profile ID enrolls normally and is independent of the removed one.
    _enroll(directory, store, monkeypatch, _SECOND)
    assert _data(_run(directory, "status", "--profile", _SECOND))["local_state"] == "READY"
    assert _data(_run(directory, "status", "--profile", _FIRST))["local_state"] == "MISSING"


# --------------------------------------------------- the observation record


def test_removing_an_observation_that_was_never_retained_does_not_create_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, _Store(), monkeypatch, _FIRST)
    profile = load_configuration(directory).profiles[0]
    assert isinstance(profile, MetaAccountTokenProfile)

    assert remove_meta_token_observation(directory, profile) is False
    assert not (directory / _OBSERVATIONS).exists()


def test_removing_a_profile_leaves_the_other_profiles_record_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    before = _observations(directory)
    assert len(before) == 2

    _data(_run(directory, "remove-local", "--profile", _FIRST))

    after = _observations(directory)
    assert len(after) == 1
    ((key, record),) = after.items()
    assert canonical_bytes(record) == canonical_bytes(before[key])
    assert (directory / _OBSERVATIONS).read_bytes() == canonical_bytes(after)


def test_a_retain_cannot_interleave_with_a_removal_of_another_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both writers share one lock, so a concurrent retain is refused, not lost."""
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    profiles = {p.profile_id: p for p in load_configuration(directory).profiles}
    first, second = profiles[_FIRST], profiles[_SECOND]
    assert isinstance(first, MetaAccountTokenProfile)
    assert isinstance(second, MetaAccountTokenProfile)
    before_second = _observations(directory)[observation_records._key(second)]
    interleaved: list[str] = []
    real_read_all = observation_records._read_all

    def read_then_interleave(path: Path) -> dict:
        stored = real_read_all(path)
        if not interleaved:
            interleaved.append("attempted")
            with pytest.raises(AdhelmError) as busy:
                retain_meta_token_observation(
                    directory,
                    second,
                    granted_scopes=("ads_read",),
                    credential_binding_ref="credential-binding:sha256:" + "0" * 64,
                    observed_at=_NOW,
                    expires_at=_NOW + timedelta(days=1),
                    data_access_expires_at=_NOW + timedelta(days=2),
                )
            assert busy.value.code == "META_TOKEN_OBSERVATIONS_BUSY"
        return stored

    monkeypatch.setattr(observation_records, "_read_all", read_then_interleave)

    assert remove_meta_token_observation(directory, first) is True

    assert interleaved == ["attempted"]
    assert _observations(directory) == {observation_records._key(second): before_second}


def test_the_direct_function_returns_a_result_with_no_revocation_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)

    result = remove_meta_token_local(directory, _FIRST)

    assert result.bundle_removed is True
    assert result.retained_inspection_removed is False
    assert result.remote_revocation_verified is False
    assert result.provider == "meta_ads"
    assert not _bundle_keys(store)


# ------------------------------------------------- a busy observations file


def _diagnostic(result) -> dict:
    return _envelope(result)["diagnostics"][0]


def test_inspect_says_the_result_was_not_retained_when_the_observations_file_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _configure(directory)
    _enroll(directory, store, monkeypatch, _FIRST)
    _inspect_transport(monkeypatch)

    with observation_records._observation_file_lock(directory):
        result = _run(directory, "inspect", "--profile", _FIRST)

    diagnostic = _diagnostic(result)
    assert _code(result, 3) == "META_TOKEN_OBSERVATIONS_BUSY"
    # Nothing here is a credential setup, and the message says what actually happened.
    assert "credential setup" not in diagnostic["message"].lower()
    assert "not retained" in diagnostic["message"]
    assert f"adhelm auth meta-token inspect --profile {_FIRST}" in diagnostic["next_action"]
    assert not (directory / _OBSERVATIONS).exists()

    # Rerunning once the other command is done retains the inspection.
    _data(_run(directory, "inspect", "--profile", _FIRST))
    assert len(_observations(directory)) == 1


def test_remove_local_names_itself_as_the_recovery_when_the_observations_file_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    second_bundle = _bundle_key(directory, _SECOND)
    second_record = _observations(directory)[
        observation_records._key(_profile_named(directory, _SECOND))
    ]
    _no_network(monkeypatch)

    with observation_records._observation_file_lock(directory):
        busy = _run(directory, "remove-local", "--profile", _FIRST)

    diagnostic = _diagnostic(busy)
    assert _code(busy, 6) == "META_TOKEN_INSPECTION_REMOVAL_BUSY"
    # The bundle is already gone; the record is not, and nothing else moved.
    assert _bundle_keys(store) == {second_bundle}
    assert len(_observations(directory)) == 2
    # The recovery is the same command, not "run status" or "delete the file".
    assert f"adhelm auth meta-token remove-local --profile {_FIRST}" in diagnostic["next_action"]
    assert "delete" not in diagnostic["next_action"].lower()
    assert "meta-token-observations.json" not in diagnostic["next_action"]

    rerun = _data(_run(directory, "remove-local", "--profile", _FIRST))

    assert rerun["bundle_removed"] is False
    assert rerun["retained_inspection_removed"] is True
    remaining = _observations(directory)
    assert list(remaining.values()) == [second_record]
    assert _bundle_keys(store) == {second_bundle}


def test_an_unreadable_file_keeps_the_delete_the_file_advice_and_is_not_called_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    store = _Store()
    _two_inspected_profiles(directory, store, monkeypatch)
    (directory / _OBSERVATIONS).write_bytes(b'{"truncated":')

    result = _run(directory, "remove-local", "--profile", _FIRST)

    assert _code(result, 6) == "META_TOKEN_INSPECTION_REMOVAL_UNVERIFIED"
    assert "delete" in _diagnostic(result)["next_action"].lower()


def _profile_named(directory: Path, profile_id: str) -> MetaAccountTokenProfile:
    profile = next(p for p in load_configuration(directory).profiles if p.profile_id == profile_id)
    assert isinstance(profile, MetaAccountTokenProfile)
    return profile
