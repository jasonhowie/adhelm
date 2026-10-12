"""Optional, deterministic local views of reviewed retained evidence.

The public report/audit entry point accepts only reviewed normalized reports
and audits. Focused plan-artifact modules may use the private typed-document
writer below after they reconstruct and redact their own closed retained input.
This is not a general document renderer or an execution surface.
"""

from __future__ import annotations

import csv
import hashlib
import html
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Literal

from adhelm.artifact_detail import ArtifactDetail, present_document
from adhelm.audit import AuditResult
from adhelm.canonical import canonical_bytes, digest
from adhelm.directory_access import (
    Directory,
    create_private_child,
    fstat,
    link_child,
    open_child_for_read,
    replace_child,
    stat_child,
    unlink_child,
)
from adhelm.errors import AdhelmError
from adhelm.models import ArtifactRule, OutputPreferences, Provider
from adhelm.projections import _redact_payload, escape_formula
from adhelm.reporting import NormalizedReport

if sys.platform == "win32":
    from adhelm import windows_filesystem

ArtifactFormat = Literal["json", "csv", "html"]
Workflow = Literal["audit", "report", "plan", "verify", "diagnostics"]
Stage = Literal["completion", "before_approval", "meaningful_change", "failure"]
Purpose = Literal["audit", "comparison", "plan_review", "integration_map", "result", "diagnostics"]


@dataclass(frozen=True)
class ArtifactTaskOverride:
    """A one-task user instruction; this module never persists it."""

    requested: bool = True
    formats: tuple[ArtifactFormat, ...] = ()
    output_directory: str | None = None
    purpose: Purpose | None = None
    replacement_authorized: bool | None = None


@dataclass(frozen=True)
class ArtifactContext:
    workflow: Workflow
    stage: Stage
    purpose: Purpose
    provider: Provider
    profile_id: str | None = None


@dataclass(frozen=True)
class ArtifactDecision:
    """Complete side-effect-free resolution of optional output policy."""

    outcome: Literal["GENERATE", "SUPPRESSED", "NEEDS_CONFIGURATION"]
    reason: str
    formats: tuple[ArtifactFormat, ...] = ()
    output_directory: Path | None = None
    preferences_digest: str | None = None
    max_rows: int | None = None
    max_output_bytes: int | None = None
    reuse_allowed: bool = True
    replacement_authorized: bool = False
    detail: ArtifactDetail = "compact"


