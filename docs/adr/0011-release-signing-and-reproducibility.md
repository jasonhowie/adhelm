# ADR 0011: Release signing and reproducibility

- Status: Pending
- Date: 2026-09-04

## Context

The release contract requires reproducible signed macOS, Windows, Linux, and
OCI artifacts, checksums, SBOM, provenance, and a signed Git tag. The owning
legal identity, certificate subjects, CI/OIDC trust, hardware-backed key paths,
timestamp authorities, and notarization account are not yet selected.

## Proposed decision

Reproduce unsigned payloads bit for bit from the exact commit and pinned build
inputs. Generate checksums, SPDX or CycloneDX SBOM, dependency/license results,
and SLSA-compatible provenance before signing. Bind:

- macOS Developer ID and notarization to the reproducible payload;
- Windows Authenticode and timestamp to the reproducible payload;
- OCI signature to the immutable image digest;
- release manifest/checksums to the selected release identity; and
- the signed `v1.0.0` tag to the exact passing commit.

Timestamped signatures and notarization wrappers are authenticated bindings,
not falsely claimed byte-identical outputs. Private keys and tokens remain
outside source, chat, logs, fixtures, evidence, and Git.

## Resolution gate

Accept this ADR only after owners select public identities, protected signing
flows, timestamp/notarization endpoints, rotation/revocation procedures, and
offline verification commands. Until then release signing is blocked, while
unsigned reproducibility work may proceed.
