"""Independent adversarial review of GA4 CREATE plan static artifacts."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_ga4_admin_execution_observation import _KEY
from test_ga4_create_workflow import _setup
from typer.testing import CliRunner

import adhelm.artifacts as shared_artifacts
import adhelm.ga4_create_plan_artifacts as plan_artifacts
import adhelm.saved_ga4_create_plans as saved_plans
from adhelm import credentials
from adhelm.canonical import canonical_bytes
from adhelm.cli import app as root_app
from adhelm.errors import AdhelmError
from adhelm.models import OutputPreferences


def _loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan
    from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan

    request, ledger, _, profile, _, _, _, _ = _setup(tmp_path, monkeypatch)
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    action, provenance = load_ga4_key_event_create_plan(ledger, retained.plan_record_digest)
    return action, provenance, profile


def _document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    action, provenance, _ = _loaded(tmp_path, monkeypatch)
    checked, source = plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source(
        action, provenance
    )
    raw = plan_artifacts._ga4_create_plan_document(  # noqa: SLF001
        checked, source, key=_KEY, max_rows=50
    )
    return plan_artifacts.Ga4CreatePlanArtifactDocument.model_validate_json(canonical_bytes(raw))


def _decision(output: Path, profile_id: str, *, formats: tuple[str, ...] = ("json",)):
    return plan_artifacts.explicit_ga4_key_event_create_plan_artifact_decision(
        OutputPreferences(artifact_mode="OFF", max_output_bytes=262_144),
        profile_id=profile_id,
        formats=formats,  # type: ignore[arg-type]
        output_directory=str(output),
        replacement_authorized=None,
    )


def _artifact_request(
    output: Path, *, replacement_authorized: object | None = None
) -> dict[str, object]:
    request: dict[str, object] = {
        "schema_version": 1,
        "formats": ["json", "html"],
        "output_directory": str(output),
    }
    if replacement_authorized is not None:
        request["replacement_authorized"] = replacement_authorized
    return request


def _invoke_artifact(directory: Path, record_digest: str, request: Path):
    return CliRunner().invoke(
        root_app,
        [
            "--data-dir",
            str(directory),
            "artifacts",
            "ga4-key-event-plan",
            record_digest,
            "--input",
            str(request),
        ],
    )


def test_document_serializer_rejects_constructed_nested_authority_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public typed document must not serialize a forged authority claim."""
    document = _document(tmp_path, monkeypatch)
    forged_authority = document.content.authority.model_copy(update={"execution_authority": True})
    forged = document.model_copy(
        update={"content": document.content.model_copy(update={"authority": forged_authority})}
    )

    with pytest.raises(ValueError):
        forged.model_dump(mode="json", warnings="error")


def test_document_fixture_is_json_serializable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the fixture useful while the forged serializer guard is reviewed."""
    assert json.loads(json.dumps(_document(tmp_path, monkeypatch).model_dump(mode="json")))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.__setitem__("schema_version", 1.0),
        lambda raw: raw["content"]["authority"].__setitem__("execution_authority", 0),
        lambda raw: raw["content"]["authority"].__setitem__("remote_mutations", 0.0),
        lambda raw: raw["content"]["historical_review"].__setitem__("static_artifact", 1),
        lambda raw: raw["content"]["historical_review"].__setitem__(
            "opening_or_editing_authorizes_execution", 0.0
        ),
        lambda raw: raw["content"]["review"].__setitem__("expired_at_review", 1),
    ],
)
def test_document_rejects_raw_numeric_control_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: object
) -> None:
    """Raw JSON must retain boolean/integer types through the artifact boundary."""
    raw = deepcopy(_document(tmp_path, monkeypatch).model_dump(mode="json"))
    mutate(raw)  # type: ignore[operator]

    with pytest.raises(ValidationError):
        # Do not canonicalize here: canonical JSON would turn a raw ``1.0``
        # back into ``1`` before the document's original-type guard sees it.
        plan_artifacts.Ga4CreatePlanArtifactDocument.model_validate_json(json.dumps(raw))


def test_writer_rechecks_source_at_document_handoff_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source failure after zero-key preflight must create no output file."""
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    decision = _decision(output, profile.profile_id)
    original = plan_artifacts.revalidate_ga4_key_event_create_plan_artifact_source
    calls = 0

    def fail_at_writer_handoff(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise AdhelmError("GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID", "private source sentinel")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        plan_artifacts,
        "revalidate_ga4_key_event_create_plan_artifact_source",
        fail_at_writer_handoff,
    )
    result = plan_artifacts.render_ga4_key_event_create_plan_artifacts(
        action, provenance, decision, key=_KEY
    )

    assert calls >= 3
    assert result.generation_failure_code == "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID"
    assert result.artifacts == ()
    assert list(output.iterdir()) == []
    assert "private source sentinel" not in json.dumps(result.to_data())