@dataclass(frozen=True)
class RetainedArtifactInput:
    """Reviewed evidence with digest references from a verified local checkpoint."""

    value: NormalizedReport | AuditResult
    source_record_digest: str
    checkpoint_digest: str

    def __post_init__(self) -> None:
        for name, value in (
            ("source_record_digest", self.source_record_digest),
            ("checkpoint_digest", self.checkpoint_digest),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not isinstance(self.value, (NormalizedReport, AuditResult)):
            raise TypeError(
                "Artifacts support only retained NormalizedReport or AuditResult values"
            )
        if (
            isinstance(self.value, AuditResult)
            and self.value.source_record_digest != self.source_record_digest
        ):
            raise ValueError("Audit source record digest must match retained evidence")


@dataclass(frozen=True)
class ArtifactMetadata:
    path: Path
    format: ArtifactFormat
    reused: bool
    artifact_digest: str
    input_digest: str
    settings_digest: str
    source_record_digest: str
    checkpoint_digest: str


@dataclass
class _ArtifactDirectory:
    """An opened destination directory that cannot be redirected by path races."""

    path: Path
    # A POSIX directory descriptor, or the pinned directory chain on Windows.
    handle: Directory
    device: int
    inode: int

    def close(self) -> None:
        if isinstance(self.handle, int):
            os.close(self.handle)
        else:
            self.handle.close()


def _set_private_mode(descriptor: int, mode: int) -> None:
    if sys.platform == "win32":
        # Windows files and directories are created with a protected private DACL.
        return
    fchmod = getattr(os, "fchmod", None)
    if not callable(fchmod):
        raise OSError("Private artifact permissions are unavailable")
    fchmod(descriptor, mode)


class ArtifactGenerationError(AdhelmError):
    """A local write failure preserves references to already generated views."""

    def __init__(self, completed: tuple[ArtifactMetadata, ...], failure_code: str) -> None:
        if (
            not isinstance(failure_code, str)
            or not 1 <= len(failure_code) <= 80
            or not failure_code.isascii()
        ):
            raise ValueError("Artifact failure code must be a bounded ASCII identifier")
        super().__init__(
            "ARTIFACT_GENERATION_FAILED",
            "Optional artifacts could not all be written; retained source evidence is unchanged.",
            exit_code=5 if completed else 3,
            next_action="Inspect artifact references and the local destination before retrying.",
        )
        self.completed = completed
        self.failure_code = failure_code


_DECISION_REASONS = {
    "GENERATE": frozenset({"EXPLICIT_USER_REQUEST", "MATCHING_SAVED_RULE"}),
    "SUPPRESSED": frozenset(
        {
            "ARTIFACT_MODE_OFF",
            "NO_MATCHING_WORKFLOW_RULE",
            "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST",
            "PURPOSE_NOT_ENABLED",
            "TASK_OVERRIDE_DISABLED",
        }
    ),
    "NEEDS_CONFIGURATION": frozenset(
        {
            "LOCAL_OUTPUT_DIRECTORY_REQUIRED",
            "RULE_FORMAT_REQUIRED",
            "TASK_FORMAT_REQUIRED",
        }
    ),
}


def _policy_error() -> AdhelmError:
    return AdhelmError(
        "ARTIFACT_POLICY_INVALID",
        "Artifact generation requires a resolved installed local policy.",
        exit_code=2,
    )


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_artifact_decision(value: object) -> ArtifactDecision:
    """Normalize a resolver decision before it reaches any filesystem primitive.

    ``ArtifactDecision`` is a public frozen dataclass rather than a Pydantic
    model, so library callers can construct values without its annotations being
    enforced. The writer must not trust a forged digest, output path, or
    truthy replacement flag merely because it received that dataclass.
    """
    if not isinstance(value, ArtifactDecision) or type(value) is not ArtifactDecision:
        raise _policy_error()
    decision = value
    if type(decision.outcome) is not str:
        raise _policy_error()
    reasons = _DECISION_REASONS.get(decision.outcome)
    if reasons is None or type(decision.reason) is not str or decision.reason not in reasons:
        raise _policy_error()
    if decision.outcome != "GENERATE":
        # These outcomes deliberately carry no arbitrary caller-owned metadata
        # into agent-visible result serialization.
        return ArtifactDecision(decision.outcome, decision.reason)
    formats = decision.formats
    if (
        type(formats) is not tuple
        or not 1 <= len(formats) <= 3
        or any(
            type(format_name) is not str or format_name not in {"json", "csv", "html"}
            for format_name in formats
        )
        or tuple(sorted(set(formats))) != formats
        or type(decision.output_directory) is not type(Path())
        or not _is_digest(decision.preferences_digest)
        or type(decision.max_rows) is not int
        or not 1 <= decision.max_rows <= 500
        or type(decision.max_output_bytes) is not int
        or not 1_024 <= decision.max_output_bytes <= 1_048_576
        or type(decision.reuse_allowed) is not bool
        or type(decision.replacement_authorized) is not bool
        or type(decision.detail) is not str
        or decision.detail not in {"compact", "standard", "detailed"}
    ):
        raise _policy_error()
    directory = decision.output_directory
    if (
        not directory.is_absolute()
        or ".." in directory.parts
        or any(
            any(ord(character) < 32 or ord(character) == 127 for character in component)
            for component in directory.parts
        )
    ):
        raise _policy_error()
    return ArtifactDecision(
        "GENERATE",
        decision.reason,
        formats=formats,
        output_directory=directory,
        preferences_digest=decision.preferences_digest,
        max_rows=decision.max_rows,
        max_output_bytes=decision.max_output_bytes,
        reuse_allowed=decision.reuse_allowed,
        replacement_authorized=decision.replacement_authorized,
        detail=decision.detail,
    )


class _ArtifactPublishedCleanupError(OSError):
    """A target was linked successfully but its private temporary file remained."""

    def __init__(self, path: Path) -> None:
        super().__init__("Artifact temporary-file cleanup failed after publication")
        self.path = path


class _ArtifactLinkInProgress(OSError):
    """The winner has linked its fsynced target but not removed its temp name."""


def resolve_artifact_policy(
    context: ArtifactContext,
    preferences: OutputPreferences | None,
    *,
    task_override: ArtifactTaskOverride | None = None,
) -> ArtifactDecision:
    """Resolve saved preferences without writing a file or scheduling work."""
    saved = preferences or OutputPreferences()
    if task_override is not None:
        if type(task_override.requested) is not bool or (
            task_override.replacement_authorized is not None
            and type(task_override.replacement_authorized) is not bool
        ):
            raise AdhelmError(
                "INVALID_ARTIFACT_OVERRIDE",
                "Artifact task override has an invalid authorization value.",
                exit_code=2,
            )
        if not task_override.requested:
            return ArtifactDecision("SUPPRESSED", "TASK_OVERRIDE_DISABLED")
        formats = _formats(task_override.formats or tuple(saved.formats))
        if not formats:
            return ArtifactDecision("NEEDS_CONFIGURATION", "TASK_FORMAT_REQUIRED")
        directory = (
            saved.output_directory
            if task_override.output_directory is None
            else task_override.output_directory
        )
        return _generation_decision(
            formats,
            directory,
            saved,
            replacement_authorized=task_override.replacement_authorized,
            reason="EXPLICIT_USER_REQUEST",
        )

    if saved.artifact_mode == "OFF":
        return ArtifactDecision("SUPPRESSED", "ARTIFACT_MODE_OFF")
    if saved.artifact_mode == "ON_REQUEST":
        return ArtifactDecision("SUPPRESSED", "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST")
    matching = [rule for rule in saved.workflow_rules if _matches(rule, context)]
    if not matching:
        return ArtifactDecision("SUPPRESSED", "NO_MATCHING_WORKFLOW_RULE")
    if context.purpose not in saved.purposes:
        return ArtifactDecision("SUPPRESSED", "PURPOSE_NOT_ENABLED")
    formats = _formats(format_name for rule in matching for format_name in rule.formats)
    if not formats:
        return ArtifactDecision("NEEDS_CONFIGURATION", "RULE_FORMAT_REQUIRED")
    return _generation_decision(
        formats,
        saved.output_directory,
        saved,
        replacement_authorized=None,
        reason="MATCHING_SAVED_RULE",
    )


def generate_artifacts(
    retained: RetainedArtifactInput, decision: ArtifactDecision, *, key: bytes
) -> tuple[ArtifactMetadata, ...]:
    """Create selected safe local files; a suppressed decision has no side effect."""
    return _generate_typed_artifacts(
        decision,
        source_record_digest=retained.source_record_digest,
        checkpoint_digest=retained.checkpoint_digest,
        document_builder=lambda max_rows: _document(retained, key, max_rows=max_rows),
        source_schema=type(retained.value).model_json_schema(),
        key=key,
    )


def _generate_typed_artifacts(
    decision: ArtifactDecision,
    *,
    source_record_digest: str,
    checkpoint_digest: str,
    document_builder: Callable[[int], dict[str, object]],
    source_schema: Mapping[str, object],
    key: bytes,
    renderer_identity: str | None = None,
) -> tuple[ArtifactMetadata, ...]:
    """Publish one caller-owned typed redacted document through the safe writer.

    This private helper deliberately has no arbitrary public-input API. Each
    caller owns a closed source type, its revalidation, redaction projection,
    and static schema before reaching the common output path.
    """
    decision = _validated_artifact_decision(decision)
    if decision.outcome != "GENERATE":
        return ()
    if not _is_digest(source_record_digest) or not _is_digest(checkpoint_digest):
        raise _policy_error()
    if type(key) is not bytes or len(key) < 32:
        raise AdhelmError(
            "EXPORT_KEY_REQUIRED",
            "Artifact redaction requires a private local 32-byte export key.",
        )
    if (
        decision.output_directory is None
        or decision.preferences_digest is None
        or decision.max_rows is None
        or decision.max_output_bytes is None
    ):
        raise AdhelmError(
            "ARTIFACT_POLICY_INVALID",
            "Artifact generation requires a resolved local policy.",
        )
    output_directory = _safe_existing_directory(decision.output_directory)
    directory = _open_artifact_directory(output_directory)
    output: list[ArtifactMetadata] = []
    try:
        document = document_builder(decision.max_rows)
        input_digest = digest(document)
        document = present_document(
            document,
            decision.detail,
            source_schema if decision.detail == "detailed" else None,
        )
        settings_digest = (
            decision.preferences_digest
            if renderer_identity is None
            else digest(
                {
                    "policy_settings_digest": decision.preferences_digest,
                    "renderer_identity": renderer_identity,
                }
            )
        )
        base = f"artifact-{input_digest[:16]}-{settings_digest[:16]}"
        rendered = [
            (format_name, _render(document, format_name)) for format_name in decision.formats
        ]
        if any(len(contents) > decision.max_output_bytes for _, contents in rendered):
            raise AdhelmError(
                "ARTIFACT_SIZE_LIMIT_EXCEEDED",
                "The requested artifact exceeds the configured output-size bound.",
            )
        for format_name, contents in rendered:
            post_publish_failure: str | None = None
            try:
                # The policy path may have changed after it was opened. Do not
                # publish into an inode that no longer has the configured safe
                # local path.
                _assert_directory_current(directory)
                path, reused = _write_versioned(
                    directory,
                    base,
                    format_name,
                    contents,
                    reuse=decision.reuse_allowed,
                    replace=decision.replacement_authorized,
                )
            except _ArtifactPublishedCleanupError as exc:
                # The target has been linked and its bytes were fsynced. Preserve
                # the usable reference even though the hidden temporary cleanup
                # needs attention.
                path, reused = exc.path, False
                post_publish_failure = "TEMPORARY_CLEANUP_FAILED"
            except (OSError, AdhelmError) as exc:
                failure_code = exc.code if isinstance(exc, AdhelmError) else "LOCAL_IO_FAILED"
                raise ArtifactGenerationError(tuple(output), failure_code) from exc
            try:
                expected_target = _artifact_status(
                    directory,
                    path.name,
                    allow_link_in_progress=post_publish_failure is not None,
                )
            except _ArtifactLinkInProgress as exc:
                raise ArtifactGenerationError(tuple(output), "ARTIFACT_PUBLICATION_RACE") from exc
            except (OSError, AdhelmError) as exc:
                failure_code = (
                    exc.code if isinstance(exc, AdhelmError) else "UNSAFE_ARTIFACT_DESTINATION"
                )
                raise ArtifactGenerationError(tuple(output), failure_code) from exc
            metadata = ArtifactMetadata(
                path=path,
                format=format_name,
                reused=reused,
                artifact_digest=hashlib.sha256(contents).hexdigest(),
                input_digest=input_digest,
                settings_digest=settings_digest,
                source_record_digest=source_record_digest,
                checkpoint_digest=checkpoint_digest,
            )
            directory_fsync_error: OSError | None = None
            try:
                _assert_directory_current(directory)
                # A reused path may be the target of a prior publication whose
                # directory fsync failed. Reconfirm it before reporting success.
                _fsync_directory(directory)
            except OSError as exc:
                directory_fsync_error = exc
            except AdhelmError as exc:
                raise ArtifactGenerationError(tuple(output), exc.code) from exc
            try:
                _assert_directory_current(directory)
                _assert_artifact_current(
                    directory,
                    path.name,
                    expected_target,
                    contents,
                    allow_link_in_progress=post_publish_failure is not None,
                )
            except _ArtifactLinkInProgress as exc:
                raise ArtifactGenerationError(tuple(output), "ARTIFACT_PUBLICATION_RACE") from exc
            except OSError as exc:
                # The opened inode remains private, but the configured path no
                # longer names it. Do not return a misleading or redirected path.
                # A close failure in the transient verification descriptor occurs
                # after publication; retain the completed reference while
                # reporting the verification failure as partial.
                raise ArtifactGenerationError(
                    tuple([*output, metadata]), "DIRECTORY_CLOSE_FAILED"
                ) from exc
            except AdhelmError as exc:
                raise ArtifactGenerationError(tuple(output), exc.code) from exc
            if directory_fsync_error is not None:
                # Publication succeeded; keep its reference even if directory durability fails.
                raise ArtifactGenerationError(
                    tuple([*output, metadata]), "DIRECTORY_FSYNC_FAILED"
                ) from directory_fsync_error
            output.append(metadata)
            if post_publish_failure is not None:
                raise ArtifactGenerationError(tuple(output), post_publish_failure)
        return tuple(output)
    finally:
        was_raising = sys.exc_info()[0] is not None
        try:
            directory.close()
        except OSError as exc:
            # Closing a descriptor after file and directory fsync must not erase
            # the published references. If another failure is already active,
            # preserve that more actionable outcome instead of masking it.
            if not was_raising:
                raise ArtifactGenerationError(tuple(output), "DIRECTORY_CLOSE_FAILED") from exc


def _generation_decision(
    formats: tuple[ArtifactFormat, ...],
    directory: str | None,
    preferences: OutputPreferences,
    *,
    replacement_authorized: bool | None,
    reason: str,
) -> ArtifactDecision:
    if directory is None:
        return ArtifactDecision(
            "NEEDS_CONFIGURATION", "LOCAL_OUTPUT_DIRECTORY_REQUIRED", formats=formats
        )
    path = _safe_existing_directory(_parse_local_directory(directory))
    effective_replacement = (
        preferences.overwrite_policy == "REPLACE_AUTHORIZED"
        if replacement_authorized is None
        else replacement_authorized
    )
    # Changes to permission or destination do not alter rendered bytes. Keeping
    # those out of the rendering digest permits reuse and authorized repair.
    settings = {
        "renderer_version": 2,
        "detail": preferences.detail,
        "max_rows": preferences.max_rows,
        "formats": formats,
    }
    return ArtifactDecision(
        "GENERATE",
        reason,
        formats=formats,
        output_directory=path,
        preferences_digest=digest(settings),
        max_rows=preferences.max_rows,
        max_output_bytes=preferences.max_output_bytes,
        reuse_allowed=preferences.regeneration == "REUSE",
        replacement_authorized=effective_replacement,
        detail=preferences.detail,
    )


def _matches(rule: ArtifactRule, context: ArtifactContext) -> bool:
    return (
        rule.workflow == context.workflow
        and rule.stage == context.stage
        and (not rule.providers or context.provider in rule.providers)
        and (not rule.profiles or context.profile_id in rule.profiles)
    )


def _formats(values: Iterable[ArtifactFormat]) -> tuple[ArtifactFormat, ...]:
    selected = tuple(sorted(set(values)))
    if any(value not in {"json", "csv", "html"} for value in selected):
        raise AdhelmError(
            "UNSUPPORTED_ARTIFACT_FORMAT", "Requested artifact format is unsupported."
        )
    return selected


def _parse_local_directory(value: str) -> Path:
    if (
        type(value) is not str
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AdhelmError(
            "INVALID_ARTIFACT_DESTINATION",
            "Artifact destination must be an absolute local path.",
        )
    if "://" in value:
        raise AdhelmError(
            "REMOTE_ARTIFACT_DESTINATION_UNSUPPORTED", "Artifacts require a local directory."
        )
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AdhelmError(
            "INVALID_ARTIFACT_DESTINATION",
            "Artifact destination must be an absolute local path.",
        )
    return path


def _safe_existing_directory(path: Path) -> Path:
    directory = _open_artifact_directory(path)
    directory.close()
    return path


def _open_artifact_directory(path: Path) -> _ArtifactDirectory:
    """Open every directory component without following a symlink.

    Artifact publication is descriptor-relative so a concurrent rename/symlink
    swap cannot redirect a verified user-selected destination. On Windows every
    component is opened as a reparse point, rejected if it is one, and held open
    so it cannot be renamed or replaced.
    """
    if sys.platform == "win32":
        try:
            pinned = windows_filesystem.open_directory(path)
        except OSError as exc:
            raise AdhelmError(
                "UNSAFE_ARTIFACT_DESTINATION",
                "Artifact destination must be an existing local directory without symbolic links.",
            ) from exc
        try:
            status = pinned.directory_stat()
        except BaseException:
            pinned.close()
            raise
        return _ArtifactDirectory(path, pinned, status.st_dev, status.st_ino)
    required = (os.open, os.link, os.unlink, os.rename)
    flags = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if (
        no_follow is None
        or any(operation not in os.supports_dir_fd for operation in required)
        or os.link not in os.supports_follow_symlinks
        or os.stat not in os.supports_follow_symlinks
        or not path.is_absolute()
    ):
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Secure local artifact publication is unavailable for this destination.",
        )
    flags |= no_follow
    try:
        descriptor = os.open(path.anchor, flags)
    except OSError as exc:
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Artifact destination must be an existing local directory without symbolic links.",
        ) from exc
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode):
            raise AdhelmError(
                "UNSAFE_ARTIFACT_DESTINATION",
                "Artifact destination must be an existing local directory.",
            )
        return _ArtifactDirectory(path, descriptor, status.st_dev, status.st_ino)
    except (OSError, AdhelmError) as exc:
        os.close(descriptor)
        if isinstance(exc, AdhelmError):
            raise
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Artifact destination must be an existing local directory without symbolic links.",
        ) from exc


