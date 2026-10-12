"""Validate an exact committed snapshot and two offline development builds.

Run with the project's locked development environment. This script creates local
evidence only; it never signs, tags, publishes, or asserts release readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DEFAULT_BRIDGE_SOURCE_SHA256 = "1377be2450de426cec82ce37fb9301e310bbbf201be32ea67c677cfe57722ef3"
_DEFAULT_BRIDGE_MODULE = "adhelm/_generated_installed_approval_bridge_binding.py"
_DEFAULT_META_BRIDGE_SOURCE_SHA256 = (
    "00b530fa0bc985a132695b75fa55b5d31e18f58fc86d7a2d1c129fa8383c2f2c"
)
_DEFAULT_META_BRIDGE_MODULE = "adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
_UNCONFIGURED_BRIDGE_SOURCES = (
    (_DEFAULT_BRIDGE_MODULE, _DEFAULT_BRIDGE_SOURCE_SHA256),
    (_DEFAULT_META_BRIDGE_MODULE, _DEFAULT_META_BRIDGE_SOURCE_SHA256),
)
_DEFAULT_PYTEST_WORKERS = 1
_DEFAULT_PYTEST_TIMEOUT_SECONDS = 900
_DEFAULT_ULTRA_TIMEOUT_SECONDS = 450
_OTHER_CHECK_TIMEOUT_SECONDS = 300
_PYTEST_SUMMARY_DURATION = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")
_PYTEST_SUMMARY_COUNT = re.compile(r"\b(\d+)\s+(passed|failed|skipped|errors?)\b")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_unconfigured_bridge_source(snapshot: Path) -> None:
    for module, expected_sha256 in _UNCONFIGURED_BRIDGE_SOURCES:
        source = snapshot / "src" / module
        if not source.is_file() or sha256(source) != expected_sha256:
            raise RuntimeError(
                "Development execution validation requires the exact unconfigured bridge source."
            )


def require_unconfigured_bridge_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as wheel_archive:
        for module, expected_sha256 in _UNCONFIGURED_BRIDGE_SOURCES:
            if (
                wheel_archive.namelist().count(module) != 1
                or hashlib.sha256(wheel_archive.read(module)).hexdigest() != expected_sha256
            ):
                raise RuntimeError(
                    "Configured bridge wheels must never be installed by this validator."
                )


def meta_pause_planning_smoke(call: Callable[..., dict[str, Any]], directory: Path) -> None:
    """Check the installed local contract with every protected boundary blocked."""
    schema = call("schema", "meta-campaign-pause-plan-request")["data"]["schema"]
    properties = schema.get("properties", {})
    if (
        schema.get("additionalProperties") is not False
        or set(properties) != {"schema_version", "profile_id", "campaign_id", "expires_in_seconds"}
        or set(schema.get("required", [])) != {"profile_id", "campaign_id"}
        or properties["schema_version"].get("const") != 1
        or properties["campaign_id"].get("type") != "string"
        or properties["campaign_id"].get("pattern") != "^[1-9][0-9]{0,39}$"
        or properties["expires_in_seconds"].get("type") != "integer"
        or properties["expires_in_seconds"].get("minimum") != 60
        or properties["expires_in_seconds"].get("maximum") != 900
    ):
        raise RuntimeError("Installed Meta pause request lost its closed local bounds.")
    catalog = call("plan", "meta-campaign-pause", "catalog")
    if (
        catalog["data"]["plans"]
        or catalog["total_count"] != 0
        or catalog["data"]["execution_authority"] is not False
        or catalog["data"]["remote_mutations"] is not False
    ):
        raise RuntimeError("Installed Meta pause catalog lost its historical-only contract.")
    request = directory / "installed-meta-pause-refusal.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-missing-meta-edit",
                "campaign_id": "987654321",
                "expires_in_seconds": 300,
            }
        )
    )
    refused = call(
        "plan", "meta-campaign-pause", "create", "--input", str(request), expected_exit=4
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "META_TOKEN_EDIT_PROFILE_REQUIRED"
        or refused["data"]
        or "987654321" in json.dumps(refused)
    ):
        raise RuntimeError("Installed Meta pause planning did not refuse the missing EDIT profile.")


def meta_pause_artifact_smoke(call: Callable[..., dict[str, Any]], directory: Path) -> None:
    """Verify installed historical-output contracts without keys or provider access."""
    schema = call("schema", "meta-campaign-pause-plan-artifact-render")["data"]["schema"]
    properties = schema.get("properties", {})
    if (
        schema.get("additionalProperties") is not False
        or set(properties)
        != {"schema_version", "formats", "output_directory", "replacement_authorized"}
        or properties["schema_version"].get("const") != 1
        or "formats" not in schema.get("required", [])
        or properties["formats"].get("minItems") != 1
        or properties["formats"].get("maxItems") != 3
        or properties["formats"].get("items", {}).get("enum") != ["json", "csv", "html"]
    ):
        raise RuntimeError("Installed Meta artifact request lost its closed local-output bounds.")
    document = call("schema", "meta-campaign-pause-plan-artifact-document", "--root-only")["data"][
        "schema"
    ]
    if (
        document.get("additionalProperties") is not False
        or document.get("properties", {}).get("artifact_kind", {}).get("const")
        != "meta_campaign_pause_plan_review"
        or set(document.get("required", [])) != {"source_evidence", "content"}
    ):
        raise RuntimeError("Installed Meta artifact document lost its retained-history binding.")
    destination = directory / "installed-meta-artifacts"
    destination.mkdir(mode=0o700)
    request = directory / "installed-meta-artifact-refusal.json"
    request.write_text(
        json.dumps({"schema_version": 1, "formats": ["json"], "output_directory": str(destination)})
    )
    refused = call(
        "artifacts",
        "meta-campaign-pause",
        "0" * 64,
        "--input",
        str(request),
        expected_exit=3,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "META_PAUSE_PLAN_NOT_RETAINED"
        or refused["data"]
        or list(destination.iterdir())
    ):
        raise RuntimeError("Installed Meta artifacts did not refuse the missing retained source.")


def google_ads_pause_artifact_smoke(call: Callable[..., dict[str, Any]], directory: Path) -> None:
    """Verify installed Google historical-output contracts without keys or provider access."""
    schema = call("schema", "google-ads-campaign-pause-plan-artifact-render")["data"]["schema"]
    properties = schema.get("properties", {})
    if (
        schema.get("additionalProperties") is not False
        or set(properties)
        != {"schema_version", "formats", "output_directory", "replacement_authorized"}
        or properties["schema_version"].get("const") != 1
        or "formats" not in schema.get("required", [])
        or properties["formats"].get("minItems") != 1
        or properties["formats"].get("maxItems") != 3
        or properties["formats"].get("items", {}).get("enum") != ["json", "csv", "html"]
    ):
        raise RuntimeError("Installed Google artifact request lost its closed local-output bounds.")
    document = call("schema", "google-ads-campaign-pause-plan-artifact-document", "--root-only")[
        "data"
    ]["schema"]
    if (
        document.get("additionalProperties") is not False
        or document.get("properties", {}).get("artifact_kind", {}).get("const")
        != "google_ads_campaign_pause_plan_review"
        or set(document.get("required", [])) != {"source_evidence", "content"}
    ):
        raise RuntimeError("Installed Google artifact document lost its retained-history binding.")
    destination = directory / "installed-google-ads-artifacts"
    destination.mkdir(mode=0o700)
    request = directory / "installed-google-ads-artifact-refusal.json"
    request.write_text(
        json.dumps({"schema_version": 1, "formats": ["json"], "output_directory": str(destination)})
    )
    refused = call(
        "artifacts",
        "google-ads-campaign-pause",
        "0" * 64,
        "--input",
        str(request),
        expected_exit=3,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "GOOGLE_ADS_PAUSE_PLAN_NOT_RETAINED"
        or refused["data"]
        or list(destination.iterdir())
    ):
        raise RuntimeError("Installed Google artifacts did not refuse the missing retained source.")


def provider_audit_smoke(call: Callable[..., dict[str, Any]], directory: Path) -> None:
    """Check the installed provider-audit route refuses without retained evidence."""
    discovery = call("discover")["data"]
    contract = discovery.get("provider_audit", {})
    if (
        "audit --provider-rules" not in discovery["commands"]
        or "provider-audit-result" not in discovery["schemas"]
        or contract.get("live_provider_reads") != "NONE"
        or contract.get("ledger_appends") != "NONE"
        or contract.get("analytics_completeness") != "BOUNDED_NON_EXHAUSTIVE"
        or contract.get("read_boundary") != "INVENTORY_READ_COMPLETED_MARKER_REQUIRED"
        or contract.get("live_verified") is not False
        or contract.get("provider_ui_reconciled") is not False
    ):
        raise RuntimeError("Installed provider-audit discovery lost its truthful bounds.")
    policy = directory / "installed-audit-policy.json"
    policy.write_text(
        json.dumps(
            {"maximum_observation_age_seconds": 604800, "require_conversion_evidence": False}
        )
    )
    refused = call(
        "audit",
        "0" * 64,
        "--input",
        str(policy),
        "--provider-rules",
        expected_exit=3,
        timeout=30,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "REPORT_NOT_RETAINED"
    ):
        raise RuntimeError("Installed provider audit accepted unretained evidence.")


def meta_token_status_disclosure_smoke(call: Callable[..., dict[str, Any]]) -> None:
    """Check the installed Meta token status leaf still fails closed and claims nothing.

    `auth meta-token status` gained retained-expiry, purpose-clamped scope,
    revocation, and app-review disclosure fields.  None of them may turn an
    unknown profile into an answer, and the leaf must still refuse before any
    credential-store work.
    """
    discovery = call("discover")["data"]
    if "auth meta-token status" not in discovery["commands"]:
        raise RuntimeError("Installed Meta token status command is not discoverable.")
    refused = call(
        "auth", "meta-token", "status", "--profile", "unconfigured", expected_exit=2, timeout=30
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "META_TOKEN_PROFILE_REQUIRED"
        or refused["data"]
    ):
        raise RuntimeError("Installed Meta token status accepted an unconfigured profile.")


def tiktok_token_status_disclosure_smoke(call: Callable[..., dict[str, Any]]) -> None:
    """Check the installed TikTok token status leaf fails closed and claims nothing.

    `auth tiktok-token status` reports a locally recorded expiry, salted
    advertiser pseudonyms, and an explicit `NOT_DOCUMENTED_LOCALLY` permission
    literal.  None of that may turn an unknown profile into an answer, and the
    leaf must refuse before any credential-store work.  The TikTok rule module
    must also be discoverable on the provider-audit registry.

    `adhelm.cli` registers the `auth tiktok-token` group and lists its four leaves
    in `_COMMANDS`, so this smoke reads the installed command from `discover`
    exactly as the command-parity test does.
    """
    discovery = call("discover")["data"]
    if "tiktok_ads" not in discovery.get("provider_audit", {}).get("rule_modules", []):
        raise RuntimeError("Installed discovery lost the TikTok provider audit rule module.")
    if "auth tiktok-token status" not in discovery["commands"]:
        raise RuntimeError("Installed TikTok token status command is not discoverable.")
    refused = call(
        "auth",
        "tiktok-token",
        "status",
        "--profile",
        "installed-smoke-absent",
        expected_exit=2,
        timeout=30,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "TIKTOK_TOKEN_PROFILE_REQUIRED"
        or refused["data"]
    ):
        raise RuntimeError("Installed TikTok token status accepted an unconfigured profile.")


def reddit_token_status_smoke(call: Callable[..., dict[str, Any]]) -> None:
    """Check the installed Reddit token status leaf fails closed and claims nothing.

    `auth reddit-token status` reports a locally recorded expiry, a salted
    account pseudonym and revocation guidance, never a provider answer.  None of
    that may turn an unknown profile into an answer, the refusal diagnostic must
    survive the shared privacy screen, and the Reddit rule module must be
    discoverable on the provider-audit registry.
    """
    discovery = call("discover")["data"]
    if "reddit_ads" not in discovery.get("provider_audit", {}).get("rule_modules", []):
        raise RuntimeError("Installed discovery lost the Reddit provider audit rule module.")
    for leaf in ("enroll", "refresh", "status", "remove-local"):
        if f"auth reddit-token {leaf}" not in discovery["commands"]:
            raise RuntimeError("Installed Reddit token command is not discoverable.")
    refused = call(
        "auth", "reddit-token", "status", "--profile", "unconfigured", expected_exit=2, timeout=30
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "REDDIT_TOKEN_PROFILE_REQUIRED"
        or refused["data"]
    ):
        raise RuntimeError("Installed Reddit token status accepted an unconfigured profile.")


#: Every pair P03, P04, P04-OA, P04-MA and P05 registered on both
#: provider-neutral registries.  They are a registered command surface, not a
#: usable capability: the installed approval bridge still refuses
#: ``COMPOSITE_EXECUTION_PLAN``, so `execute plan` refuses two gates earlier,
#: before any credential.  The order is the registries' own sort by
#: ``(provider, operation)``.
_GOOGLE_ADS_PAUSE_PAIR = {"provider": "google_ads", "operation": "PAUSE"}
_GOOGLE_ADS_CREATE_PAIR = {"provider": "google_ads", "operation": "CREATE"}
_META_ADS_CREATE_PAIR = {"provider": "meta_ads", "operation": "CREATE"}
_OPENAI_ADS_CREATE_PAIR = {"provider": "openai_ads", "operation": "CREATE"}
#: P05.  ``ACTIVATE`` is the separate spend-explicit activation; ``UPDATE`` is
#: the budget, bid, and schedule maintenance.  ``ARCHIVE`` is delete-class and
#: is deliberately not registered, so it is neither compilable nor advertised.
_GOOGLE_ADS_ACTIVATE_PAIR = {"provider": "google_ads", "operation": "ACTIVATE"}
_GOOGLE_ADS_UPDATE_PAIR = {"provider": "google_ads", "operation": "UPDATE"}
_REGISTERED_EXECUTION_PAIRS = [
    _GOOGLE_ADS_ACTIVATE_PAIR,
    _GOOGLE_ADS_CREATE_PAIR,
    _GOOGLE_ADS_PAUSE_PAIR,
    _GOOGLE_ADS_UPDATE_PAIR,
    _META_ADS_CREATE_PAIR,
    _OPENAI_ADS_CREATE_PAIR,
]


def gtm_dependency_graph_smoke(call: Callable[..., dict[str, Any]]) -> None:
    """Check the installed GTM graph route is discoverable and refuses without evidence."""
    discovery = call("discover")["data"]
    if "measurement gtm-graph" not in discovery["commands"]:
        raise RuntimeError("Installed GTM dependency graph command is not discoverable.")
    refused = call("measurement", "gtm-graph", "0" * 64, expected_exit=3, timeout=30)
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "MEASUREMENT_NOT_RETAINED"
    ):
        raise RuntimeError("Installed GTM dependency graph accepted an unretained observation.")


def google_oauth_status_smoke(call: Callable[..., dict[str, Any]]) -> None:
    """Check the installed local Google OAuth status route is bounded and truthful.

    The installed CLI must advertise the command, register the Google Ads audit
    rule module, and refuse an unconfigured profile as invalid input rather
    than returning an empty status that could read as a healthy one.  No secret
    can appear in either result: this route contacts no Google endpoint.
    """
    discovery = call("discover")["data"]
    if "auth google-oauth status" not in discovery["commands"] or "google_ads" not in discovery.get(
        "provider_audit", {}
    ).get("rule_modules", []):
        raise RuntimeError("Installed Google discovery lost its status command or rule module.")
    refused = call(
        "auth",
        "google-oauth",
        "status",
        "--profile",
        "installed-smoke-absent",
        expected_exit=2,
        timeout=30,
    )
    if refused["outcome"] != "FAILED_CLOSED" or refused["diagnostics"][0]["code"] not in {
        "PROFILE_REQUIRED",
        "CONFIGURATION_INVALID",
    }:
        raise RuntimeError("Installed Google OAuth status accepted an unconfigured profile.")


def execution_plan_smoke(call: Callable[..., dict[str, Any]], directory: Path) -> None:
    """Check the installed generic plan route refuses with no compiler installed."""
    discovery = call("discover")["data"]
    contract = discovery.get("execution_plan", {})
    if (
        not {
            "plan execution create",
            "plan execution catalog",
            "plan execution show",
        }
        <= set(discovery["commands"])
        or "execution-plan-request" not in discovery["schemas"]
        or contract.get("planning") != "GENERIC_RECORD_INSTALLED_NO_PROVIDER_COMPILER"
        or contract.get("compilable_operations") != _REGISTERED_EXECUTION_PAIRS
        or contract.get("refusal_code") != "EXECUTION_PLAN_NOT_COMPILABLE"
        or contract.get("historical_review") != "REDACTED_LOCAL_HISTORY_ONLY"
        or contract.get("artifact_generation")
        != "NOT_IMPLEMENTED_USE_PER_PROVIDER_ARTIFACT_COMMANDS"
        or contract.get("execution") != "UNRELEASED"
        or contract.get("execution_command") != "execute plan"
        or contract.get("executable_operations") != _REGISTERED_EXECUTION_PAIRS
        or contract.get("execution_first_refusal_code") != "WRITES_NOT_RELEASED"
        or [gate["gate"] for gate in contract.get("execution_refusal_gates", [])]
        != [
            "RELEASE_POLICY_WRITES_RELEASED",
            "COMPILED_APPROVAL_BRIDGE_INSTALLED",
            "APPROVAL_KIND_RENDERED_BY_INSTALLED_HOST",
            "CONFIGURED_EXECUTION_ADAPTER",
        ]
        or contract.get("native_setup_prerequisite") != "REQUIRED"
        or contract.get("automatic_execution_from_plan_or_artifact") is not False
        or contract.get("default_runtime_provider_mutations") is not False
        or contract.get("live_provider_execution_verified") is not False
    ):
        raise RuntimeError("Installed generic plan discovery lost its truthful bounds.")
    # ``google_ads CREATE`` is compilable since P04, so the uncompilable pair
    # this smoke names is a provider with no registered planner at all.
    request = directory / "installed-execution-plan-refusal.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-missing-planner",
                "provider": "meta_ads",
                "operation": "CREATE",
                "account_ref": "meta-ads.installed-smoke",
            }
        )
    )
    refused = call(
        "plan",
        "execution",
        "create",
        "--provider",
        "meta_ads",
        "--operation",
        "CREATE",
        "--input",
        str(request),
        expected_exit=3,
        timeout=30,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "EXECUTION_PLAN_NOT_COMPILABLE"
        or "meta_ads CREATE" not in refused["diagnostics"][0]["message"]
        or refused["data"]
        or "meta-ads.installed-smoke" in json.dumps(refused)
    ):
        raise RuntimeError("Installed generic plan create did not refuse an uncompilable pair.")
    catalog = call("plan", "execution", "catalog", timeout=30)
    if catalog["data"]["plans"] or catalog["total_count"] != 0:
        raise RuntimeError("A refused generic plan create retained local history.")


def generic_execution_refusal_smoke(call: Callable[..., dict[str, Any]]) -> None:
    """Check both generic leaves are refused by the release policy, before credentials.

    Neither takes an input file: one positional record digest is the whole
    input, so a fixed all-zero digest is a complete request.  `recover catalog`
    is a local history read and must stay available and empty.
    """

    discovery = call("discover")["data"]
    recovery = discovery.get("attempt_recovery", {})
    if (
        not {"execute plan", "recover attempt", "recover catalog"} <= set(discovery["commands"])
        or recovery.get("commands") != ["recover attempt", "recover catalog"]
        or recovery.get("status") != "UNRELEASED"
        or recovery.get("executable_operations") != _REGISTERED_EXECUTION_PAIRS
        or recovery.get("first_refusal_code") != "WRITES_NOT_RELEASED"
        or len(recovery.get("refusal_gates", [])) != 4
        or recovery.get("attribution") != "NO_ACTOR_ATTRIBUTED"
        or recovery.get("settles_attempt") is not False
        or recovery.get("authorizes_retry") is not False
        or recovery.get("provider_mutations") is not False
        or recovery.get("live_provider_execution_verified") is not False
    ):
        raise RuntimeError("Installed attempt-recovery discovery lost its truthful bounds.")
    for command in (("execute", "plan"), ("recover", "attempt")):
        refused = call(*command, "0" * 64, expected_exit=3, timeout=30)
        if (
            refused["outcome"] != "FAILED_CLOSED"
            or refused["diagnostics"][0]["code"] != "WRITES_NOT_RELEASED"
            or refused["data"]
        ):
            raise RuntimeError("Installed generic execution was not refused by the release policy.")
    catalog = call("recover", "catalog", timeout=30)
    if (
        catalog["outcome"] != "UNRESOLVED_ATTEMPT_HISTORY_NO_EXECUTION_AUTHORITY"
        or catalog["data"]["attempts"]
        or catalog["total_count"] != 0
        or catalog["data"]["history_verification"] != "HASH_CHAIN_VERIFIED_LOCAL_SCAN"
        or catalog["data"]["blocking_effect"] != "BLOCKS_EVERY_LATER_GUARDED_WRITE"
        or catalog["data"]["recovery_authorizes_retry"] is not False
    ):
        raise RuntimeError("Installed unresolved attempt catalog lost its bounded contract.")


def google_ads_pause_execution_refusal_smoke(call: Callable[..., dict[str, Any]]) -> None:
    """Check the installed Google PAUSE execution route refuses, in its own order.

    P03 registered the ("google_ads", PAUSE) planner and adapter, so `discover`
    reports the pair from both registries and no longer says the capability is
    unimplemented.  Two refusals prove the pair is still not usable here: a
    retained plan that does not exist is refused by a local history read, and
    `execute plan` is refused by the release policy before it loads a plan,
    opens the adapter, or reads any credential.
    """

    discovery = call("discover")["data"]
    google = discovery.get("google_ads_campaign_pause", {})
    if (
        google.get("execution") != "UNRELEASED"
        or google.get("planning") != "RETAINED_DEVELOPMENT_FIXTURE_ONLY"
        or google.get("default_runtime_provider_mutations") is not False
        or google.get("live_provider_execution_verified") is not False
        or discovery.get("execution_plan", {}).get("executable_operations")
        != _REGISTERED_EXECUTION_PAIRS
    ):
        raise RuntimeError("Installed Google PAUSE execution discovery lost its truthful bounds.")
    missing = call("plan", "execution", "show", "0" * 64, expected_exit=3, timeout=30)
    if (
        missing["outcome"] != "FAILED_CLOSED"
        or missing["diagnostics"][0]["code"] != "EXECUTION_PLAN_NOT_RETAINED"
        or missing["data"]
    ):
        raise RuntimeError("Installed generic plan show accepted an unretained record digest.")
    refused = call("execute", "plan", "0" * 64, expected_exit=3, timeout=30)
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "WRITES_NOT_RELEASED"
        or refused["data"]
    ):
        raise RuntimeError(
            "Installed Google PAUSE execution was not refused by the release policy."
        )


def google_ads_create_execution_refusal_smoke(
    call: Callable[..., dict[str, Any]], directory: Path
) -> None:
    """Check the installed Google CREATE execution route refuses, in its own order.

    P04 registered the ("google_ads", CREATE) planner and adapter, so `discover`
    reports the pair from both registries.  The capability is still unusable in
    an ordinary build, and this proves it fails closed *before* any credential:
    `plan execution create` refuses a request that names no retained candidate
    selection, and it names no customer, campaign, or account reference back.
    """

    discovery = call("discover")["data"]
    if discovery.get("execution_plan", {}).get("compilable_operations") != (
        _REGISTERED_EXECUTION_PAIRS
    ):
        raise RuntimeError("Installed Google CREATE planning discovery lost its truthful bounds.")
    request = directory / "installed-google-ads-create-refusal.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-google-ads-create-smoke",
                "provider": "google_ads",
                "operation": "CREATE",
                "account_ref": "google_ads.customer.1234567890",
                "resource_ref": "google_ads.candidate." + "0" * 64,
            }
        )
    )
    refused = call(
        "plan",
        "execution",
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "CREATE",
        "--input",
        str(request),
        expected_exit=2,
        timeout=30,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "INVALID_GOOGLE_ADS_CREATE_EXECUTION_SELECTION"
        or refused["data"]
        or "1234567890" in json.dumps(refused)
    ):
        raise RuntimeError("Installed Google CREATE planning did not refuse before credentials.")
    catalog = call("plan", "execution", "catalog", timeout=30)
    if catalog["data"]["plans"]:
        raise RuntimeError("A refused Google CREATE plan create retained local history.")


def openai_ads_create_execution_refusal_smoke(
    call: Callable[..., dict[str, Any]], directory: Path
) -> None:
    """Check the installed OpenAI CREATE execution route refuses, in its own order.

    P04-OA registered the ("openai_ads", CREATE) planner and journaled
    child-egress adapter, so `discover` reports the pair from both registries.
    The capability is still unusable in an ordinary build, and this proves it
    fails closed *before* any credential: `plan execution create` refuses a
    request that names no retained candidate selection, and it names no ad
    account, campaign, or creative reference back.
    """

    discovery = call("discover")["data"]
    contract = discovery.get("execution_plan", {})
    if (
        contract.get("compilable_operations") != _REGISTERED_EXECUTION_PAIRS
        or contract.get("executable_operations") != _REGISTERED_EXECUTION_PAIRS
    ):
        raise RuntimeError("Installed OpenAI CREATE planning discovery lost its truthful bounds.")
    request = directory / "installed-openai-ads-create-refusal.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-openai-ads-create-smoke",
                "provider": "openai_ads",
                "operation": "CREATE",
                "account_ref": "openai_ads.account.account_installed_smoke",
                "resource_ref": "openai_ads.candidate." + "0" * 64,
            }
        )
    )
    refused = call(
        "plan",
        "execution",
        "create",
        "--provider",
        "openai_ads",
        "--operation",
        "CREATE",
        "--input",
        str(request),
        expected_exit=2,
        timeout=30,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "INVALID_OPENAI_ADS_CREATE_EXECUTION_SELECTION"
        or refused["data"]
        or "account_installed_smoke" in json.dumps(refused)
    ):
        raise RuntimeError("Installed OpenAI CREATE planning did not refuse before credentials.")
    catalog = call("plan", "execution", "catalog", timeout=30)
    if catalog["data"]["plans"]:
        raise RuntimeError("A refused OpenAI CREATE plan create retained local history.")


def google_ads_activate_execution_refusal_smoke(
    call: Callable[..., dict[str, Any]], directory: Path
) -> None:
    """Check the installed Google ACTIVATE route refuses, in its own order.

    P05 registered the ("google_ads", ACTIVATE) planner and adapter, so
    `discover` reports the pair from both registries.  Activation is the
    separate spend-explicit authorization, and this proves it fails closed
    *before* any credential: `plan execution create` refuses a request that
    names no retained CREATE plan selection, and it names no customer,
    campaign, budget, or account reference back.
    """

    discovery = call("discover")["data"]
    if discovery.get("execution_plan", {}).get("compilable_operations") != (
        _REGISTERED_EXECUTION_PAIRS
    ):
        raise RuntimeError("Installed Google ACTIVATE discovery lost its truthful bounds.")
    request = directory / "installed-google-ads-activate-refusal.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-google-ads-activate-smoke",
                "provider": "google_ads",
                "operation": "ACTIVATE",
                "account_ref": "google_ads.customer.1234567890",
                "resource_ref": "google_ads.create_plan." + "0" * 64,
            }
        )
    )
    refused = call(
        "plan",
        "execution",
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "ACTIVATE",
        "--input",
        str(request),
        expected_exit=2,
        timeout=30,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "INVALID_GOOGLE_ADS_ACTIVATE_EXECUTION_SELECTION"
        or refused["data"]
        or "1234567890" in json.dumps(refused)
    ):
        raise RuntimeError("Installed Google ACTIVATE planning did not refuse before credentials.")
    catalog = call("plan", "execution", "catalog", timeout=30)
    if catalog["data"]["plans"]:
        raise RuntimeError("A refused Google ACTIVATE plan create retained local history.")


def google_ads_maintain_execution_refusal_smoke(
    call: Callable[..., dict[str, Any]], directory: Path
) -> None:
    """Check the installed Google maintenance pair refuses before any credential.

    P05 registered ("google_ads", UPDATE) only; ("google_ads", ARCHIVE) is
    delete-class and stays unregistered until a cited reference read can supply
    AGENTS rule 11's dependency proof, so discovery does not advertise it.  The
    first request below is deliberately well formed apart from its operation:
    it carries a complete `CAMPAIGN_ARCHIVE` intent under `--operation UPDATE`,
    so the refusal proves the planner derives the operation from the intent and
    will not reach the irreversible path through an update request.
    """

    discovery = call("discover")["data"]
    if discovery.get("execution_plan", {}).get("executable_operations") != (
        _REGISTERED_EXECUTION_PAIRS
    ):
        raise RuntimeError("Installed Google maintenance discovery lost its truthful bounds.")
    request = directory / "installed-google-ads-maintain-refusal.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-google-ads-maintain-smoke",
                "provider": "google_ads",
                "operation": "UPDATE",
                "account_ref": "google_ads.customer.1234567890",
                "resource_ref": "google_ads.campaign_name." + "0" * 64,
                "maintain_selection": {
                    "schema_version": 1,
                    "selection_kind": "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN",
                    "campaign_name": "Installed smoke campaign",
                    "intent": {
                        "maintenance_kind": "CAMPAIGN_ARCHIVE",
                        "disposition": "IRREVERSIBLE_ARCHIVE",
                        "acknowledged_irreversible": True,
                    },
                },
            }
        )
    )
    refused = call(
        "plan",
        "execution",
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "UPDATE",
        "--input",
        str(request),
        expected_exit=2,
        timeout=30,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "INVALID_GOOGLE_ADS_MAINTAIN_EXECUTION_SELECTION"
        or refused["data"]
        or "1234567890" in json.dumps(refused)
    ):
        raise RuntimeError("Installed Google maintenance planning did not refuse the operation.")
    # The archive pair is not registered, so it refuses as uncompilable without
    # ever naming the campaign or customer back, and discovery never lists it.
    archive_request = directory / "installed-google-ads-archive-refusal.json"
    archive_request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-google-ads-archive-smoke",
                "provider": "google_ads",
                "operation": "ARCHIVE",
                "account_ref": "google_ads.customer.1234567890",
                "resource_ref": "google_ads.campaign_name." + "0" * 64,
                "maintain_selection": {
                    "schema_version": 1,
                    "selection_kind": "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN",
                    "campaign_name": "Installed smoke campaign",
                    "intent": {
                        "maintenance_kind": "CAMPAIGN_ARCHIVE",
                        "disposition": "IRREVERSIBLE_ARCHIVE",
                        "acknowledged_irreversible": True,
                    },
                },
            }
        )
    )
    archive_refused = call(
        "plan",
        "execution",
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "ARCHIVE",
        "--input",
        str(archive_request),
        expected_exit=3,
        timeout=30,
    )
    if (
        archive_refused["outcome"] != "FAILED_CLOSED"
        or archive_refused["diagnostics"][0]["code"] != "EXECUTION_PLAN_NOT_COMPILABLE"
        or archive_refused["data"]
        or "1234567890" in json.dumps(archive_refused)
        or "Installed smoke campaign" in json.dumps(archive_refused)
    ):
        raise RuntimeError("Installed Google ARCHIVE planning did not refuse as uncompilable.")
    catalog = call("plan", "execution", "catalog", timeout=30)
    if catalog["data"]["plans"]:
        raise RuntimeError("A refused Google maintenance plan create retained local history.")


def meta_ads_create_execution_refusal_smoke(
    call: Callable[..., dict[str, Any]], directory: Path
) -> None:
    """Check the installed Meta CREATE execution route refuses, in its own order.

    P04-MA registered the ("meta_ads", CREATE) planner and journaled
    child-egress adapter, so `discover` reports the pair from both registries.
    The capability is still unusable in an ordinary build, and this proves it
    fails closed *before* any credential: `plan execution create` refuses a
    request that names no retained candidate selection, and it names no ad
    account, campaign, Page, or creative reference back.
    """

    discovery = call("discover")["data"]
    contract = discovery.get("execution_plan", {})
    if (
        contract.get("compilable_operations") != _REGISTERED_EXECUTION_PAIRS
        or contract.get("executable_operations") != _REGISTERED_EXECUTION_PAIRS
    ):
        raise RuntimeError("Installed Meta CREATE planning discovery lost its truthful bounds.")
    request = directory / "installed-meta-ads-create-refusal.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-meta-ads-create-smoke",
                "provider": "meta_ads",
                "operation": "CREATE",
                "account_ref": "meta_ads.account.act_120210000000000",
                "resource_ref": "meta_ads.candidate." + "0" * 64,
            }
        )
    )
    refused = call(
        "plan",
        "execution",
        "create",
        "--provider",
        "meta_ads",
        "--operation",
        "CREATE",
        "--input",
        str(request),
        expected_exit=2,
        timeout=30,
    )
    if (
        refused["outcome"] != "FAILED_CLOSED"
        or refused["diagnostics"][0]["code"] != "INVALID_META_ADS_CREATE_EXECUTION_SELECTION"
        or refused["data"]
        or "120210000000000" in json.dumps(refused)
    ):
        raise RuntimeError("Installed Meta CREATE planning did not refuse before credentials.")
    catalog = call("plan", "execution", "catalog", timeout=30)
    if catalog["data"]["plans"]:
        raise RuntimeError("A refused Meta CREATE plan create retained local history.")


def creative_registration_smoke(call: Callable[..., dict[str, Any]], directory: Path) -> None:
    """Exercise the public local-media handoff without manufacturing ledger records."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    media = directory / "installed-creative.png"
    media.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30\x40\x50\x60"))
        + chunk(b"IEND", b"")
    )
    analyzed = call("creative", "analyze", "--input", str(media))["data"]
    for field in ("asset_record_digest", "asset_analysis_record_digest"):
        if re.fullmatch(r"[0-9a-f]{64}", str(analyzed.get(field))) is None:
            raise RuntimeError("Installed image analysis lost its retained source handoff.")
    schema = call("schema", "creative-registration-request", "--root-only")["data"]["schema"]
    if "source" not in schema["properties"]:
        raise RuntimeError("Installed creative registration schema is missing its typed source.")
    request: dict[str, Any] = {
        "schema_version": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "creative_family_id": "family:PRIVATE-FAMILY-ID-CREATIVE-SMOKE",
        "family_label": "PRIVATE-FAMILY-CREATIVE-SMOKE",
        "provider_format": "LOCAL_IMAGE",
        "locale": "en-US",
        "destination_ref": "destination:installed-creative",
        "components": [
            {
                "component_id": "component:installed-headline",
                "component_role": "HEADLINE",
                "ordinal": 0,
                "exact_text": "PRIVATE-COPY-CREATIVE-SMOKE",
                "normalized_text": "PRIVATE-COPY-CREATIVE-SMOKE",
                "language": "en-US",
                "provider_slot": "headline",
                "provenance": "DECLARED",
                "confidence_basis_points": 10_000,
                "extraction_status": "DECLARED",
            }
        ],
        "source": {
            "registration_kind": "IDENTITY_IMAGE",
            "asset_record_digest": analyzed["asset_record_digest"],
            "asset_analysis_record_digest": analyzed["asset_analysis_record_digest"],
        },
    }
    input_file = directory / "installed-creative-registration.json"
    input_file.write_text(json.dumps(request))
    registered = call("creative", "register", "--input", str(input_file))["data"]
    if (
        not registered.get("rendition_id")
        or not registered.get("fingerprint_id")
        or registered.get("local_registration_status") != "RETAINED_VERIFIED_LOCAL_ONLY"
        or registered.get("remote_mutations") is not False
        or registered.get("execution_authority") is not False
        or registered.get("deployment_status") != "NOT_CREATED"
        or registered.get("provider_currentness") != "NOT_EVALUATED"
    ):
        raise RuntimeError("Installed creative registration lost its derived-evidence boundary.")
    repeated = call("creative", "register", "--input", str(input_file))["data"]
    if repeated["record_digest"] != registered["record_digest"]:
        raise RuntimeError("Installed creative registration duplicated an identical request.")
    shown = call("creative", "show", registered["record_digest"])
    text_request = {
        **request,
        "provider_format": "LOCAL_TEXT",
        "source": {"registration_kind": "TEXT_ONLY"},
    }
    text_input = directory / "installed-text-registration.json"
    text_input.write_text(json.dumps(text_request))
    text_registered = call("creative", "register", "--input", str(text_input))["data"]
    if (
        text_registered.get("registration_kind") != "TEXT_ONLY"
        or text_registered.get("rendition_id") is not None
        or text_registered.get("fingerprint_id") is not None
        or text_registered.get("remote_mutations") is not False
        or text_registered.get("execution_authority") is not False
    ):
        raise RuntimeError("Installed text registration fabricated media or authority.")
    text_repeated = call("creative", "register", "--input", str(text_input))["data"]
    if text_repeated["record_digest"] != text_registered["record_digest"]:
        raise RuntimeError("Installed text registration duplicated an identical request.")
    text_shown = call("creative", "show", text_registered["record_digest"])
    catalog = call("creative", "catalog")
    if any(
        item["record_digest"] not in json.dumps(catalog) for item in (registered, text_registered)
    ):
        raise RuntimeError("Installed creative catalog lost the registered version.")
    visible = json.dumps(
        [registered, repeated, shown, text_registered, text_repeated, text_shown, catalog]
    )
    if any(
        value in visible
        for value in (
            request["creative_family_id"],
            request["family_label"],
            "PRIVATE-COPY-CREATIVE-SMOKE",
        )
    ):
        raise RuntimeError("Installed creative review exposed local marketing text.")


