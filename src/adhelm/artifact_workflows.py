"""Report-artifact orchestration over verified retained local evidence only."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from adhelm.artifacts import (
    ArtifactContext,
    ArtifactDecision,
    ArtifactGenerationError,
    ArtifactMetadata,
    RetainedArtifactInput,
    _artifact_status,
    _ArtifactDirectory,
    _fsync_directory,
    _open_artifact_directory,
    _set_private_mode,
    generate_artifacts,
    resolve_artifact_policy,
)
from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.directory_access import (
    create_private_child,
    fstat,
    open_child_for_read,
    replace_child,
    stat_child,
    unlink_child,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import OutputPreferences, Provider
from adhelm.reporting import NormalizedReport
from adhelm.saved_reports import load_report

_HEX = re.compile(r"^[0-9a-f]{64}$")
_CACHE_SCHEMA_VERSION = 1
_CACHE_MAX_BYTES = 262_144
_CACHE_DIRECTORY_NAME = "artifact-cache"
_CACHE_FILE_NAME = "report-artifacts.json"


@dataclass(frozen=True)
class ReportArtifactPreflight:
    completion: ArtifactDecision
    meaningful_change: ArtifactDecision


@dataclass(frozen=True)
class ReportArtifactResult:
    effective_decision: ArtifactDecision
    artifacts: tuple[ArtifactMetadata, ...]
    source_record_digest: str | None
    checkpoint_digest: str | None
    reused_prior: bool = False
    generation_failure_code: str | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "effective_policy": {
                "outcome": self.effective_decision.outcome,
                "reason": self.effective_decision.reason,
                "formats": list(self.effective_decision.formats),
                "preferences_digest": self.effective_decision.preferences_digest,
            },
            "source_record_digest": self.source_record_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "reused_prior": self.reused_prior,
            "generation_failure_code": self.generation_failure_code,
            "artifacts": [
                {
                    "path": str(item.path),
                    "format": item.format,
                    "reused": item.reused,
                    "artifact_digest": item.artifact_digest,
                    "input_digest": item.input_digest,
                    "settings_digest": item.settings_digest,
                }
                for item in self.artifacts
            ],
        }


def preflight_report_artifacts(
    preferences: OutputPreferences,
    *,
    provider: Provider,
    profile_id: str,
    ephemeral: bool,
) -> ReportArtifactPreflight:
    """Resolve every possible report rule before credential or HTTP access."""
    completion = resolve_artifact_policy(
        ArtifactContext(
            workflow="report",
            stage="completion",
            purpose="result",
            provider=provider,
            profile_id=profile_id,
        ),
        preferences,
    )
    meaningful_change = resolve_artifact_policy(
        ArtifactContext(
            workflow="report",
            stage="meaningful_change",
            purpose="result",
            provider=provider,
            profile_id=profile_id,
        ),
        preferences,
    )
    for decision in (completion, meaningful_change):
        if decision.outcome == "NEEDS_CONFIGURATION":
            raise AdhelmError(
                "ARTIFACT_CONFIGURATION_REQUIRED",
                "A matching automatic artifact rule needs local configuration.",
                next_action="Configure supported formats and an existing local output directory.",
            )
    if ephemeral and any(
        decision.outcome == "GENERATE" for decision in (completion, meaningful_change)
    ):
        raise AdhelmError(
            "EPHEMERAL_ARTIFACT_REQUIRES_RETENTION",
            "Automatic report artifacts require a verified retained report.",
            next_action="Remove --ephemeral or disable the matching artifact rule.",
        )
    return ReportArtifactPreflight(completion, meaningful_change)


def finalize_report_artifacts(
    root: Path,
    ledger: Ledger,
    *,
    stored_record: dict[str, Any],
    preflight: ReportArtifactPreflight,
    key: bytes,
) -> ReportArtifactResult:
    """Render a selected rule from a post-append verified checkpoint.

    The cache is only a derived path/index lookup.  It cannot establish report
    truth: every cache reuse first proves its referenced report remains in the
    verified performance ledger.
    """
    record_digest = _digest_field(stored_record.get("record_digest"))
    current, provenance = load_report(ledger, record_digest)
    checkpoint_digest = _digest_field(provenance.get("checkpoint_digest"))
    observation_key = _observation_key(stored_record)
    prior = _prior_report_record(ledger, observation_key, stored_record)
    changed = prior is None or _semantic_report_digest(current) != _semantic_digest_of_record(
        ledger, prior
    )
    decision = _effective_decision(preflight, changed)
    if decision.outcome != "GENERATE":
        return ReportArtifactResult(decision, (), record_digest, checkpoint_digest)

    semantic_digest = _semantic_report_digest(current)
    try:
        cached = _load_cached_artifacts(
            root,
            ledger,
            observation_key=observation_key,
            semantic_digest=semantic_digest,
            decision=decision,
            key=key,
        )
    except (AdhelmError, OSError) as exc:
        return ReportArtifactResult(
            decision,
            (),
            record_digest,
            checkpoint_digest,
            generation_failure_code=exc.code
            if isinstance(exc, AdhelmError)
            else "ARTIFACT_GENERATION_FAILED",
        )
    if not changed and cached is not None:
        return ReportArtifactResult(
            decision,
            cached,
            cached[0].source_record_digest,
            cached[0].checkpoint_digest,
            reused_prior=True,
        )
    if not changed and decision is preflight.meaningful_change:
        suppressed = replace(
            decision,
            outcome="SUPPRESSED",
            reason="MEANINGFUL_CHANGE_NOT_DETECTED",
            formats=(),
        )
        return ReportArtifactResult(suppressed, (), record_digest, checkpoint_digest)

    retained = RetainedArtifactInput(current, record_digest, checkpoint_digest)
    artifacts: tuple[ArtifactMetadata, ...] = ()
    try:
        artifacts = generate_artifacts(retained, decision, key=key)
        _save_cached_artifacts(
            root,
            observation_key=observation_key,
            semantic_digest=semantic_digest,
            artifacts=artifacts,
            decision=decision,
            key=key,
        )
    except ArtifactGenerationError as exc:
        return ReportArtifactResult(
            decision,
            exc.completed,
            record_digest,
            checkpoint_digest,
            generation_failure_code=exc.failure_code,
        )
    except (AdhelmError, OSError) as exc:
        return ReportArtifactResult(
            decision,
            artifacts,
            record_digest,
            checkpoint_digest,
            generation_failure_code=exc.code
            if isinstance(exc, AdhelmError)
            else "ARTIFACT_GENERATION_FAILED",
        )
    return ReportArtifactResult(decision, artifacts, record_digest, checkpoint_digest)


def _effective_decision(preflight: ReportArtifactPreflight, changed: bool) -> ArtifactDecision:
    if changed and preflight.meaningful_change.outcome == "GENERATE":
        return preflight.meaningful_change
    if preflight.completion.outcome == "GENERATE":
        return preflight.completion
    return preflight.meaningful_change


def _semantic_report_digest(report: NormalizedReport) -> str:
    """Compare report meaning, retaining freshness and all quality statuses."""
    return digest(
        {
            "envelope": report.envelope.model_dump(
                mode="json",
                exclude={"report_id", "observed_at", "source_ledger_checkpoints"},
            ),
            "performance_rows": [
                row.model_dump(mode="json", exclude={"report_id"})
                for row in report.performance_rows
            ],
            "conversion_rows": [
                row.model_dump(mode="json", exclude={"report_id"}) for row in report.conversion_rows
            ],
        }
    )


def _semantic_digest_of_record(ledger: Ledger, record: dict[str, Any]) -> str:
    report, _ = load_report(ledger, _digest_field(record.get("record_digest")))
    return _semantic_report_digest(report)


def _prior_report_record(
    ledger: Ledger, observation_key: str, current_record: dict[str, Any]
) -> dict[str, Any] | None:
    current_sequence = current_record.get("stream_sequence")
    if not isinstance(current_sequence, int) or current_sequence < 1:
        raise AdhelmError("REPORT_NOT_RETAINED", "Retained report has no valid stream sequence.")
    matches = [
        record
        for record in ledger.records("performance")
        if record["record_kind"] == "PERFORMANCE_OBSERVATION"
        and record["payload"]["observation_key"] == observation_key
        and isinstance(record.get("stream_sequence"), int)
        and record["stream_sequence"] < current_sequence
    ]
    return matches[-1] if matches else None


def _observation_key(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("observation_key"), str):
        raise AdhelmError(
            "REPORT_NOT_RETAINED", "Retained report has no valid observation identity."
        )
    return str(payload["observation_key"])


def _digest_field(value: object) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise AdhelmError("REPORT_NOT_RETAINED", "Retained report evidence digest is invalid.")
    return value


def _open_cache_directory(root: Path) -> _ArtifactDirectory:
    """Open the derived cache through stable, no-follow directory descriptors.

    On Windows the cache directory keeps the whole checked chain above it pinned
    (see ``PinnedDirectory``) until it is closed, so path-addressed operations on
    the cache cannot resolve through a replaced ancestor.
    """
    try:
        root_directory = _open_artifact_directory(root)
    except (AdhelmError, OSError) as exc:
        raise AdhelmError("UNSAFE_ARTIFACT_CACHE", "Artifact cache root is unsafe.") from exc
    try:
        cache = _open_cache_child(root, root_directory)
    except BaseException:
        _close_quietly(root_directory)
        raise
    try:
        root_directory.close()
    except OSError as exc:
        _close_quietly(cache)
        raise AdhelmError("UNSAFE_ARTIFACT_CACHE", "Artifact cache directory is unsafe.") from exc
    return cache


def _close_quietly(directory: _ArtifactDirectory) -> None:
    try:
        directory.close()
    except OSError:
        pass


def _open_cache_child(root: Path, root_directory: _ArtifactDirectory) -> _ArtifactDirectory:
    parent = root_directory.handle
    descriptor: int | None = None
    try:
        if not isinstance(parent, int):
            pinned = parent.open_child_directory(_CACHE_DIRECTORY_NAME, create=True, private=True)
            try:
                status = pinned.directory_stat()
            except BaseException:
                pinned.close()
                raise
            return _ArtifactDirectory(
                root / _CACHE_DIRECTORY_NAME, pinned, status.st_dev, status.st_ino
            )
        if os.mkdir not in os.supports_dir_fd:
            raise AdhelmError(
                "UNSAFE_ARTIFACT_CACHE",
                "Secure local artifact cache publication is unavailable.",
            )
        created = False
        try:
            os.mkdir(_CACHE_DIRECTORY_NAME, 0o700, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(_CACHE_DIRECTORY_NAME, flags, dir_fd=parent)
        if created:
            _set_private_mode(descriptor, 0o700)
        status = os.fstat(descriptor)
        _validate_private_cache_directory(status)
        return _ArtifactDirectory(
            root / _CACHE_DIRECTORY_NAME, descriptor, status.st_dev, status.st_ino
        )
    except (OSError, AdhelmError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, AdhelmError) and exc.code == "UNSAFE_ARTIFACT_CACHE":
            raise
        raise AdhelmError("UNSAFE_ARTIFACT_CACHE", "Artifact cache directory is unsafe.") from exc


def _validate_private_cache_directory(status: os.stat_result) -> None:
    getuid = getattr(os, "getuid", None)
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o700
        or (getuid is not None and status.st_uid != getuid())
    ):
        raise AdhelmError("UNSAFE_ARTIFACT_CACHE", "Artifact cache directory is unsafe.")


def _assert_cache_directory_current(root: Path, directory: _ArtifactDirectory) -> None:
    current = _open_cache_directory(root)
    try:
        if (current.device, current.inode) != (directory.device, directory.inode):
            raise AdhelmError(
                "UNSAFE_ARTIFACT_CACHE", "Artifact cache path changed while it was being written."
            )
    finally:
        current.close()


def _cache_file_status(
    directory: _ArtifactDirectory, *, missing_ok: bool = False
) -> os.stat_result | None:
    try:
        status = stat_child(directory.handle, _CACHE_FILE_NAME)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache file disappeared.") from None
    getuid = getattr(os, "getuid", None)
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_nlink != 1
        or (getuid is not None and status.st_uid != getuid())
    ):
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache file is unsafe.")
    return status


def _status_fingerprint(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        getattr(status, "st_mtime_ns", 0),
        getattr(status, "st_ctime_ns", 0),
    )


def _read_cache_payload(directory: _ArtifactDirectory) -> object | None:
    expected = _cache_file_status(directory, missing_ok=True)
    if expected is None:
        return None
    if expected.st_size > _CACHE_MAX_BYTES:
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache exceeds its bounded size.")
    try:
        descriptor = open_child_for_read(directory.handle, _CACHE_FILE_NAME)
    except OSError as exc:
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache is unreadable.") from exc
    try:
        opened = fstat(descriptor)
        if _status_fingerprint(opened) != _status_fingerprint(expected):
            raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache changed while being read.")
        remaining = _CACHE_MAX_BYTES + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        current = _cache_file_status(directory)
        if current is None or _status_fingerprint(current) != _status_fingerprint(opened):
            raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache changed while being read.")
    finally:
        os.close(descriptor)
    encoded = b"".join(chunks)
    if len(encoded) > _CACHE_MAX_BYTES:
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache exceeds its bounded size.")
    try:
        return cast(object, parse_json(encoded))
    except (AdhelmError, ValueError) as exc:
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache is invalid.") from exc


def _open_cache_temporary(directory: _ArtifactDirectory) -> tuple[int, str]:
    for _ in range(16):
        name = f".report-artifacts-{secrets.token_hex(16)}"
        try:
            return create_private_child(directory.handle, name), name
        except FileExistsError:
            continue
    raise AdhelmError(
        "ARTIFACT_PUBLICATION_RACE", "Artifact cache temporary names did not stabilize."
    )


def _write_cache_payload(root: Path, directory: _ArtifactDirectory, encoded: bytes) -> None:
    descriptor, temporary = _open_cache_temporary(directory)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _set_private_mode(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_cache_directory_current(root, directory)
        replace_child(directory.handle, temporary, _CACHE_FILE_NAME)
        published = True
        _fsync_directory(directory)
        _assert_cache_directory_current(root, directory)
    finally:
        if not published:
            try:
                unlink_child(directory.handle, temporary)
            except FileNotFoundError:
                pass


def _load_cached_artifacts(
    root: Path,
    ledger: Ledger,
    *,
    observation_key: str,
    semantic_digest: str,
    decision: ArtifactDecision,
    key: bytes,
) -> tuple[ArtifactMetadata, ...] | None:
    if not decision.reuse_allowed or decision.preferences_digest is None:
        return None
    destination = _safe_cache_destination(decision)
    directory = _open_cache_directory(root)
    try:
        payload = _read_cache_payload(directory)
    finally:
        directory.close()
    if payload is None:
        return None
    entries = _validate_cache(payload, key=key)
    for entry in reversed(entries):
        if (
            entry["observation_key"] != observation_key
            or entry["semantic_digest"] != semantic_digest
            or entry["settings_digest"] != decision.preferences_digest
        ):
            continue
        try:
            _verify_cache_mac(entry, key)
            source, _ = load_report(ledger, entry["source_record_digest"])
            if _semantic_report_digest(source) != semantic_digest:
                raise ValueError("cache source report differs")
            source_record = next(
                record
                for record in ledger.records("performance")
                if record["record_digest"] == entry["source_record_digest"]
            )
            if _observation_key(source_record) != observation_key:
                raise ValueError("cache observation differs")
            _validate_entry_metadata(entry, destination, decision)
            artifacts = tuple(
                _metadata_from_cache(item, entry, destination, decision)
                for item in entry["artifacts"]
            )
        except (AdhelmError, ValueError, TypeError) as exc:
            raise AdhelmError(
                "ARTIFACT_CACHE_INVALID", "Artifact cache cannot be trusted."
            ) from exc
        return tuple(replace(item, reused=True) for item in artifacts)
    return None


def _save_cached_artifacts(
    root: Path,
    *,
    observation_key: str,
    semantic_digest: str,
    artifacts: tuple[ArtifactMetadata, ...],
    decision: ArtifactDecision,
    key: bytes,
) -> None:
    if not artifacts:
        return
    destination = _safe_cache_destination(decision)
    directory = _open_cache_directory(root)
    try:
        payload = _read_cache_payload(directory)
        entries = _validate_cache(payload, key=key) if payload is not None else []
        entry = {
            "observation_key": observation_key,
            "semantic_digest": semantic_digest,
            "settings_digest": artifacts[0].settings_digest,
            "source_record_digest": artifacts[0].source_record_digest,
            "checkpoint_digest": artifacts[0].checkpoint_digest,
            "artifacts": [
                {
                    "path": str(item.path),
                    "format": item.format,
                    "reused": item.reused,
                    "artifact_digest": item.artifact_digest,
                    "input_digest": item.input_digest,
                    "settings_digest": item.settings_digest,
                    "source_record_digest": item.source_record_digest,
                    "checkpoint_digest": item.checkpoint_digest,
                }
                for item in artifacts
            ],
        }
        _validate_entry_metadata(entry, destination, decision)
        entry["integrity_mac"] = _cache_mac(entry, key)
        entries = [
            prior
            for prior in entries
            if not (
                prior["observation_key"] == observation_key
                and prior["semantic_digest"] == semantic_digest
                and prior["settings_digest"] == entry["settings_digest"]
            )
        ]
        entries.append(entry)
        entries = entries[-100:]
        encoded = (
            canonical_bytes({"schema_version": _CACHE_SCHEMA_VERSION, "entries": entries}) + b"\n"
        )
        if len(encoded) > _CACHE_MAX_BYTES:
            raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache exceeds its bounded size.")
        _write_cache_payload(root, directory, encoded)
    finally:
        directory.close()


def _validate_cache(value: object, *, key: bytes | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "entries"}:
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has an invalid schema.")
    if value["schema_version"] != _CACHE_SCHEMA_VERSION or not isinstance(value["entries"], list):
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has an invalid schema.")
    if len(value["entries"]) > 100:
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has too many entries.")
    entries = value["entries"]
    if not all(isinstance(entry, dict) for entry in entries):
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has an invalid entry.")
    for entry in entries:
        _validate_cache_entry(entry)
        if key is not None:
            try:
                _verify_cache_mac(entry, key)
            except ValueError as exc:
                raise AdhelmError(
                    "ARTIFACT_CACHE_INVALID", "Artifact cache cannot be trusted."
                ) from exc
    return entries


def _validate_cache_entry(entry: dict[str, Any]) -> None:
    expected = {
        "observation_key",
        "semantic_digest",
        "settings_digest",
        "source_record_digest",
        "checkpoint_digest",
        "artifacts",
        "integrity_mac",
    }
    if set(entry) != expected or not isinstance(entry["observation_key"], str):
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has an invalid entry.")
    for name in (
        "semantic_digest",
        "settings_digest",
        "source_record_digest",
        "checkpoint_digest",
    ):
        _digest_field(entry[name])
    _digest_field(entry["integrity_mac"])
    artifacts = entry["artifacts"]
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 3:
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has invalid metadata.")
    for item in artifacts:
        if not isinstance(item, dict):
            raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has invalid metadata.")
        _metadata_from_cache_shape(item)


def _metadata_from_cache_shape(item: dict[str, Any]) -> ArtifactMetadata:
    expected = {
        "path",
        "format",
        "reused",
        "artifact_digest",
        "input_digest",
        "settings_digest",
        "source_record_digest",
        "checkpoint_digest",
    }
    if (
        set(item) != expected
        or not isinstance(item["path"], str)
        or not Path(item["path"]).is_absolute()
    ):
        raise ValueError("invalid artifact path")
    if item["format"] not in {"json", "csv", "html"} or not isinstance(item["reused"], bool):
        raise ValueError("invalid artifact metadata")
    for name in (
        "artifact_digest",
        "input_digest",
        "settings_digest",
        "source_record_digest",
        "checkpoint_digest",
    ):
        _digest_field(item[name])
    return ArtifactMetadata(
        path=Path(item["path"]),
        format=item["format"],
        reused=item["reused"],
        artifact_digest=item["artifact_digest"],
        input_digest=item["input_digest"],
        settings_digest=item["settings_digest"],
        source_record_digest=item["source_record_digest"],
        checkpoint_digest=item["checkpoint_digest"],
    )


def _metadata_from_cache(
    item: dict[str, Any],
    entry: dict[str, Any],
    destination: Path,
    decision: ArtifactDecision,
) -> ArtifactMetadata:
    metadata = _metadata_from_cache_shape(item)
    if (
        metadata.settings_digest != entry["settings_digest"]
        or metadata.source_record_digest != entry["source_record_digest"]
        or metadata.checkpoint_digest != entry["checkpoint_digest"]
        or metadata.settings_digest != decision.preferences_digest
        or metadata.path.parent != destination
        or metadata.format not in decision.formats
        or not metadata.path.name.startswith("artifact-")
        or metadata.path.suffix != f".{metadata.format}"
    ):
        raise ValueError("cache metadata does not match selected policy")
    try:
        contents = _read_cached_artifact_bytes(
            metadata.path, destination, decision.max_output_bytes
        )
    except (AdhelmError, OSError, ValueError) as exc:
        raise ValueError("cache artifact is unsafe") from exc
    if hashlib.sha256(contents).hexdigest() != metadata.artifact_digest:
        raise ValueError("cache artifact digest differs")
    return metadata


def _validate_entry_metadata(
    entry: dict[str, Any], destination: Path, decision: ArtifactDecision
) -> None:
    items = entry["artifacts"]
    if not isinstance(items, list) or len(items) != len(decision.formats):
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has unexpected formats.")
    formats = {item.get("format") for item in items if isinstance(item, dict)}
    if formats != set(decision.formats) or len(formats) != len(items):
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has unexpected formats.")
    for item in items:
        if not isinstance(item, dict):
            raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache has invalid metadata.")
        _metadata_from_cache(item, entry, destination, decision)


def _read_cached_artifact_bytes(path: Path, destination: Path, maximum_bytes: int | None) -> bytes:
    """Read one cached artifact through the same no-follow boundary as publication."""
    if maximum_bytes is None or path.parent != destination:
        raise ValueError("artifact output bound or destination is invalid")
    directory = _open_artifact_directory(destination)
    try:
        expected = _artifact_status(directory, path.name)
        if expected.st_size > maximum_bytes:
            raise ValueError("artifact exceeds configured bound")
        descriptor = open_child_for_read(directory.handle, path.name)
        try:
            opened = fstat(descriptor)
            if _status_fingerprint(opened) != _status_fingerprint(expected):
                raise ValueError("artifact changed while being read")
            remaining = maximum_bytes + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            current = _artifact_status(directory, path.name)
            if _status_fingerprint(current) != _status_fingerprint(opened):
                raise ValueError("artifact changed while being read")
        finally:
            os.close(descriptor)
    finally:
        directory.close()
    contents = b"".join(chunks)
    if len(contents) > maximum_bytes:
        raise ValueError("artifact exceeds configured bound")
    return contents


def _safe_cache_destination(decision: ArtifactDecision) -> Path:
    destination = decision.output_directory
    if destination is None or not destination.is_absolute():
        raise AdhelmError("ARTIFACT_CACHE_INVALID", "Artifact cache destination is invalid.")
    try:
        directory = _open_artifact_directory(destination)
    except (AdhelmError, OSError) as exc:
        raise AdhelmError(
            "ARTIFACT_CACHE_INVALID", "Artifact cache destination is unsafe."
        ) from exc
    directory.close()
    return destination


def _cache_mac(entry: dict[str, Any], key: bytes) -> str:
    unsigned = {name: value for name, value in entry.items() if name != "integrity_mac"}
    return hmac.new(key, canonical_bytes(unsigned), hashlib.sha256).hexdigest()


def _verify_cache_mac(entry: dict[str, Any], key: bytes) -> None:
    expected = _cache_mac(entry, key)
    value = entry.get("integrity_mac")
    if not isinstance(value, str) or not hmac.compare_digest(value, expected):
        raise ValueError("cache integrity proof differs")