def _assert_directory_current(directory: _ArtifactDirectory) -> None:
    current = _open_artifact_directory(directory.path)
    try:
        if (current.device, current.inode) != (directory.device, directory.inode):
            raise AdhelmError(
                "UNSAFE_ARTIFACT_DESTINATION",
                "Artifact destination changed while output was being generated.",
            )
    finally:
        current.close()


def _document(retained: RetainedArtifactInput, key: bytes, *, max_rows: int) -> dict[str, object]:
    if isinstance(retained.value, NormalizedReport):
        body = _report_document(retained.value, key, max_rows=max_rows)
    elif isinstance(retained.value, AuditResult):
        body = _audit_document(retained.value, key, max_rows=max_rows)
    else:  # RetainedArtifactInput enforces this at construction.
        raise AdhelmError(
            "UNSUPPORTED_ARTIFACT_INPUT", "Artifact input is not a reviewed retained type."
        )
    return {
        "schema_version": 1,
        "artifact_kind": body["artifact_kind"],
        "source_evidence": {
            "source_record_digest": retained.source_record_digest,
            "checkpoint_digest": retained.checkpoint_digest,
        },
        "content": body,
    }


def _report_document(report: NormalizedReport, key: bytes, *, max_rows: int) -> dict[str, object]:
    if len(report.performance_rows) + len(report.conversion_rows) > max_rows:
        raise AdhelmError(
            "ARTIFACT_ROW_LIMIT_EXCEEDED", "Report exceeds the supported artifact row bound."
        )
    return {
        "artifact_kind": "normalized_report",
        "report": _redact_payload(report.model_dump(mode="json"), key, False),
    }


