"""Independent adversarial coverage for static Google Ads PAUSE plan artifacts."""

from __future__ import annotations

import csv
import json
import re
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from pydantic_core import PydanticSerializationError
from test_google_ads_pause_plan_artifacts import _KEY, _PROFILE, _decision, _loaded

import adhelm.google_ads_pause_plan_artifacts as plan_artifacts
from adhelm.errors import AdhelmError
from adhelm.models import OutputPreferences

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# The complete closed vocabulary a rendered cell may use. Nothing in this
# document is user-supplied prose, so no cell can open a formula or a command.
_POINTER = re.compile(r"^/|^(?:/[A-Za-z_][A-Za-z0-9_]*)+$", re.ASCII)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$", re.ASCII)
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$", re.ASCII)
_CLOSED_CELL = re.compile(
    r"^(?:"
    r"|[A-Z][A-Z0-9_]*"  # fixed status, enum, currency and capability tokens
    r"|[a-z][a-z0-9_]*"  # lowercase enums, provider names and JSON booleans
    r"|[0-9a-f]{64}"  # canonical digests
    r"|opaque:[0-9a-f]{64}"  # redacted sharing references
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|\+00:00)"
    r"|[0-9]+"  # bounded integers such as schema versions and minor units
    r"|[A-Za-z]+(?:/[A-Za-z0-9_+-]+)+"  # a multi-segment IANA account timezone
    r")$",
    re.ASCII,
)