def test_partial_writer_failure_preserves_only_completed_redacted_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second-format local write error remains bounded and does not erase the first file."""
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    decision = _decision(output, profile.profile_id, formats=("json", "csv"))
    original = shared_artifacts._write_versioned
    private_error = "private-second-write-sentinel"
    calls = 0

    def fail_second(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(private_error)
        return original(*args, **kwargs)

    monkeypatch.setattr(shared_artifacts, "_write_versioned", fail_second)
    result = plan_artifacts.render_ga4_key_event_create_plan_artifacts(
        action, provenance, decision, key=_KEY
    )

    assert result.generation_failure_code == "LOCAL_IO_FAILED"
    assert len(result.artifacts) == 1
    assert result.artifacts[0].format == decision.formats[0]
    assert result.artifacts[0].path.is_file()
    data = result.to_data()
    assert len(data["artifacts"]) == 1
    assert private_error not in json.dumps(data)


def test_symlinked_destination_never_receives_ga4_artifact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GA4 wrapper must preserve the shared writer's descriptor-safe directory rule."""
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdhelmError) as raised:
        _decision(linked, profile.profile_id)

    assert raised.value.code == "UNSAFE_ARTIFACT_DESTINATION"
    assert list(outside.iterdir()) == []


def test_historical_redacted_render_is_deterministic_without_provider_or_oauth_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The static renderer uses retained facts, preserves history, and redacts identifiers."""
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    decision = _decision(output, profile.profile_id, formats=("json", "csv", "html"))
    first = plan_artifacts.render_ga4_key_event_create_plan_artifacts(
        action, provenance, decision, key=_KEY
    )
    second = plan_artifacts.render_ga4_key_event_create_plan_artifacts(
        action, provenance, decision, key=_KEY
    )

    assert first.generation_failure_code is None
    assert all(item.reused for item in second.artifacts)
    contents = {item.format: item.path.read_text() for item in first.artifacts}
    document = json.loads(contents["json"])
    review = document["content"]["review"]
    assert document["content"]["historical_review"]["as_of"] == provenance["recorded_at"]
    assert review["reviewed_at"] == provenance["recorded_at"]
    assert review["expired_at_review"] is False
    assert document["content"]["authority"]["execution_authority"] is False
    assert document["content"]["historical_review"]["current_validity"] == (
        "NOT_EVALUATED_HISTORICAL_ARTIFACT"
    )
    rendered = "\n".join(contents.values())
    for private in (
        "qualified_request",
        "properties/123",
        "ga4_admin.property.123",
        "ga4_admin.web_stream.456",
        profile.profile_id,
        profile.principal_ref,
        profile.client_ref,
        "eventName",
        "fingerprint_key_marker",
        "inventory_content_marker",
    ):
        assert private not in rendered
    assert "opaque:" in rendered
    assert "<script" not in contents["html"].lower()
    assert "href=" not in contents["html"].lower()


def test_explicit_cli_rejects_raw_boolean_alias_before_history_or_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid explicit authorization flag must not reach configuration, history, or a key."""
    request = tmp_path / "private-request.json"
    request.write_text(json.dumps(_artifact_request(tmp_path, replacement_authorized=1)))

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid request reached local history or sharing-key access")

    monkeypatch.setattr("adhelm.artifact_cli.load_configuration", prohibited)
    monkeypatch.setattr(credentials, "export_key", prohibited)
    result = _invoke_artifact(tmp_path, "0" * 64, request)

    assert result.exit_code != 0
    diagnostic = json.loads(result.output)["diagnostics"][0]
    assert diagnostic["code"] == "SCHEMA_VALIDATION_FAILED"
    assert "private-request.json" not in result.output


def test_explicit_cli_rejects_forged_provenance_before_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unallowlisted loader field cannot reach redaction or a document writer."""
    action, provenance, _ = _loaded(tmp_path, monkeypatch)
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_artifact_request(output)))
    private = "private-loader-provenance-sentinel"
    forged = {**provenance, "private_bundle": private}

    monkeypatch.setattr(
        saved_plans,
        "load_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: (action, forged),
    )
    monkeypatch.setattr(
        credentials,
        "export_key",
        lambda: pytest.fail("forged provenance acquired the sharing key"),
    )
    result = _invoke_artifact(tmp_path, provenance["record_digest"], request)

    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == (
        "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID"
    )
    assert private not in result.output
    assert list(output.iterdir()) == []


def test_explicit_cli_partial_failure_keeps_only_completed_refs_and_hides_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit command preserves its retained source and completed artifact reference."""
    action, provenance, profile = _loaded(tmp_path, monkeypatch)
    del action
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_artifact_request(output)))
    original = shared_artifacts._write_versioned
    private_error = "private-cli-second-write-sentinel"
    writes = 0

    def fail_second(*args: object, **kwargs: object):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError(private_error)
        return original(*args, **kwargs)

    monkeypatch.setattr(credentials, "export_key", lambda: _KEY)
    monkeypatch.setattr(shared_artifacts, "_write_versioned", fail_second)
    result = _invoke_artifact(tmp_path, provenance["record_digest"], request)

    assert result.exit_code == 5, result.output
    envelope = json.loads(result.output)
    artifact_result = envelope["data"]["artifact_result"]
    assert envelope["outcome"] == "PARTIAL"
    assert artifact_result["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(artifact_result["artifacts"]) == len(envelope["artifact_refs"]) == 1
    assert artifact_result["artifacts"][0]["format"] in {"html", "json"}
    assert len(list(output.iterdir())) == 1
    assert private_error not in result.output
    assert profile.profile_id not in result.output