def _audit_document(audit: AuditResult, key: bytes, *, max_rows: int) -> dict[str, object]:
    if len(audit.findings) + len(audit.recommendations) > max_rows:
        raise AdhelmError(
            "ARTIFACT_ROW_LIMIT_EXCEEDED", "Audit exceeds the supported artifact row bound."
        )
    return {
        "artifact_kind": "audit_report",
        "audit": _redact_payload(audit.model_dump(mode="json"), key, False),
    }


def _render(document: dict[str, object], format_name: ArtifactFormat) -> bytes:
    if format_name == "json":
        return canonical_bytes(document) + b"\n"
    if format_name == "csv":
        return _csv_document(document)
    if format_name == "html":
        return _html_document(document)
    raise AdhelmError("UNSUPPORTED_ARTIFACT_FORMAT", "Requested artifact format is unsupported.")


def _csv_document(document: dict[str, object]) -> bytes:
    output = StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=["section", "ordinal", "field", "value"],
        lineterminator="\r\n",
        extrasaction="raise",
    )
    writer.writeheader()
    for section, ordinal, field, value in _scalar_rows(document):
        writer.writerow(
            {
                "section": escape_formula(section),
                "ordinal": str(ordinal),
                "field": escape_formula(field),
                "value": escape_formula(value),
            }
        )
    return output.getvalue().encode("utf-8")


