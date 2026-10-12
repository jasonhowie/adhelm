"""Offline native-spec candidate previews without plan or provider authority."""

from pathlib import Path
from typing import Annotated, Literal

import typer

from adhelm import __version__
from adhelm.canonical import canonical_bytes
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.models import ResultEnvelope
from adhelm.projections import _redact_payload
from adhelm.providers.openai_compiler import (
    OpenAIAdsCampaignBrief,
    compile_openai_ads_campaign,
)
from adhelm.saved_candidates import CampaignBrief, compile_candidate, retain_candidate

app = typer.Typer(no_args_is_help=True)


def _retain_compilation(ctx: typer.Context, brief: CampaignBrief, command: str) -> None:
    """Preflight a key-free receipt before retaining normalized compilation evidence."""
    from adhelm.cli import emit, root

    compilation = compile_candidate(brief)
    maximum = load_configuration(root(ctx)).output.max_output_bytes
    data = {
        "candidate_digest": compilation.candidate_digest,
        "spec_digest": compilation.spec_digest,
        "record_digest": "0" * 64,
        "maximum_spend": compilation.maximum_spend.model_dump(mode="json"),
        "persisted": True,
        "remote_side_effects": False,
        "requires_new_compilation_for_planning": True,
    }
    envelope = ResultEnvelope(
        tool_version=__version__,
        command=command,
        outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
        data=data,
    )
    if len(canonical_bytes(envelope)) + 1 > maximum:
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "Candidate receipt exceeds the output limit.",
            next_action="Increase the saved output limit; no candidate was appended.",
        )
    try:
        record = retain_candidate(Ledger(root(ctx)), brief)
    except (OSError, AdhelmError) as exc:
        raise AdhelmError(
            "CANDIDATE_RETENTION_UNVERIFIED",
            "Local candidate retention could not be verified; no remote write occurred.",
            next_action="Run history verify and candidate catalog before retaining again.",
        ) from exc
    data["record_digest"] = record["record_digest"]
    emit(command, data, outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE", maximum_bytes=maximum)


def _ensure_redacted_preview_fits(
    command: str, data: dict[str, object], maximum_bytes: int
) -> None:
    """Reject an over-limit preview before accessing the local sharing key.

    HMAC opaque references have a fixed ASCII length for every valid key. A
    fixed in-memory key therefore gives the exact final envelope byte length
    without reading or creating the OS-stored export key.
    """
    probe = _redact_payload(data, b"\0" * 32, False)
    envelope = ResultEnvelope(
        tool_version=__version__,
        command=command,
        outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
        data=probe,
    )
    if len(canonical_bytes(envelope)) + 1 > maximum_bytes:
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "Preview exceeds the configured output limit.",
            next_action=(
                "Increase saved max_output_bytes or reduce the brief; no candidate was retained "
                "and no provider action was sent."
            ),
        )


def _fixed_offline_preview(
    ctx: typer.Context,
    input_file: Path,
    provider: Literal["meta_ads", "google_ads"],
    *,
    retain: bool,
) -> None:
    from adhelm.cli import emit, guarded, root
    from adhelm.providers.google_ads_compiler import (
        GoogleAdsCompiledCampaignSpec,
        GoogleAdsSearchCampaignBrief,
        compile_google_ads_search_campaign,
    )
    from adhelm.providers.meta_compiler import (
        MetaAdsCampaignBrief,
        MetaAdsCompiledCampaignSpec,
        compile_meta_ads_campaign,
    )

    command = "compile meta-ads" if provider == "meta_ads" else "compile google-ads"

    def work() -> None:
        spec: MetaAdsCompiledCampaignSpec | GoogleAdsCompiledCampaignSpec
        brief: MetaAdsCampaignBrief | GoogleAdsSearchCampaignBrief
        if provider == "meta_ads":
            brief = validate_json_file(MetaAdsCampaignBrief, input_file)
            if retain:
                _retain_compilation(ctx, brief, command)
                return
            spec = compile_meta_ads_campaign(brief)
        else:
            brief = validate_json_file(GoogleAdsSearchCampaignBrief, input_file)
            if retain:
                _retain_compilation(ctx, brief, command)
                return
            spec = compile_google_ads_search_campaign(brief)
        # Role handles are caller-chosen labels. Move them into the common
        # keyed-reference domain before any agent-visible output.
        review = {
            name.removesuffix("_handle") + "_ref" if name.endswith("_handle") else name: value
            for name, value in spec.redacted_review.model_dump(mode="json").items()
        }
        preview = {
            "provider": spec.provider,
            "provider_api_version": spec.provider_api_version,
            "capability_version": spec.capability_version,
            "input_digest": spec.input_digest,
            "review": review,
            "planning_boundary": spec.planning_boundary.model_dump(mode="json"),
            "compiler_evidence": "FIXTURE_VERIFIED_LIVE_UNVERIFIED",
            "persisted": False,
            "remote_side_effects": False,
            "activation_requires_new_plan_and_authorization": True,
        }
        preferences = load_configuration(root(ctx)).output
        _ensure_redacted_preview_fits(command, preview, preferences.max_output_bytes)
        data = _redact_payload(preview, export_key(), False)
        emit(
            command,
            data,
            outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded(command, work)


@app.command("meta-ads")
def meta_ads(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    retain: Annotated[bool, typer.Option("--retain")] = False,
) -> None:
    """Preview a fixed paused traffic/image candidate without provider authority."""
    _fixed_offline_preview(ctx, input_file, "meta_ads", retain=retain)


@app.command("google-ads")
def google_ads(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    retain: Annotated[bool, typer.Option("--retain")] = False,
) -> None:
    """Preview a fixed paused Search/RSA candidate without provider authority."""
    _fixed_offline_preview(ctx, input_file, "google_ads", retain=retain)


@app.command("openai-ads")
def openai_ads(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    retain: Annotated[bool, typer.Option("--retain")] = False,
) -> None:
    """Validate a paused chat-card candidate; this does not produce an executable plan."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        brief = validate_json_file(OpenAIAdsCampaignBrief, input_file)
        if retain:
            _retain_compilation(ctx, brief, "compile openai-ads")
            return
        spec = compile_openai_ads_campaign(brief)
        preferences = load_configuration(root(ctx)).output
        review = spec.redacted_review.model_dump(mode="json")
        # A local logical handle can contain a customer-selected campaign label.
        # Put it in the shared reference domain before producing agent output.
        review["campaign_ref"] = review.pop("campaign_handle")
        preview = {
            "provider": spec.provider,
            "provider_api_version": spec.provider_api_version,
            "capability_version": spec.capability_version,
            "review": review,
            "planning_boundary": spec.planning_boundary.model_dump(mode="json"),
            "activation_boundary": spec.activation_boundary.model_dump(mode="json"),
            "lossy_mappings": list(spec.lossy_mappings),
            "unsupported_mappings": list(spec.unsupported_mappings),
            "persisted": False,
            "remote_side_effects": False,
        }
        _ensure_redacted_preview_fits("compile openai-ads", preview, preferences.max_output_bytes)
        data = _redact_payload(preview, export_key(), False)
        emit(
            "compile openai-ads",
            data,
            outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded("compile openai-ads", work)
