"""Complete, redacted strategy previews and explicit sealed local retention."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from adhelm import __version__
from adhelm.canonical import canonical_bytes, digest
from adhelm.compile_cli import _ensure_redacted_preview_fits
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.models import ResultEnvelope
from adhelm.projections import _redact_payload
from adhelm.saved_strategies import retain_strategy
from adhelm.strategy import OpenAIStrategyCandidate, StrategyBrief, compile_strategy
from adhelm.strategy_records import StrategyCandidateCompilationRecord


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _retain_strategy(ctx: typer.Context, brief: StrategyBrief) -> None:
    """Preflight a compact key-free receipt before one atomic ledger append."""
    from adhelm.cli import emit, root

    command = "compile strategy"
    maximum = load_configuration(root(ctx)).output.max_output_bytes
    receipt: dict[str, Any] | None = None

    def preflight(observation: StrategyCandidateCompilationRecord) -> None:
        nonlocal receipt
        receipt = {
            "record_digest": "0" * 64,
            "strategy_brief_digest": observation.strategy_brief_digest,
            "strategy_spec_digest": observation.strategy_spec_digest,
            "selected_provider_count": len(observation.child_compilations),
            "total_caps_by_currency": [
                cap.model_dump(mode="json") for cap in observation.total_caps_by_currency
            ],
            "maximum_spend_by_currency": [
                spend.model_dump(mode="json") for spend in observation.maximum_spend_by_currency
            ],
            "expires_at": observation.expires_at.isoformat(),
            "persisted": True,
            "remote_side_effects": False,
            "requires_new_compilation_for_planning": True,
            "authorization_status": "NO_AUTHORITY_GRANTED",
        }
        envelope = ResultEnvelope(
            tool_version=__version__,
            command=command,
            outcome=observation.execution_status,
            data=receipt,
        )
        if len(canonical_bytes(envelope)) + 1 > maximum:
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Strategy retention receipt exceeds the saved output limit.",
                next_action="Increase saved max_output_bytes; no strategy was appended.",
            )

    record = retain_strategy(Ledger(root(ctx)), brief, now=_utc_now, before_append=preflight)
    if receipt is None:  # Defensive guard if a future retention path changes its callback contract.
        raise AdhelmError("LOCAL_IO_FAILED", "Strategy retention receipt was not prepared.")
    receipt["record_digest"] = record["record_digest"]
    emit(
        command,
        receipt,
        outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
        maximum_bytes=maximum,
    )


def register_commands(app: typer.Typer) -> None:
    @app.command("strategy")
    def strategy(
        ctx: typer.Context,
        input_file: Annotated[Path, typer.Option("--input")],
        retain: Annotated[bool, typer.Option("--retain")] = False,
    ) -> None:
        """Compile every selected native brief and preview the complete budget and gates."""
        from adhelm.cli import emit, guarded, root

        def work() -> None:
            brief = validate_json_file(StrategyBrief, input_file)
            now = _utc_now()
            if brief.expires_at <= now:
                raise AdhelmError(
                    "STRATEGY_EXPIRED",
                    "The strategy brief has expired.",
                    exit_code=2,
                    next_action="Prepare a new brief with a future expiry and new content digest.",
                )
            if retain:
                _retain_strategy(ctx, brief)
                return
            compiled = compile_strategy(brief, now=lambda: now)
            children: list[dict[str, Any]] = []
            for candidate in compiled.candidates:
                spec = candidate.spec
                child: dict[str, Any] = {
                    "provider": candidate.provider,
                    "provider_api_version": spec.provider_api_version,
                    "capability_version": spec.capability_version,
                    "input_digest": spec.input_digest,
                    "spec_digest": spec.spec_digest,
                    "review": candidate.review.model_dump(mode="json"),
                    "planning_boundary": spec.planning_boundary.model_dump(mode="json"),
                    "activation_requires_new_plan_and_authorization": True,
                }
                if isinstance(candidate, OpenAIStrategyCandidate):
                    child["activation_boundary"] = candidate.spec.activation_boundary.model_dump(
                        mode="json"
                    )
                    child["lossy_mappings"] = list(candidate.spec.lossy_mappings)
                    child["unsupported_mappings"] = list(candidate.spec.unsupported_mappings)
                children.append(child)
            # No source brief, native payload, free-text intent, assumptions,
            # or provenance enters the agent-visible result. The exact source
            # is bound by digest; this compact preview cannot authorize it.
            preview: dict[str, Any] = {
                "strategy_spec_digest": compiled.strategy_spec_digest,
                "input_digest": compiled.input_digest,
                "strategy_brief_digest": brief.brief_id.removeprefix("strategy:sha256:"),
                "approval_owner_ref": compiled.approval_owner,
                "expires_at": compiled.expires_at.isoformat(),
                "source_context": {
                    "business_intent_digest": digest(compiled.business_intent),
                    "assumptions_count": len(compiled.assumptions),
                    "provenance_count": len(compiled.provenance),
                    "free_text_status": "OMITTED_FROM_AGENT_OUTPUT",
                },
                "total_caps_by_currency": [
                    cap.model_dump(mode="json") for cap in compiled.total_caps_by_currency
                ],
                "maximum_spend_by_currency": [
                    spend.model_dump(mode="json") for spend in compiled.maximum_spend_by_currency
                ],
                "ordered_child_spec_digests": list(compiled.ordered_child_spec_digests),
                "selected_provider_count": len(children),
                "candidates": children,
                "unmet_live_proof": compiled.unmet_live_proof,
                "compiler_evidence": compiled.fixture_only_status,
                "persisted": False,
                "remote_side_effects": False,
                "authorization_status": "NO_AUTHORITY_GRANTED",
            }
            maximum = load_configuration(root(ctx)).output.max_output_bytes
            _ensure_redacted_preview_fits("compile strategy", preview, maximum)
            data = _redact_payload(preview, export_key(), False)
            emit(
                "compile strategy",
                data,
                outcome=compiled.status,
                maximum_bytes=maximum,
            )

        guarded("compile strategy", work)