def _scalar_rows(
    value: object, *, prefix: str = "", ordinal: int = 0
) -> list[tuple[str, int, str, str]]:
    rows: list[tuple[str, int, str, str]] = []
    if isinstance(value, dict):
        for key in sorted(value):
            rows.extend(_scalar_rows(value[key], prefix=f"{prefix}/{key}", ordinal=ordinal))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            rows.extend(_scalar_rows(child, prefix=prefix, ordinal=index))
    else:
        rendered = (
            "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
        )
        section, _, field = prefix.rpartition("/")
        rows.append((section or "/", ordinal, field, rendered))
    return rows


def _html_document(document: dict[str, object]) -> bytes:
    title = "AdHelm retained evidence artifact"
    rows = "".join(
        "<tr><td>"
        + html.escape(section)
        + "</td><td>"
        + str(ordinal)
        + "</td><td>"
        + html.escape(field)
        + "</td><td>"
        + html.escape(value)
        + "</td></tr>"
        for section, ordinal, field, value in _scalar_rows(document)
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title></head><body><h1>{html.escape(title)}</h1>"
        "<p>Static local rendering. It contains no scripts, remote assets, or action links.</p>"
        "<table><thead><tr><th>Section</th><th>Ordinal</th><th>Field</th><th>Value</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></body></html>"
    ).encode()


