"""Bounded offline previews for the remaining advertising compiler contracts."""

from pathlib import Path
from typing import Annotated, Literal

import typer

from adhelm.compile_cli import _ensure_redacted_preview_fits, _retain_compilation
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.input_files import validate_json_file
from adhelm.projections import _redact_payload
from adhelm.providers.linkedin_compiler import (
    LinkedInAdsCampaignBrief,
    LinkedInAdsCompiledCampaignSpec,
    compile_linkedin_ads_campaign,
)
from adhelm.providers.reddit_compiler import (
    RedditAdsCampaignBrief,
    RedditAdsCompiledCampaignSpec,
    compile_reddit_ads_campaign,
)
from adhelm.providers.tiktok_compiler import (
    TikTokAdsCampaignBrief,
    TikTokAdsCompiledCampaignSpec,
    compile_tiktok_ads_campaign,
)


def _preview(
    ctx: typer.Context,
    input_file: Path,
    provider: Literal["linkedin-ads", "reddit-ads", "tiktok-ads"],
    *,
    retain: bool = False,
) -> None:
    from adhelm.cli import emit, guarded, root

    command = f"compile {provider}"

    def work() -> None:
        spec: (
            LinkedInAdsCompiledCampaignSpec
            | RedditAdsCompiledCampaignSpec
            | TikTokAdsCompiledCampaignSpec
        )
        brief: LinkedInAdsCampaignBrief | RedditAdsCampaignBrief | TikTokAdsCampaignBrief
        if provider == "linkedin-ads":
            brief = validate_json_file(LinkedInAdsCampaignBrief, input_file)
            if retain:
                _retain_compilation(ctx, brief, command)
                return
            spec = compile_linkedin_ads_campaign(brief)
        elif provider == "reddit-ads":
            brief = validate_json_file(RedditAdsCampaignBrief, input_file)
            if retain:
                _retain_compilation(ctx, brief, command)
                return
            spec = compile_reddit_ads_campaign(brief)
        else:
            brief = validate_json_file(TikTokAdsCampaignBrief, input_file)
            if retain:
                _retain_compilation(ctx, brief, command)
                return
            spec = compile_tiktok_ads_campaign(brief)
        review = {
            name.removesuffix("_handle") + "_ref" if name.endswith("_handle") else name: value
            for name, value in spec.redacted_review.model_dump(mode="json").items()
        }
        # Emit the closed review and every unresolved check; native request
        # templates, creative text, destination URLs and issued IDs stay local.
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
        maximum = load_configuration(root(ctx)).output.max_output_bytes
        _ensure_redacted_preview_fits(command, preview, maximum)
        data = _redact_payload(preview, export_key(), False)
        emit(command, data, outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE", maximum_bytes=maximum)

    guarded(command, work)


def register_commands(app: typer.Typer) -> None:
    """Register fixed commands on the shared compile group without a passthrough."""

    @app.command("linkedin-ads")
    def linkedin_ads(
        ctx: typer.Context,
        input_file: Annotated[Path, typer.Option("--input")],
        retain: Annotated[bool, typer.Option("--retain")] = False,
    ) -> None:
        """Preview draft engagement templates for an existing dark image Post."""
        _preview(ctx, input_file, "linkedin-ads", retain=retain)

    @app.command("reddit-ads")
    def reddit_ads(
        ctx: typer.Context,
        input_file: Annotated[Path, typer.Option("--input")],
        retain: Annotated[bool, typer.Option("--retain")] = False,
    ) -> None:
        """Preview a paused US feed/post traffic candidate with a lifetime cap."""
        _preview(ctx, input_file, "reddit-ads", retain=retain)

    @app.command("tiktok-ads")
    def tiktok_ads(
        ctx: typer.Context,
        input_file: Annotated[Path, typer.Option("--input")],
        retain: Annotated[bool, typer.Option("--retain")] = False,
    ) -> None:
        """Preview a disabled website/video candidate with declared media limits."""
        _preview(ctx, input_file, "tiktok-ads", retain=retain)
