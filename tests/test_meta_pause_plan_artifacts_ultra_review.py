"""Independent adversarial coverage for static Meta PAUSE plan artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic_core import PydanticSerializationError
from test_meta_pause_plan_artifacts import _KEY, _decision, _loaded

import adhelm.meta_pause_plan_artifacts as plan_artifacts
from adhelm.errors import AdhelmError
from adhelm.models import OutputPreferences


def test_source_drift_after_zero_key_preflight_never_reaches_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path must recheck source facts after a successful dry render."""
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir(mode=0o700)
    decision = _decision(output)
    plan_artifacts.preflight_meta_campaign_pause_plan_artifact_output(action, provenance, decision)
    provenance["plan_digest"] = "f" * 64

    def writer_reached(*_args: object, **_kwargs: object) -> object:
        pytest.fail("changed retained source reached the artifact writer")

    monkeypatch.setattr(plan_artifacts, "_generate_typed_artifacts", writer_reached)
    with pytest.raises(AdhelmError) as raised:
        plan_artifacts.render_meta_campaign_pause_plan_artifacts(
            action,
            provenance,
            decision,
            key=_KEY,
        )

    assert raised.value.code == "META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("destination_kind", ["symlink", "fifo"])
def test_unsafe_explicit_destination_is_refused_before_document_or_writer(
    tmp_path: Path, destination_kind: str, make_link, make_pipe
) -> None:
    """The Meta wrapper preserves the common descriptor-safe destination gate."""
    destination = tmp_path / destination_kind
    if destination_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        make_link(destination, outside)
    else:
        destination = make_pipe(destination_kind)

    with pytest.raises(AdhelmError) as raised:
        plan_artifacts.explicit_meta_campaign_pause_plan_artifact_decision(
            OutputPreferences(artifact_mode="OFF"),
            profile_id="meta-edit-private-marker",
            formats=("json",),
            output_directory=str(destination),
            replacement_authorized=None,
        )

    assert raised.value.code == "UNSAFE_ARTIFACT_DESTINATION"
    if destination_kind == "symlink":
        assert list((tmp_path / "outside").iterdir()) == []


def test_every_rendered_format_excludes_native_and_credential_evidence(tmp_path: Path) -> None:
    """Static review files carry only the M40 redacted review surface."""
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir(mode=0o700)
    result = plan_artifacts.render_meta_campaign_pause_plan_artifacts(
        action,
        provenance,
        _decision(output),
        key=_KEY,
    )

    assert result.generation_failure_code is None
    rendered = b"\n".join(item.path.read_bytes() for item in result.artifacts)
    private_values: tuple[bytes, ...] = (
        action.execution_binding.credential_binding_ref.encode("ascii"),
        action.execution_binding.principal_ref.encode("ascii"),
        action.execution_binding.configuration_digest.encode("ascii"),
        action.execution_binding.profile_configuration_digest.encode("ascii"),
        action.execution_binding.required_scope.encode("ascii"),
        b"canonical_challenge",
        b"challenge_nonce",
        b"canonical_receipt",
        b"active_public_key",
        b"native_observed_at",
        b"request_id",
    )
    assert all(value not in rendered for value in private_values)
    assert b"HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE" in rendered
    assert b"UNRESOLVED_NO_BLIND_RETRY" in rendered
    assert b"UNRELEASED" in rendered


def test_constructed_document_cannot_be_serialized_as_an_artifact_body() -> None:
    """A missing-slot public document cannot become a generic writer payload."""
    forged = object.__new__(plan_artifacts.MetaCampaignPausePlanArtifactDocument)

    with pytest.raises(PydanticSerializationError) as raised:
        forged.model_dump(mode="json", warnings="error")

    assert "Meta campaign PAUSE artifact document is invalid" in str(raised.value)


def test_revalidation_refuses_unknown_or_malformed_loader_provenance(tmp_path: Path) -> None:
    """Only the complete loader shape may reach the review projection."""
    action, provenance = _loaded(tmp_path)
    attempted: list[dict[str, Any]] = [
        {**provenance, "unexpected_native_reference": "private"},
        {**provenance, "checkpoint_digest": "not-a-digest"},
        {**provenance, "execution_authority": 0},
    ]

    for malformed in attempted:
        with pytest.raises(AdhelmError) as raised:
            plan_artifacts.revalidate_meta_campaign_pause_plan_artifact_source(action, malformed)
        assert raised.value.code == "META_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"