def _write_versioned(
    directory: _ArtifactDirectory,
    base: str,
    format_name: ArtifactFormat,
    contents: bytes,
    *,
    reuse: bool,
    replace: bool,
) -> tuple[Path, bool]:
    version = 1
    publication_races = 0
    while version < 10_000:
        suffix = "" if version == 1 else f".v{version}"
        name = f"{base}{suffix}.{format_name}"
        path = directory.path / name
        try:
            matches = _existing_file_matches(directory, name, contents)
        except _ArtifactLinkInProgress:
            publication_races += 1
            if publication_races > 16:
                raise AdhelmError(
                    "ARTIFACT_PUBLICATION_RACE",
                    "Artifact destination changed repeatedly during publication.",
                ) from None
            time.sleep(0.005)
            continue
        except FileNotFoundError:
            try:
                _create_bytes(directory, name, contents)
            except FileExistsError:
                # Another local process won this name between lookup and link.
                # Reinspect the same name: matching bytes can be reused and a
                # nonmatching artifact gets a distinct version.
                publication_races += 1
                if publication_races > 16:
                    raise AdhelmError(
                        "ARTIFACT_PUBLICATION_RACE",
                        "Artifact destination changed repeatedly during publication.",
                    ) from None
                continue
            return path, False
        if reuse and matches:
            return path, True
        if replace and version == 1:
            _replace_bytes(directory, name, contents)
            return path, False
        version += 1
    raise AdhelmError("ARTIFACT_VERSION_EXHAUSTED", "No safe artifact version is available.")