def validation_environment() -> dict[str, str]:
    """Keep caller cache access but reject environment-selected validation behavior."""
    environment = dict(os.environ)
    for name in list(environment):
        if (
            name.startswith(
                ("PYTEST_", "MYPY_", "PYTHON", "RUFF_", "PIP_", "COVERAGE_", "GIT_", "HATCH_")
            )
            or (name.startswith("UV_") and name != "UV_CACHE_DIR")
            or name
            in {
                "MYPYPATH",
                "VIRTUAL_ENV",
                "VIRTUAL_ENV_PROMPT",
                "CONDA_PREFIX",
                "CONDA_DEFAULT_ENV",
            }
        ):
            environment.pop(name)
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONHASHSEED="0",
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        GIT_ATTR_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return environment


def extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")


def run(arguments: list[str], *, cwd: Path, environment: dict[str, str], timeout: int = 300) -> str:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"Validation command failed with exit {result.returncode}: {arguments[0]}\n"
            f"{result.stdout[-16384:]}\n{result.stderr[-16384:]}"
        )
    return result.stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    """Declare every caller-selected validation input, including test bounds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--uv", default=shutil.which("uv"))
    parser.add_argument("--source-date-epoch", type=int)
    parser.add_argument("--pytest-workers", type=int, default=_DEFAULT_PYTEST_WORKERS)
    parser.add_argument("--pytest-timeout", type=int, default=_DEFAULT_PYTEST_TIMEOUT_SECONDS)
    parser.add_argument("--ultra-timeout", type=int, default=_DEFAULT_ULTRA_TIMEOUT_SECONDS)
    return parser


def require_positive_bounds(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject worker counts and deadlines that cannot express a real run."""
    for option, value in (
        ("--pytest-workers", args.pytest_workers),
        ("--pytest-timeout", args.pytest_timeout),
        ("--ultra-timeout", args.ultra_timeout),
    ):
        if value < 1:
            parser.error(f"{option} must be an integer of at least 1.")