def test_source_drift_after_zero_key_preflight_never_reaches_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path must recheck source facts after a successful dry render."""
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir(mode=0o700)
    decision = _decision(output)
    plan_artifacts.preflight_google_ads_campaign_pause_plan_artifact_output(
        action, provenance, decision
    )
    provenance["plan_digest"] = "f" * 64

    def writer_reached(*_args: object, **_kwargs: object) -> object:
        pytest.fail("changed retained source reached the artifact writer")

    monkeypatch.setattr(plan_artifacts, "_generate_typed_artifacts", writer_reached)
    with pytest.raises(AdhelmError) as raised:
        plan_artifacts.render_google_ads_campaign_pause_plan_artifacts(
            action,
            provenance,
            decision,
            key=_KEY,
        )

    assert raised.value.code == "GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"
    assert list(output.iterdir()) == []


def test_tampered_retained_action_is_refused_before_review_or_publication(
    tmp_path: Path,
) -> None:
    """A digest-consistent provenance cannot rescue a mutated retained action."""
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir(mode=0o700)
    tampered = action.model_copy(
        update={
            "postcondition": action.postcondition.model_copy(
                update={"configured_status": "ENABLED"}
            )
        }
    )

    with pytest.raises(AdhelmError) as raised:
        plan_artifacts.render_google_ads_campaign_pause_plan_artifacts(
            tampered, provenance, _decision(output), key=_KEY
        )

    assert raised.value.code in {
        "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN",
        "GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID",
    }
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("destination_kind", ["symlink", "fifo"])
def test_unsafe_explicit_destination_is_refused_before_document_or_writer(
    tmp_path: Path, destination_kind: str, make_link, make_pipe
) -> None:
    """The Google wrapper preserves the common descriptor-safe destination gate."""
    destination = tmp_path / destination_kind
    if destination_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        make_link(destination, outside)
    else:
        destination = make_pipe(destination_kind)

    with pytest.raises(AdhelmError) as raised:
        plan_artifacts.explicit_google_ads_campaign_pause_plan_artifact_decision(
            OutputPreferences(artifact_mode="OFF"),
            profile_id=_PROFILE,
            formats=("json",),
            output_directory=str(destination),
            replacement_authorized=None,
        )

    assert raised.value.code == "UNSAFE_ARTIFACT_DESTINATION"
    if destination_kind == "symlink":
        assert list((tmp_path / "outside").iterdir()) == []


def test_every_rendered_format_excludes_credential_and_customer_evidence(tmp_path: Path) -> None:
    """Static review files carry only the redacted Google Ads review surface."""
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir(mode=0o700)
    result = plan_artifacts.render_google_ads_campaign_pause_plan_artifacts(
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
        action.account_binding.account_ref.encode("ascii"),
        action.account_binding.account_client_ref.encode("ascii"),
        action.observation_binding.campaign.campaign_ref.encode("ascii"),
        action.workspace_binding.workspace_profile_ref.encode("ascii"),
        action.execution_binding.profile_ref.encode("ascii"),
        action.execution_binding.client_application_ref.encode("ascii"),
        b"canonical_challenge",
        b"challenge_nonce",
        b"canonical_receipt",
        b"active_public_key",
        b"native_observed_at",
        b"request_id",
        b"developer-token",
        b"login-customer-id",
    )
    assert all(value not in rendered for value in private_values)
    assert b"HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE" in rendered
    assert b"UNRESOLVED_NO_BLIND_RETRY" in rendered
    assert b"UNRELEASED" in rendered
    assert b"CONFIGURED_PAUSE_DOES_NOT_PROVE_EFFECTIVE_DELIVERY_OR_ZERO_RESIDUAL_SPEND" in rendered
    document = json.loads(
        next(item.path.read_text() for item in result.artifacts if item.format == "json")
    )
    review = document["content"]["review"]
    reference_fields = [name for name in review if name.endswith("_ref")]
    assert len(reference_fields) >= 7
    for name in reference_fields:
        value = review[name]
        assert value is None or (type(value) is str and value.startswith("opaque:"))


def test_csv_document_has_no_free_text_injection_surface(tmp_path: Path) -> None:
    """Every emitted CSV cell is a closed token, so nothing can carry a payload.

    Asserting only that no cell starts with a formula prefix would be vacuous
    here: this document contains no free-text field for a formula to reach. The
    honest property is stronger and is what makes the shared escaping rule
    unnecessary for this artifact kind. Every value cell is a fixed enum, a
    64-hex digest, an ``opaque:`` reference, an RFC 3339 UTC instant, an ISO
    currency code, a decimal integer, ``true``/``false``, or empty; every
    section and field name is a JSON pointer of identifier segments.
    """
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir(mode=0o700)
    result = plan_artifacts.render_google_ads_campaign_pause_plan_artifacts(
        action, provenance, _decision(output), key=_KEY
    )
    path = next(item.path for item in result.artifacts if item.format == "csv")

    rows = list(csv.reader(StringIO(path.read_text(encoding="utf-8"))))

    assert rows[0] == ["section", "ordinal", "field", "value"]
    assert len(rows) > 1
    for section, ordinal, field, value in rows[1:]:
        assert _POINTER.fullmatch(section) is not None, section
        assert _INTEGER.fullmatch(ordinal) is not None, ordinal
        assert _IDENTIFIER.fullmatch(field) is not None, field
        assert _CLOSED_CELL.fullmatch(value) is not None, (field, value)
        for cell in (section, ordinal, field, value):
            assert not cell.startswith(_FORMULA_PREFIXES), cell
            assert not any(character in cell for character in ',"\n\r'), cell
    values = {field: value for _section, _ordinal, field, value in rows[1:]}
    assert values["minor_units"] == "0"
    assert values["execution_command"] == "UNRELEASED"


def test_html_output_is_inert_static_markup(tmp_path: Path) -> None:
    """A rendered review may not carry script, links, or remote assets."""
    action, provenance = _loaded(tmp_path)
    output = tmp_path / "views"
    output.mkdir(mode=0o700)
    result = plan_artifacts.render_google_ads_campaign_pause_plan_artifacts(
        action, provenance, _decision(output), key=_KEY
    )
    document = next(
        item.path.read_text(encoding="utf-8") for item in result.artifacts if item.format == "html"
    )

    lowered = document.lower()
    for forbidden in ("<script", "href=", "src=", "onclick", "javascript:", "<iframe", "http://"):
        assert forbidden not in lowered


def test_constructed_document_cannot_be_serialized_as_an_artifact_body() -> None:
    """A missing-slot public document cannot become a generic writer payload."""
    forged = object.__new__(plan_artifacts.GoogleAdsCampaignPausePlanArtifactDocument)

    with pytest.raises(PydanticSerializationError) as raised:
        forged.model_dump(mode="json", warnings="error")

    assert "Google Ads campaign PAUSE artifact document is invalid" in str(raised.value)


def test_revalidation_refuses_unknown_or_malformed_loader_provenance(tmp_path: Path) -> None:
    """Only the complete loader shape may reach the review projection."""
    action, provenance = _loaded(tmp_path)
    attempted: list[dict[str, Any]] = [
        {**provenance, "unexpected_native_reference": "private"},
        {**provenance, "checkpoint_digest": "not-a-digest"},
        {**provenance, "execution_authority": 0},
        {**provenance, "requires_current_kernel_checks": False},
        {**provenance, "historical_evidence_status": "CURRENT_REMOTE_EVIDENCE"},
        {name: value for name, value in provenance.items() if name != "plan_digest"},
    ]

    for malformed in attempted:
        with pytest.raises(AdhelmError) as raised:
            plan_artifacts.revalidate_google_ads_campaign_pause_plan_artifact_source(
                action, malformed
            )
        assert raised.value.code == "GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"