def _artifact_status(
    directory: _ArtifactDirectory, name: str, *, allow_link_in_progress: bool = False
) -> os.stat_result:
    try:
        status = stat_child(directory.handle, name)
    except FileNotFoundError as exc:
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Artifact path changed while output was being generated.",
        ) from exc
    _validate_private_regular(status, allow_link_in_progress=allow_link_in_progress)
    return status


def _assert_artifact_current(
    directory: _ArtifactDirectory,
    name: str,
    expected: os.stat_result,
    contents: bytes,
    *,
    allow_link_in_progress: bool,
) -> None:
    try:
        current = _artifact_status(directory, name, allow_link_in_progress=allow_link_in_progress)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise AdhelmError(
                "UNSAFE_ARTIFACT_DESTINATION",
                "Artifact path changed while output was being generated.",
            )
        matches = _existing_file_matches(
            directory, name, contents, allow_link_in_progress=allow_link_in_progress
        )
    except FileNotFoundError as exc:
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Artifact path changed while output was being generated.",
        ) from exc
    except _ArtifactLinkInProgress:
        raise
    except OSError as exc:
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Artifact path changed while output was being generated.",
        ) from exc
    if not matches:
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Artifact bytes changed while output was being generated.",
        )


def _existing_file_matches(
    directory: _ArtifactDirectory,
    name: str,
    contents: bytes,
    *,
    allow_link_in_progress: bool = False,
) -> bool:
    """Read only the exact private regular target through the opened directory."""
    expected = stat_child(directory.handle, name)
    _validate_private_regular(expected, allow_link_in_progress=allow_link_in_progress)
    if expected.st_size != len(contents):
        return False
    descriptor = open_child_for_read(directory.handle, name)
    try:
        opened = fstat(descriptor)
        _validate_private_regular(opened, allow_link_in_progress=allow_link_in_progress)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise AdhelmError(
                "UNSAFE_ARTIFACT_DESTINATION",
                "Artifact path changed while checking reusable output.",
            )
        if opened.st_size != len(contents):
            return False
        chunks: list[bytes] = []
        remaining = len(contents) + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        current = stat_child(directory.handle, name)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise AdhelmError(
                "UNSAFE_ARTIFACT_DESTINATION",
                "Artifact path changed while checking reusable output.",
            )
        return b"".join(chunks) == contents
    finally:
        os.close(descriptor)