def pytest_command(python: str, *, cache_dir: Path, targets: list[str], workers: int) -> list[str]:
    """Build one bounded pytest command; more than one worker distributes by file."""
    command = [
        python,
        "-m",
        "pytest",
        "-q",
        "--override-ini",
        "addopts=",
        "--override-ini",
        f"cache_dir={cache_dir}",
    ]
    if workers > 1:
        # Plugin autoload is disabled in the validation environment, so load
        # xdist explicitly before requesting workers.
        command += ["-p", "xdist", "-n", str(workers), "--dist=loadfile"]
    return [*command, *targets]


def pytest_configuration_record(args: argparse.Namespace) -> dict[str, object]:
    """Record the exact caller-selected test parallelism and deadlines."""
    workers = int(args.pytest_workers)
    return {
        "workers": workers,
        "dist": "loadfile" if workers > 1 else "serial",
        "timeout_seconds": int(args.pytest_timeout),
        "ultra_timeout_seconds": int(args.ultra_timeout),
    }


def parse_pytest_summary(output: str) -> dict[str, int] | None:
    """Read exact counts from a ``pytest -q`` summary line, or record none at all."""
    for line in reversed(output.splitlines()):
        candidate = line.strip().strip("=").strip()
        if _PYTEST_SUMMARY_DURATION.search(candidate) is None:
            continue
        counts = {"passed": 0, "skipped": 0, "failed": 0, "errors": 0}
        matched = False
        for amount, term in _PYTEST_SUMMARY_COUNT.findall(candidate):
            counts["errors" if term.startswith("error") else term] += int(amount)
            matched = True
        if matched:
            return counts
    return None


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    require_positive_bounds(parser, args)
    if args.uv is None:
        parser.error("Install the development toolchain first; uv is required.")
    repo = args.repo.resolve()
    environment = validation_environment()
    commit = run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{args.revision}^{{commit}}"],
        cwd=repo,
        environment=environment,
    )
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise RuntimeError("Expected one exact SHA-1 source commit.")
    epoch = args.source_date_epoch
    if epoch is None:
        epoch = int(
            run(["git", "show", "-s", "--format=%ct", commit], cwd=repo, environment=environment)
        )
    if epoch < 0:
        parser.error("SOURCE_DATE_EPOCH must be nonnegative.")
    if args.output is None:
        output = Path(tempfile.mkdtemp(prefix="adhelm-development-"))
    else:
        output = args.output.absolute()
        output.mkdir(mode=0o700, exist_ok=False)
    snapshot = output / "source"
    archive = output / "source.tar"
    run(
        ["git", "archive", "--format=tar", "--output", str(archive), commit],
        cwd=repo,
        environment=environment,
    )
    extract_archive(archive, snapshot)
    require_unconfigured_bridge_source(snapshot)
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    source_environment = {**environment, "PYTHONPATH": str(snapshot / "src")}
    python = sys.executable
    tool_cache = output / "tool-cache"
    tool_cache.mkdir(mode=0o700)

    def execute(
        arguments: list[str],
        *,
        cwd: Path = snapshot,
        env: dict[str, str] = source_environment,
        timeout: int = 300,
    ) -> str:
        return run(arguments, cwd=cwd, environment=env, timeout=timeout)

    execute(
        [
            python,
            "-c",
            "import adhelm\n"
            "from pathlib import Path\n"
            "package = Path(adhelm.__file__).resolve()\n"
            "if not package.is_relative_to(Path.cwd() / 'src'):\n"
            "    raise SystemExit('source import did not use the archived snapshot')\n",
        ]
    )
    checks = {}
    check_seconds: dict[str, float] = {}
    for name, arguments in (
        (
            "ruff_check",
            [python, "-m", "ruff", "check", "--config", "pyproject.toml", "--no-cache", "."],
        ),
        (
            "ruff_format_check",
            [
                python,
                "-m",
                "ruff",
                "format",
                "--config",
                "pyproject.toml",
                "--no-cache",
                "--check",
                ".",
            ],
        ),
        (
            "strict_mypy",
            [
                python,
                "-m",
                "mypy",
                "--config-file",
                "pyproject.toml",
                "--cache-dir",
                str(tool_cache / "mypy"),
            ],
        ),
        (
            "pytest",
            pytest_command(
                python,
                cache_dir=tool_cache / "pytest",
                targets=["tests"],
                workers=args.pytest_workers,
            ),
        ),
        ("repository_contract_check", [python, "scripts/check_repository.py"]),
    ):
        # The full-suite deadline is caller-selected with --pytest-timeout and
        # recorded in the validation record with the measured elapsed seconds;
        # every other check retains its existing five-minute bound.
        started = time.monotonic()
        checks[name] = execute(
            arguments,
            timeout=args.pytest_timeout if name == "pytest" else _OTHER_CHECK_TIMEOUT_SECONDS,
        )
        check_seconds[name] = round(time.monotonic() - started, 3)
        print(f"{name}: PASSED", flush=True)
    reviews = sorted(
        set(snapshot.glob("tests/*ultra_review.py"))
        | {
            snapshot / "tests/test_adversarial_review.py",
            snapshot / "tests/test_execution_recovery_review.py",
        }
    )
    ultra_started = time.monotonic()
    checks["ultra_review"] = execute(
        pytest_command(
            python,
            cache_dir=tool_cache / "ultra-pytest",
            targets=[str(review) for review in reviews],
            workers=args.pytest_workers,
        ),
        timeout=args.ultra_timeout,
    )
    check_seconds["ultra_review"] = round(time.monotonic() - ultra_started, 3)
    print("ultra_review: PASSED", flush=True)

    license_path = output / "dependency-license-inventory.json"
    license_repeat_path = output / "dependency-license-inventory-repeat.json"
    for destination in (license_path, license_repeat_path):
        execute(
            [
                python,
                "scripts/audit_dependency_licenses.py",
                "--lock",
                str(snapshot / "uv.lock"),
                "--scope",
                "all",
                "--output",
                str(destination),
            ]
        )
    license_inventory = json.loads(license_path.read_text())
    if (
        license_path.read_bytes() != license_repeat_path.read_bytes()
        or license_inventory["lock"]["sha256"] != sha256(snapshot / "uv.lock")
        or license_inventory["decision"] != "REVIEW_REQUIRED_NO_LEGAL_COMPATIBILITY_DECISION"
    ):
        raise RuntimeError("License evidence lost deterministic lock binding or review limits.")
    print("dependency_license_inventory: PASSED (review required)", flush=True)

    builds: list[dict[str, str]] = []
    for label in ("one", "two"):
        build_source = output / f"build-source-{label}"
        extract_archive(archive, build_source)
        destination = output / f"build-{label}"
        execute(
            [
                args.uv,
                "--no-config",
                "build",
                "--offline",
                "--python",
                python,
                "--out-dir",
                str(destination),
            ],
            cwd=build_source,
            env=environment,
        )
        builds.append(
            {
                p.name: sha256(p)
                for p in sorted(destination.iterdir())
                if p.name.endswith((".whl", ".tar.gz"))
            }
        )
    if len(builds[0]) != 2 or builds[0] != builds[1]:
        raise RuntimeError(
            "Both development wheel and source archive must reproduce byte-for-byte."
        )
    print("development_build_reproducibility: PASSED", flush=True)
    wheels = list((output / "build-one").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Development build did not produce exactly one wheel.")
    wheel = wheels[0]
    require_unconfigured_bridge_wheel(wheel)
    installed = output / "installed"
    execute(
        [
            args.uv,
            "--no-config",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            python,
            "--target",
            str(installed),
            str(wheel),
        ],
        cwd=output,
        env=environment,
    )
    # These smokes model the local installed CLI. Explicit CI refusals have
    # separate fake coverage; an inherited runner flag must not change this
    # missing-profile scenario into a different precondition.
    installed_environment = {**environment, "PYTHONPATH": str(installed), "CI": "false"}
    execute(
        [
            python,
            "-c",
            "import adhelm\n"
            "import os\n"
            "from pathlib import Path\n"
            "package = Path(adhelm.__file__).resolve().parent\n"
            "target = Path(os.environ['PYTHONPATH']).resolve()\n"
            "if not package.is_relative_to(target):\n"
            "    raise SystemExit('installed import did not use the wheel target')\n"
            "if not (package / 'py.typed').is_file():\n"
            "    raise SystemExit('installed wheel is missing py.typed')\n"
            "if len(list((package / 'bundled_skills').glob('*/SKILL.md'))) != 2:\n"
            "    raise SystemExit('installed wheel is missing bundled skills')\n",
        ],
        cwd=output,
        env=installed_environment,
    )

    def installed_call(
        *arguments: str, expected_exit: int = 0, timeout: int = 300
    ) -> dict[str, Any]:
        value = json.loads(
            execute(
                [
                    python,
                    "-c",
                    "import sys\n"
                    "from pathlib import Path\n"
                    "from unittest.mock import patch\n"
                    "from adhelm import credential_lock\n"
                    "from adhelm.cli import main\n"
                    "expected_exit = 0\n"
                    "if len(sys.argv) > 2 and sys.argv[1] == '--validation-expected-exit':\n"
                    "    expected_exit = int(sys.argv[2])\n"
                    "    del sys.argv[1:3]\n"
                    "sys.argv = ['adhelm', *sys.argv[1:]]\n"
                    "blocked = AssertionError('external access forbidden')\n"
                    "lock_root = Path(sys.argv[2]) / 'credential-locks'\n"
                    "with (\n"
                    "    patch('socket.socket.connect', side_effect=blocked),\n"
                    "    patch('socket.socket.connect_ex', side_effect=blocked),\n"
                    "    patch('socket.socket.bind', side_effect=blocked),\n"
                    "    patch('socket.getaddrinfo', side_effect=blocked),\n"
                    "    patch('socket.gethostbyname', side_effect=blocked),\n"
                    "    patch('socket.gethostbyname_ex', side_effect=blocked),\n"
                    "    patch('socket.gethostbyaddr', side_effect=blocked),\n"
                    "    patch('socket.getnameinfo', side_effect=blocked),\n"
                    "    patch('adhelm.credentials._backend', side_effect=blocked),\n"
                    "    patch('subprocess.Popen', side_effect=blocked),\n"
                    "    patch.object(credential_lock, '_default_lock_root', "
                    "return_value=lock_root),\n"
                    "):\n"
                    "    try:\n"
                    "        main()\n"
                    "    except SystemExit as stopped:\n"
                    "        if (not isinstance(stopped.code, int) or "
                    "isinstance(stopped.code, bool) or stopped.code != expected_exit):\n"
                    "            raise SystemExit('Unexpected CLI exit status') from None\n"
                    "    else:\n"
                    "        if expected_exit != 0:\n"
                    "            raise SystemExit('Expected a bounded CLI rejection')\n",
                    "--validation-expected-exit",
                    str(expected_exit),
                    "--data-dir",
                    str(output / "smoke-state"),
                    *arguments,
                ],
                cwd=output,
                env=installed_environment,
                timeout=timeout,
            )
        )
        if not isinstance(value, dict):
            raise RuntimeError("Installed CLI did not return a result envelope.")
        return value

    discovery = installed_call("discover")
    input_contract = discovery["data"]["input_files"]
    if (
        input_contract["json_maximum_bytes"] != 1_048_576
        or input_contract["local_image_maximum_bytes"] != 20 * 1024 * 1024
        or input_contract["required_type"] != "REGULAR_FILE"
        or input_contract["pipes_devices_sockets"] != "REJECTED"
        or input_contract["filesystem_latency_guaranteed"] is not False
    ):
        raise RuntimeError("Installed input discovery lost its bounded regular-file contract.")
    input_pipe = output / "installed-input-pipe"
    os.mkfifo(input_pipe, mode=0o600)
    for command in (
        ("measurement", "validate-event"),
        ("creative", "analyze"),
        ("auth", "meta-token", "enroll"),
        ("plan", "meta-campaign-pause", "create"),
    ):
        rejected = installed_call(*command, "--input", str(input_pipe), expected_exit=2, timeout=5)
        if (
            rejected["outcome"] != "FAILED_CLOSED"
            or rejected["diagnostics"][0]["code"] != "INPUT_FILE_INVALID"
            or input_pipe.name in json.dumps(rejected)
        ):
            raise RuntimeError("Installed pipe input did not fail promptly and privately.")
    input_pipe.unlink()
    creative_registration_smoke(installed_call, output)
    provider_audit_smoke(installed_call, output)
    google_oauth_status_smoke(installed_call)
    tiktok_token_status_disclosure_smoke(installed_call)
    meta_pause_planning_smoke(installed_call, output)
    meta_pause_artifact_smoke(installed_call, output)
    google_ads_pause_artifact_smoke(installed_call, output)
    execution_plan_smoke(installed_call, output)
    gtm_dependency_graph_smoke(installed_call)
    generic_execution_refusal_smoke(installed_call)
    google_ads_pause_execution_refusal_smoke(installed_call)
    google_ads_create_execution_refusal_smoke(installed_call, output)
    openai_ads_create_execution_refusal_smoke(installed_call, output)
    google_ads_activate_execution_refusal_smoke(installed_call, output)
    google_ads_maintain_execution_refusal_smoke(installed_call, output)
    meta_ads_create_execution_refusal_smoke(installed_call, output)
    meta_token_status_disclosure_smoke(installed_call)
    reddit_token_status_smoke(installed_call)
    storage = installed_call("storage", "usage", timeout=5)
    if (
        storage["outcome"] != "LOCAL_OBSERVATION"
        or storage["data"]["scope"] != "SELECTED_DATA_DIRECTORY_ONLY"
        or storage["data"]["cleanup_authorized"] is not False
        or storage["data"]["reclaimable_bytes"] is not None
        or set(storage["data"]["categories"])
        != {"changes", "performance", "creative", "assets", "exports", "caches", "other"}
    ):
        raise RuntimeError(
            "Installed storage usage lost its complete local-only observation contract."
        )
    execution_request = output / "installed-openai-execution-refusal.json"
    execution_request.write_text(json.dumps({"schema_version": 1, "plan_record_digest": "0" * 64}))
    rejected_execution = installed_call(
        "execute", "openai-pause", "--input", str(execution_request), expected_exit=3, timeout=5
    )
    if (
        rejected_execution["outcome"] != "FAILED_CLOSED"
        or rejected_execution["diagnostics"][0]["code"] != "WRITES_NOT_RELEASED"
    ):
        raise RuntimeError("Installed execution was not refused by the release policy.")
    meta_execution_request = output / "installed-meta-execution-refusal.json"
    meta_execution_request.write_text(
        json.dumps({"schema_version": 1, "plan_record_digest": "0" * 64})
    )
    rejected_meta_execution = installed_call(
        "execute",
        "meta-campaign-pause",
        "--input",
        str(meta_execution_request),
        expected_exit=3,
        timeout=5,
    )
    if (
        rejected_meta_execution["outcome"] != "FAILED_CLOSED"
        or rejected_meta_execution["diagnostics"][0]["code"] != "WRITES_NOT_RELEASED"
    ):
        raise RuntimeError("Installed Meta execution was not refused by the release policy.")
    meta_draft_schema = installed_call("schema", "meta-token-profile-draft")["data"]["schema"]
    if (
        meta_draft_schema.get("additionalProperties") is not False
        or set(meta_draft_schema.get("required", []))
        != {
            "profile_id",
            "account_ref",
            "client_application_ref",
            "currency",
            "timezone",
            "inspector_credential_kind",
        }
        or set(meta_draft_schema.get("properties", {}))
        != {
            "schema_version",
            "profile_id",
            "account_ref",
            "client_application_ref",
            "currency",
            "timezone",
            "purpose",
            "inspector_credential_kind",
        }
        or meta_draft_schema["properties"]["schema_version"].get("const") != 1
    ):
        raise RuntimeError("Installed Meta enrollment lost its closed nonsecret declaration.")
    meta_draft = output / "installed-meta-enrollment-refusal.json"
    meta_draft.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": "installed-meta-read",
                "account_ref": "12345",
                "client_application_ref": "67890",
                "currency": "USD",
                "timezone": "UTC",
                "purpose": "READ",
                "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
            }
        )
    )
    meta_refusal = installed_call(
        "auth",
        "meta-token",
        "enroll",
        "--input",
        str(meta_draft),
        expected_exit=4,
        timeout=5,
    )
    if (
        meta_refusal["outcome"] != "FAILED_CLOSED"
        or meta_refusal["diagnostics"][0]["code"] != "EXPLICIT_TERMINAL_HANDOFF_REQUIRED"
        or meta_refusal["data"]
    ):
        raise RuntimeError("Installed Meta enrollment did not require explicit terminal handoff.")
    if not {
        "compile openai-ads",
        "compile meta-ads",
        "compile google-ads",
        "compile linkedin-ads",
        "compile reddit-ads",
        "compile tiktok-ads",
        "compile strategy",
        "strategy catalog",
        "strategy show",
        "candidate catalog",
        "guidance show",
        "auth status",
        "auth google-oauth enroll",
        "auth google-oauth reconnect",
        "auth meta-token enroll",
        "auth meta-token rotate",
        "auth meta-token status",
        "auth meta-token inspect",
        "auth meta-token remove-local",
        "storage usage",
        "execute openai-pause",
        "execute ga4-key-event-create",
        "measurement ga4-admin",
        "measurement ga4-admin-access",
        "measurement ga4-admin-edit read",
        "measurement ga4-admin-edit catalog",
        "measurement ga4-admin-edit show",
        "measurement openai-conversions",
        "measurement gtm",
        "measurement catalog",
        "measurement show",
        "measurement compare",
        "measurement key-event-candidate compile",
        "measurement key-event-candidate catalog",
        "measurement key-event-candidate show",
        "conversion register",
        "conversion catalog",
        "conversion show",
        "plan openai-pause",
        "plan ga4-key-event create",
        "plan ga4-key-event catalog",
        "plan ga4-key-event show",
        "plan meta-campaign-pause create",
        "plan meta-campaign-pause catalog",
        "plan meta-campaign-pause show",
        "plan catalog",
        "plan show",
        "artifacts plan",
        "artifacts ga4-key-event-plan",
        "artifacts meta-campaign-pause",
        "artifacts measurement-comparison",
    } <= set(discovery["data"]["commands"]):
        raise RuntimeError("Installed wheel is missing expected development commands.")
    oauth_draft_schema = installed_call("schema", "google-oauth-profile-draft")["data"]["schema"]
    oauth_draft_properties = oauth_draft_schema["properties"]
    if (
        oauth_draft_schema.get("additionalProperties") is not False
        or set(oauth_draft_properties["purpose"].get("enum", [])) != {"READ", "EDIT"}
        or oauth_draft_properties["purpose"].get("default") != "READ"
        or oauth_draft_properties["scopes"].get("maxItems") != 1
        or {"principal_ref", "client_secret", "credential_source", "access_token", "refresh_token"}
        & oauth_draft_properties.keys()
    ):
        raise RuntimeError(
            "Installed Google OAuth draft lost its nonsecret bounded-purpose boundary."
        )
    alternatives = oauth_draft_schema.get("allOf", [{}])[0].get("oneOf", [])
    expected_targets = {
        ("google_ads", "READ", "https://www.googleapis.com/auth/adwords"),
        ("google_ads", "EDIT", "https://www.googleapis.com/auth/adwords"),
        ("ga4", "READ", "https://www.googleapis.com/auth/analytics.readonly"),
        ("ga4_admin", "READ", "https://www.googleapis.com/auth/analytics.readonly"),
        ("ga4_admin", "EDIT", "https://www.googleapis.com/auth/analytics.edit"),
        ("gtm", "READ", "https://www.googleapis.com/auth/tagmanager.readonly"),
    }
    observed_targets = set()
    for alternative in alternatives:
        properties = alternative.get("properties", {})
        scopes = properties.get("scopes", {}).get("const", [])
        purpose = properties.get("purpose", {}).get("const")
        provider = properties.get("provider", {}).get("const")
        required = (
            {"provider", "scopes", "purpose"} if purpose == "EDIT" else {"provider", "scopes"}
        )
        if provider == "google_ads" and purpose == "EDIT":
            required |= {"currency", "timezone"}
            if properties.get("currency", {}).get("type") != "string":
                raise RuntimeError("Installed Google Ads EDIT currency must be non-null.")
        if len(scopes) != 1 or set(alternative.get("required", [])) != required:
            raise RuntimeError("Installed Google OAuth target alternatives are not exact.")
        observed_targets.add((provider, purpose, scopes[0]))
    if len(alternatives) != 6 or observed_targets != expected_targets:
        raise RuntimeError(
            "Installed Google OAuth schema advertises an unsupported provider/purpose."
        )
    schema = installed_call("schema", "openai-campaign-brief")
    if "ad_groups" not in schema["data"]["schema"]["properties"]:
        raise RuntimeError("Installed compiler schema is missing.")
    key_profile_schema = installed_call("schema", "openai-account-key-profile")["data"]["schema"]
    key_properties = key_profile_schema["properties"]
    if (
        key_properties["profile_kind"].get("const") != "OPENAI_ADS_ACCOUNT_KEY"
        or "profile_kind" not in key_profile_schema["required"]
        or key_properties["provider_scope_status"].get("const") != "NOT_DOCUMENTED"
        or {"principal_ref", "client_ref", "scopes", "secondary_credential_ref"}
        & key_properties.keys()
    ):
        raise RuntimeError("Installed OpenAI account-key schema invents OAuth metadata.")
    profile_receipt = installed_call(
        "config",
        "add-profile",
        "--input",
        str(snapshot / "examples/openai-account-key-profile.json"),
    )
    if profile_receipt["data"] != {
        "provider": "openai_ads",
        "purpose": "READ",
        "credential_stored": False,
    }:
        raise RuntimeError("Installed account-key configuration receipt is not key-free.")
    installed_config = json.loads((output / "smoke-state" / "config.json").read_text())
    if (
        len(installed_config["profiles"]) != 1
        or installed_config["profiles"][0]["profile_kind"] != "OPENAI_ADS_ACCOUNT_KEY"
        or {"principal_ref", "client_ref", "scopes"} & installed_config["profiles"][0].keys()
    ):
        raise RuntimeError("Installed account-key profile did not retain its closed shape.")
    for compiler_schema, budget_field, creative_field in (
        ("openai-campaign-brief", "lifetime_budget", "ad_groups"),
        ("meta-campaign-brief", "lifetime_budget", "creative"),
        ("google-ads-search-brief", "total_budget", "creative"),
        ("linkedin-campaign-brief", "group_total_budget", "creative"),
        ("reddit-campaign-brief", "lifetime_budget", "creative"),
        ("tiktok-campaign-brief", "total_budget", "creative"),
    ):
        document = installed_call("schema", compiler_schema)["data"]["schema"]
        properties = document["properties"]
        if creative_field not in properties or properties["schema_version"].get("const") != 1:
            raise RuntimeError("Installed compiler input schema is missing or unversioned.")
        budget_ref = properties[budget_field].get("$ref", "")
        if budget_ref != "#/$defs/Money" or set(document["$defs"]["Money"]["required"]) != {
            "minor_units",
            "currency",
        }:
            raise RuntimeError("Installed compiler budget lacks its typed money contract.")
    strategy_schema = installed_call("schema", "strategy-brief")["data"]
    strategy_root = installed_call("schema", "strategy-brief", "--root-only")["data"]
    strategy_catalog = installed_call("schema", "strategy-brief", "--definitions")["data"]
    strategy_money = installed_call("schema", "strategy-brief", "--definition", "Money")["data"]
    if not (
        strategy_schema["schema_digest"]
        == strategy_root["schema_digest"]
        == strategy_catalog["schema_digest"]
        == strategy_money["schema_digest"]
    ):
        raise RuntimeError("Installed strategy schema fragments lack a common schema digest.")
    variants = strategy_root["schema"]["properties"]["provider_briefs"]["items"]["discriminator"][
        "mapping"
    ]
    if set(variants) != {
        "google_ads",
        "meta_ads",
        "linkedin_ads",
        "tiktok_ads",
        "reddit_ads",
        "openai_ads",
    }:
        raise RuntimeError("Installed strategy schema is missing a native provider brief.")
    if strategy_money["schema"] != strategy_schema["schema"]["$defs"]["Money"]:
        raise RuntimeError("Installed schema fragment does not match its full source.")
    # The CLI calls above prove installed command wiring. Reassemble every
    # strategy fragment inside one installed interpreter rather than issuing a
    # separate command for each definition.
    execute(
        [
            python,
            "-c",
            "from adhelm.canonical import digest\n"
            "from adhelm.schema_views import installed_schema_view\n"
            "from adhelm.strategy import StrategyBrief\n"
            "full = installed_schema_view(StrategyBrief, 'strategy-brief')\n"
            "root_view = installed_schema_view(StrategyBrief, 'strategy-brief', root_only=True)\n"
            "catalog = installed_schema_view(StrategyBrief, 'strategy-brief', definitions=True)\n"
            "rebuilt = dict(root_view['schema'])\n"
            "definitions = {}\n"
            "for name in catalog['definitions']:\n"
            "    fragment = installed_schema_view(\n"
            "        StrategyBrief, 'strategy-brief', definition=name\n"
            "    )\n"
            "    if fragment['schema_digest'] != full['schema_digest']:\n"
            "        raise SystemExit('strategy schema fragment digest mismatch')\n"
            "    definitions[name] = fragment['schema']\n"
            "rebuilt['$defs'] = definitions\n"
            "if rebuilt != full['schema'] or digest(rebuilt) != full['schema_digest']:\n"
            "    raise SystemExit('strategy schema fragments do not reassemble exactly')\n",
        ],
        cwd=output,
        env=installed_environment,
    )
    candidate_schema = installed_call("schema", "candidate-record", "--root-only")["data"]["schema"]
    candidate_variants = candidate_schema["properties"]["compilation"]["discriminator"]["mapping"]
    if set(candidate_variants) != {
        "openai_ads",
        "meta_ads",
        "google_ads",
        "linkedin_ads",
        "reddit_ads",
        "tiktok_ads",
    }:
        raise RuntimeError("Installed candidate history is missing its reviewed provider variants.")
    candidate_catalog = installed_call("candidate", "catalog")
    if candidate_catalog["data"]["candidates"] or candidate_catalog["total_count"] != 0:
        raise RuntimeError("Installed candidate catalog did not inspect isolated empty history.")
    strategy_record = installed_call("schema", "strategy-candidate-record", "--root-only")["data"][
        "schema"
    ]
    strategy_properties = strategy_record["properties"]
    if (
        strategy_record.get("additionalProperties") is not False
        or strategy_properties["child_compilations"].get("maxItems") != 6
        or strategy_properties["remote_side_effects"].get("const") is not False
        or strategy_properties["execution_status"].get("const")
        != "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    ):
        raise RuntimeError("Installed strategy history lost its closed non-executable bounds.")
    retained_strategies = installed_call("strategy", "catalog")
    if retained_strategies["data"]["strategies"] or retained_strategies["total_count"] != 0:
        raise RuntimeError("Installed strategy catalog did not inspect isolated empty history.")
    artifact_schema = installed_call("schema", "pause-plan-artifact-render")["data"]["schema"]
    if artifact_schema.get("additionalProperties") is not False or set(
        artifact_schema["properties"]
    ) != {"schema_version", "formats", "output_directory", "replacement_authorized"}:
        raise RuntimeError("Installed pause-plan artifact request is not a closed local request.")
    if artifact_schema["properties"]["formats"].get("maxItems") != 3:
        raise RuntimeError("Installed pause-plan artifact formats are not bounded.")
    comparison_artifact_request = installed_call(
        "schema", "measurement-comparison-artifact-render"
    )["data"]["schema"]
    if (
        comparison_artifact_request.get("additionalProperties") is not False
        or set(comparison_artifact_request["properties"])
        != {"schema_version", "formats", "output_directory", "replacement_authorized"}
        or comparison_artifact_request["properties"]["formats"].get("maxItems") != 3
    ):
        raise RuntimeError("Installed comparison artifact request lost its closed bounds.")
    comparison_artifact_document = installed_call(
        "schema", "measurement-comparison-artifact-document", "--root-only"
    )["data"]["schema"]
    if (
        comparison_artifact_document.get("additionalProperties") is not False
        or comparison_artifact_document["properties"]["artifact_kind"].get("const")
        != "measurement_comparison_review"
        or set(comparison_artifact_document["required"]) != {"source_evidence", "content"}
    ):
        raise RuntimeError("Installed comparison artifact document lost its evidence binding.")
    key_event_request = installed_call("schema", "measurement-ga4-key-event-candidate-input")[
        "data"
    ]["schema"]
    if key_event_request.get("additionalProperties") is not False or set(
        key_event_request["properties"]
    ) != {
        "schema_version",
        "conversion_contract_record_digest",
        "declared_destination_profile_ref",
        "key_event",
    }:
        raise RuntimeError("Installed key-event candidate input lost its explicit definition.")
    key_event_record = installed_call(
        "schema", "measurement-ga4-key-event-candidate-record", "--root-only"
    )["data"]["schema"]
    if (
        key_event_record.get("additionalProperties") is not False
        or key_event_record["properties"]["event_type"].get("const")
        != "GA4_KEY_EVENT_CANDIDATE_COMPILED"
        or set(key_event_record["required"]) != {"compilation", "candidate_digest"}
    ):
        raise RuntimeError("Installed key-event history lost its distinct candidate kind.")
    key_event_catalog = installed_call("measurement", "key-event-candidate", "catalog")
    if key_event_catalog["data"]["candidates"] or key_event_catalog["total_count"] != 0:
        raise RuntimeError("Installed key-event catalog did not inspect isolated empty history.")
    measurement_schema = installed_call("schema", "measurement-observation")
    if "snapshot_digest" not in measurement_schema["data"]["schema"]["properties"]:
        raise RuntimeError("Installed measurement observation schema is missing.")
    ga4_inventory_schema = installed_call(
        "schema", "measurement-observation", "--definition", "Ga4AdminInventory"
    )["data"]["schema"]
    for marker_name in ("fingerprint_key_marker", "inventory_content_marker"):
        marker_schema = ga4_inventory_schema["properties"][marker_name]
        if (
            marker_schema.get("type") != "string"
            or marker_schema.get("pattern") != "^[a-f0-9]{64}$"
            or "default" in marker_schema
            or "anyOf" in marker_schema
            or marker_name in ga4_inventory_schema["required"]
        ):
            raise RuntimeError(
                "Installed GA4 marker schema lost its optional string-only boundary."
            )
    comparison_request = installed_call("schema", "measurement-comparison-request")["data"][
        "schema"
    ]
    if comparison_request.get("additionalProperties") is not False or set(
        comparison_request["properties"]
    ) != {"schema_version", "before_record_digest", "after_record_digest"}:
        raise RuntimeError("Installed measurement comparison selectors are not a closed request.")
    comparison_result = installed_call("schema", "measurement-comparison", "--root-only")["data"][
        "schema"
    ]
    comparison_properties = comparison_result["properties"]
    continuity_schema = installed_call(
        "schema", "measurement-comparison", "--definition", "FingerprintKeyContinuity"
    )["data"]["schema"]
    if (
        comparison_result.get("additionalProperties") is not False
        or comparison_properties["facts"].get("maxItems") != 1000
        or comparison_properties["execution_authority"].get("const") is not False
        or comparison_properties["exact_change_time"].get("type") != "null"
        or comparison_properties["fingerprint_key_continuity"].get("$ref")
        != "#/$defs/FingerprintKeyContinuity"
        or continuity_schema.get("type") != "string"
        or set(continuity_schema.get("enum", []))
        != {"NOT_EVIDENCED", "NOT_APPLICABLE", "MATCHING_RECORDED_KEY_MARKERS"}
    ):
        raise RuntimeError("Installed measurement comparison lost its bounds or uncertainty.")
    for request_schema in (
        "measurement-ga4-read",
        "measurement-ga4-edit-read",
        "measurement-openai-read",
        "measurement-gtm-read",
    ):
        properties = installed_call("schema", request_schema)["data"]["schema"]["properties"]
        if "profile_id" not in properties or properties["schema_version"].get("const") != 1:
            raise RuntimeError("Installed measurement input schema is missing or unversioned.")
    access_schema = installed_call("schema", "measurement-ga4-access")["data"]["schema"]
    if (
        access_schema.get("additionalProperties") is not False
        or set(access_schema.get("required", []))
        != {"read_profile_id", "edit_profile_id", "stream_id"}
        or access_schema["properties"]["schema_version"].get("const") != 1
    ):
        raise RuntimeError("Installed GA4 access-check schema lost its exact role selectors.")
    gtm_full_refusal = installed_call(
        "measurement",
        "gtm",
        "unconfigured-gtm-read",
        "1234567",
        "--full-inventory",
        expected_exit=3,
        timeout=10,
    )
    if (
        gtm_full_refusal["outcome"] != "FAILED_CLOSED"
        or gtm_full_refusal["diagnostics"][0]["code"] != "NOT_CONFIGURED_OPTIONAL"
        or gtm_full_refusal["data"]
    ):
        raise RuntimeError(
            "Installed complete GTM inventory did not stay optional and fail closed."
        )
    measurement_catalog = installed_call("measurement", "catalog")
    if measurement_catalog["data"]["measurements"] or measurement_catalog["total_count"] != 0:
        raise RuntimeError(
            "Installed measurement catalog did not inspect the isolated empty history."
        )
    edit_catalog = installed_call("measurement", "ga4-admin-edit", "catalog")
    if edit_catalog["data"]["inventories"] or edit_catalog["total_count"] != 0:
        raise RuntimeError("Installed EDIT inventory catalog did not inspect empty history.")
    edit_observation = installed_call("schema", "measurement-ga4-edit-observation")["data"][
        "schema"
    ]
    if (
        edit_observation.get("additionalProperties") is not False
        or edit_observation["properties"]["execution_authority"].get("const") is not False
        or edit_observation["properties"]["remote_mutations"].get("const") is not False
        or edit_observation["properties"]["event_type"].get("const")
        != "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    ):
        raise RuntimeError(
            "Installed EDIT observation schema lost its distinct no-authority boundary."
        )
    pause_schema = installed_call("schema", "openai-pause-request")["data"]["schema"]
    pause_properties = pause_schema["properties"]
    if (
        set(pause_schema["required"]) != {"profile_id", "campaign_id"}
        or pause_properties["expires_in_seconds"].get("maximum") != 900
        or pause_schema.get("additionalProperties") is not False
    ):
        raise RuntimeError("Installed pause input is missing its closed bounds.")
    plan_catalog = installed_call("plan", "catalog")
    if plan_catalog["data"]["plans"] or plan_catalog["total_count"] != 0:
        raise RuntimeError("Installed plan catalog did not inspect isolated empty history.")
    ga4_create_catalog = installed_call("plan", "ga4-key-event", "catalog")
    if ga4_create_catalog["data"]["plans"] or ga4_create_catalog["total_count"] != 0:
        raise RuntimeError("Installed GA4 CREATE plan catalog did not inspect empty history.")
    ga4_create_request = installed_call("schema", "ga4-key-event-create-plan-request")["data"][
        "schema"
    ]
    if (
        ga4_create_request.get("additionalProperties") is not False
        or set(ga4_create_request["required"]) != {"profile_id", "candidate_record_digest", "fresh"}
        or ga4_create_request["properties"]["expires_in_seconds"].get("maximum") != 900
        or ga4_create_request["properties"]["expires_in_seconds"].get("minimum") != 60
    ):
        raise RuntimeError("Installed GA4 CREATE request lost its source or expiry boundaries.")
    ga4_create_record = installed_call("schema", "ga4-key-event-create-plan-record")["data"][
        "schema"
    ]
    if (
        ga4_create_record.get("additionalProperties") is not False
        or ga4_create_record["properties"]["execution_authority"].get("const") is not False
        or ga4_create_record["properties"]["requires_current_kernel_checks"].get("const")
        is not True
        or ga4_create_record["properties"]["event_type"].get("const") != "GA4_KEY_EVENT_CREATE_PLAN"
    ):
        raise RuntimeError("Installed GA4 CREATE record lost its distinct no-authority boundary.")
    ga4_artifact_schema = installed_call("schema", "ga4-key-event-create-plan-artifact-render")[
        "data"
    ]["schema"]
    ga4_artifact_properties = ga4_artifact_schema["properties"]
    if (
        ga4_artifact_schema.get("additionalProperties") is not False
        or set(ga4_artifact_properties)
        != {"schema_version", "formats", "output_directory", "replacement_authorized"}
        or ga4_artifact_properties["schema_version"].get("const") != 1
        or ga4_artifact_properties["formats"].get("maxItems") != 3
        or ga4_artifact_properties["formats"]["items"].get("enum") != ["json", "csv", "html"]
    ):
        raise RuntimeError("Installed GA4 artifact request lost its closed local-output boundary.")
    ga4_artifact_document = installed_call(
        "schema", "ga4-key-event-create-plan-artifact-document", "--root-only"
    )["data"]["schema"]
    if (
        ga4_artifact_document.get("additionalProperties") is not False
        or ga4_artifact_document["properties"]["artifact_kind"].get("const")
        != "ga4_key_event_create_plan_review"
        or set(ga4_artifact_document["required"]) != {"source_evidence", "content"}
    ):
        raise RuntimeError("Installed GA4 artifact document lost its historical source boundary.")
    for contract_schema in ("conversion-definition", "conversion-contract"):
        properties = installed_call("schema", contract_schema)["data"]["schema"]["properties"]
        if properties["schema_version"].get("const") != 1:
            raise RuntimeError("Installed conversion schema is missing its version.")
    conversion_catalog = installed_call("conversion", "catalog")
    if conversion_catalog["data"]["contracts"] or conversion_catalog["total_count"] != 0:
        raise RuntimeError("Installed conversion catalog did not inspect isolated empty history.")
    guidance = installed_call("guidance", "catalog")
    for skill in guidance["data"]["guidance"]:
        for section in skill["sections"]:
            document = installed_call("guidance", "show", skill["name"], "--section", section)
            if not document["data"]["text"] or document["data"]["execution_authority"]:
                raise RuntimeError("Installed guidance is absent or claims authority.")
    print("installed_wheel_smoke: PASSED", flush=True)

    # uv's export describes the runtime lock graph across applicable platforms,
    # not the native libraries of a future standalone executable or OCI image.
    sbom_path = output / "runtime-lock-sbom.cdx.json"
    execute(
        [
            args.uv,
            "--no-config",
            "export",
            "--locked",
            "--offline",
            "--no-dev",
            "--format",
            "cyclonedx1.5",
            "--output-file",
            str(sbom_path),
            "--quiet",
        ],
        env=environment,
    )
    sbom = json.loads(sbom_path.read_text())
    lock_digest = sha256(snapshot / "uv.lock")
    metadata = sbom.get("metadata")
    components = sbom.get("components")
    if not isinstance(metadata, dict) or not isinstance(components, list):
        raise RuntimeError("uv did not produce the expected CycloneDX runtime-lock graph.")
    sbom["serialNumber"] = "urn:uuid:" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"adhelm:{commit}:{lock_digest}:{epoch}")
    )
    metadata["timestamp"] = datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")
    existing_properties = metadata.get("properties", [])
    if not isinstance(existing_properties, list):
        raise RuntimeError("CycloneDX metadata properties are invalid.")
    metadata["properties"] = [
        *existing_properties,
        {"name": "adhelm:source_commit", "value": commit},
        {"name": "adhelm:uv_lock_sha256", "value": lock_digest},
        {"name": "adhelm:source_date_epoch", "value": str(epoch)},
        {"name": "adhelm:scope", "value": "RUNTIME_LOCK_GRAPH_NOT_RELEASE_ARTIFACT_SBOM"},
    ]
    sbom_path.write_text(json.dumps(sbom, sort_keys=True, separators=(",", ":")) + "\n")
    result = {
        "source_commit": commit,
        "validated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "workspace": str(output),
        "source_date_epoch": epoch,
        "checks": checks,
        "pytest_configuration": pytest_configuration_record(args),
        "check_seconds": check_seconds,
        "pytest_summary": parse_pytest_summary(checks["pytest"]),
        "artifacts": builds[0],
        "installed_wheel_discovery_smoke": "PASSED",
        "installed_regular_input_smoke": "PASSED",
        "installed_creative_registration_smoke": "PASSED",
        "installed_storage_usage_smoke": "PASSED",
        "installed_openai_execution_refusal_smoke": "PASSED",
        "installed_meta_execution_refusal_smoke": "PASSED",
        "installed_meta_token_schema_smoke": "PASSED",
        "installed_meta_token_noninteractive_refusal_smoke": "PASSED",
        "installed_meta_pause_planning_smoke": "PASSED",
        "installed_meta_pause_plan_artifact_smoke": "PASSED",
        "installed_google_ads_pause_plan_artifact_smoke": "PASSED",
        "installed_execution_plan_refusal_smoke": "PASSED",
        "installed_generic_execution_refusal_smoke": "PASSED",
        "installed_google_ads_pause_execution_refusal_smoke": "PASSED",
        "installed_google_ads_create_execution_refusal_smoke": "PASSED",
        "installed_openai_ads_create_execution_refusal_smoke": "PASSED",
        "installed_google_ads_activate_execution_refusal_smoke": "PASSED",
        "installed_google_ads_maintain_execution_refusal_smoke": "PASSED",
        "installed_meta_ads_create_execution_refusal_smoke": "PASSED",
        "installed_meta_token_status_disclosure_smoke": "PASSED",
        "installed_reddit_token_status_smoke": "PASSED",
        "unconfigured_bridge_source_sha256": _DEFAULT_BRIDGE_SOURCE_SHA256,
        "unconfigured_meta_bridge_source_sha256": _DEFAULT_META_BRIDGE_SOURCE_SHA256,
        "installed_guidance_smoke": "PASSED",
        "installed_measurement_smoke": "PASSED",
        "installed_ga4_edit_inventory_smoke": "PASSED",
        "installed_ga4_create_plan_smoke": "PASSED",
        "installed_ga4_create_plan_artifact_smoke": "PASSED",
        "installed_conversion_smoke": "PASSED",
        "installed_compiler_schema_smoke": "PASSED",
        "installed_candidate_history_smoke": "PASSED",
        "installed_plan_artifact_discovery_smoke": "PASSED",
        "installed_strategy_schema_smoke": "PASSED",
        "installed_strategy_history_smoke": "PASSED",
        "installed_account_key_profile_smoke": "PASSED",
        "installed_pause_discovery_smoke": "PASSED",
        "runtime_lock_sbom": {
            "sha256": sha256(sbom_path),
            "component_count": len(components),
            "scope": "RUNTIME_LOCK_GRAPH_NOT_RELEASE_ARTIFACT_SBOM",
            "license_or_vulnerability_audit": False,
        },
        "dependency_license_inventory": {
            "sha256": sha256(license_path),
            "scope": "LOCK_GRAPH_AND_VERSION_MATCHED_INSTALLED_METADATA",
            "summary": license_inventory["summary"],
            "decision": license_inventory["decision"],
            "reproducible": True,
            "release_artifact_license_scan": False,
        },
        "release_state": "IMPLEMENTING",
        "signed_or_published": False,
    }
    (output / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