def _validate_private_regular(
    status: os.stat_result, *, allow_link_in_progress: bool = False
) -> None:
    if not stat.S_ISREG(status.st_mode):
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION", "Artifact path must be a private regular file."
        )
    if stat.S_IMODE(status.st_mode) != 0o600:
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Artifact path must have private single-link permissions.",
        )
    if status.st_nlink == 2:
        if not allow_link_in_progress:
            raise _ArtifactLinkInProgress()
    elif status.st_nlink != 1:
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION",
            "Artifact path must have private single-link permissions.",
        )
    getuid = getattr(os, "getuid", None)
    if getuid is not None and status.st_uid != getuid():
        raise AdhelmError(
            "UNSAFE_ARTIFACT_DESTINATION", "Artifact path must be owned by the current local user."
        )


def _open_temporary(directory: _ArtifactDirectory, name: str) -> tuple[int, str]:
    for _ in range(16):
        temporary = f".{name}.tmp-{secrets.token_hex(16)}"
        try:
            return create_private_child(directory.handle, temporary), temporary
        except FileExistsError:
            continue
    raise AdhelmError(
        "ARTIFACT_PUBLICATION_RACE", "Temporary artifact name allocation did not stabilize."
    )


def _create_bytes(directory: _ArtifactDirectory, name: str, contents: bytes) -> None:
    descriptor, temporary = _open_temporary(directory, name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _set_private_mode(handle.fileno(), 0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is exclusive and never follows/replaces a target symlink.
        link_child(directory.handle, temporary, name)
        published = True
    finally:
        try:
            unlink_child(directory.handle, temporary)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if published:
                raise _ArtifactPublishedCleanupError(directory.path / name) from exc
            raise


def _replace_bytes(directory: _ArtifactDirectory, name: str, contents: bytes) -> None:
    descriptor, temporary = _open_temporary(directory, name)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _set_private_mode(handle.fileno(), 0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        # A rename through the opened directory is an atomic replacement on the
        # supported local platforms and never re-resolves the configured path.
        replace_child(directory.handle, temporary, name)
        replaced = True
    finally:
        if not replaced:
            try:
                unlink_child(directory.handle, temporary)
            except FileNotFoundError:
                pass


def _fsync_directory(directory: _ArtifactDirectory | Path) -> None:
    if sys.platform == "win32":
        # Windows has no directory fsync. Each artifact file is flushed before it
        # is published and NTFS journals the directory entry.
        return
    if isinstance(directory, _ArtifactDirectory):
        assert isinstance(directory.handle, int)  # Windows returned above
        os.fsync(directory.handle)
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
