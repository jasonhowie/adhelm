# ADR 0013: Free open-source release as AdHelm

- Status: Accepted
- Date: 2026-10-10
- Decided by: product owner, recorded from a decision session on 2026-10-10

## Context

The product brief, ADR 0009 and the edition manifest describe a signed free
Account Auditor and a paid Operator. `distribution-model.md` explored an
open-core alternative. None of it shipped. Provider approvals (Meta app review,
Google Basic developer-token access, TikTok app approval, LinkedIn Standard
tier) and signing identities were the longest-lead blockers, and every one of
them exists only because the vendor would register and operate provider
applications on the customer's behalf.

The command and import name `adcp` collides with the official Python client of
the Ad Context Protocol (`adcp` on PyPI), a standard in the same field.

## Decision

1. **Fully free open source.** There is no paid tier, no paid build, no
   vendor-registered provider application, no vendor token-exchange relay, no
   AI-operated paid support, and no pricing. Users register their own provider
   applications and bring their own credentials. ADR 0009 and
   `edition-manifest.md` are superseded.
   `distribution-model.md` and the product brief's
   distribution and pricing sections are historical.
2. **License: Apache-2.0.** The repository carries `LICENSE` (Apache-2.0 text),
   `NOTICE`, and `license = "Apache-2.0"` metadata. Contributions use the
   Developer Certificate of Origin (`Signed-off-by:`), not a CLA. The AdHelm
   name and marks are not licensed by Apache-2.0; forks that redistribute must
   not imply endorsement.
3. **Name: AdHelm.** Distribution, import package and command are `adhelm`.
   Every name-bearing identifier changes in one rename ticket; see "Rename
   rules" below.
4. **Home and distribution.** Public repository `github.com/jasonhowie/adhelm`.
   Distribution is PyPI (`uv tool install adhelm` / `pipx install adhelm`)
   published through PyPI trusted publishing from GitHub Actions, plus GitHub
   releases. The first public version is `0.1.0`.
5. **History.** The public repository starts from one squashed commit of a
   scrubbed, allowlisted tree. This private repository keeps its full history
   and the documents omitted from the public tree.
6. **First release scope: read-only.** `0.1.0` covers auth lifecycle, inventory,
   aggregate reporting, audits, recommendations, local history, reconciliation,
   exports, optional artifacts and agent guidance for Google Ads, Meta Ads,
   LinkedIn Ads, TikTok Ads, Reddit Ads, OpenAI Ads, GA4 and GTM, on macOS,
   Linux and Windows. Salesforce and HubSpot are out of `0.1.0`. The release
   gate is [`release-0.1.0.md`](../release-0.1.0.md).
7. **Write code ships, refused at runtime.** Existing and in-flight mutation
   code (pause, create, activate/maintain, GA4 Admin create, the native approval
   host sources) is published. A `0.1.0` build refuses every remote mutation
   before credential access through one release-policy gate. Offline compile,
   plan and preview commands stay available because they cause no remote effect.
8. **Future writes require a self-signed approval host.** When writes are
   released, a user who wants them builds and signs the native approval host
   with their own platform signing identity (Apple Developer ID on macOS). The
   threat model of ADR 0005/0006 is not weakened by an unsigned path. Windows
   and Linux write authority remains A03 design work.
9. **Editions removed.** `src/adcp/edition.py`, the `edition` configuration
   field and every Auditor/Operator branch are deleted, not left dormant. The
   release-policy gate replaces them as the single place that refuses writes.
10. **Live evidence before release.** Each provider is labelled `LIVE_PILOTED`
    or `EXPERIMENTAL` at release. `LIVE_PILOTED` needs a redacted live read
    pilot on an account the owner administers. A provider without one ships as
    `EXPERIMENTAL` in `adhelm discover`, `adhelm doctor` and the README; it is not
    hidden and does not block the release.
11. **In-flight branches.** `claude/w3-p01-tt` (TikTok auth/read/audit),
    `claude/w3-p05-ga` (Google activate/maintain) and `claude/w3-p04-ma` (Meta
    create) are finished, reviewed and merged before the public export. The
    two write branches merge under the runtime refusal.
12. **Long-term contract kept.** [`definition-of-done.md`](../definition-of-done.md)
    remains the `v1.0.0` target with its §8 commercial bullets superseded by this
    ADR. `0.1.0` is not `v1.0.0` and makes no completion claim.

## Rename rules

There is no installed base other than the owner's development profile, so the
rename does not carry a migration. The owner re-enrolls credentials after it.

| Surface | From | To |
| --- | --- | --- |
| Distribution, import package, command | `local-ad-control-plane`, `adcp`, `adcp` | `adhelm`, `adhelm`, `adhelm` |
| Error type | `AdcpError` | `AdhelmError` |
| Environment variables | `ADCP_*` | `ADHELM_*` |
| OS credential-store service names | `local-ad-control-plane[.<kind>-v1]` | `adhelm[.<kind>-v1]` |
| Default data directory | `~/.local/share/adcp` on every platform | Platform-native: `~/.local/share/adhelm` (XDG), `~/Library/Application Support/adhelm`, `%LOCALAPPDATA%\adhelm` |
| Schema `$id` URIs | `https://local-ad-control-plane.dev/schema/...` | `https://aptsonic.com/adhelm/schema/...` |
| Domain-separation tags and record namespaces | `adcp/...`, `adcp.<kind>.v1`, `adcp-local-*`, `adcp_kernel_egress_permit` | `adhelm/...`, `adhelm.<kind>.v1`, `adhelm-local-*`, `adhelm_kernel_egress_permit` |
| Native bundle and service identifiers | `com.example.adcp.*`, `adcp-macos-approval-v1` | `com.aptsonic.adhelm.*`, `adhelm-macos-approval-v1` |

Renaming domain-separation tags changes canonical digests and golden vectors.
That is accepted: regenerate them in the rename ticket, in one commit, with a
test proving no `adcp` identifier remains outside historical documents.

### Domain and identifier root (2026-10-12)

The owner decided on 2026-10-12 that AdHelm will not use the `adhelm.dev` domain.
Its home is the owner's portfolio site, `https://aptsonic.com/adhelm`. The two
rows above therefore use the `https://aptsonic.com/adhelm/schema/...` form for
schema `$id` URIs and the `com.aptsonic.adhelm.*` reverse-DNS root for bundle,
service, access-group, provenance and enrollment identifiers, replacing the
`https://adhelm.dev/schema/...` and `dev.adhelm.*` forms that the rename first
recorded. Nothing had been published under the earlier forms. The digests and
golden vectors that embed these strings were regenerated in the same change, and
`scripts/check_repository.py` fails any `adhelm.dev` or `dev.adhelm` outside this
note.

## Consequences

- Provider approvals, the token relay, vendor app registration and paid
  distribution leave the critical path. Users carry their own provider
  onboarding, so per-provider setup documentation becomes release work.
- ADR 0011 narrows for `0.1.0`: PyPI wheels and sdists with trusted publishing,
  build provenance attestations, checksums, an SBOM and dependency/license scans.
  Signed standalone binaries, the OCI image and Authenticode are deferred
  to the first write release. The macOS approval host is not distributed in
  `0.1.0`.
- AGENTS.md rules 3, 15 and 19–29 are unchanged. The local-execution, no
  telemetry, no meter and redaction invariants apply to every build.
- `backlog/phase-2-vendor-hosted-mcp.md` has no business model and is omitted
  from the public tree; the customer-hosted track stays.
- HubSpot owner action #3 closes as deferred with CRM; F03 research stays in the
  private repository.

## Dated note (2026-10-12)

The context above names "Google Basic developer-token access" as one of the
longest-lead provider approvals. Google retired Google Ads developer tokens on
2026-09-09: API access now belongs to the Google Cloud project that owns the
OAuth client, at Test, Explorer, Basic or Standard, and users apply on the Google
Ads API Overview page in Cloud Console (source: the Developer token page,
fetched 2026-10-12, page last updated 2026-10-07). The decision is unchanged.
Every user still carries their own provider onboarding; for Google Ads that is
Explorer or higher access (AdHelm recommends Basic) for their own Cloud project,
not a developer token. AdHelm no
longer stores or sends a developer token. See the
[Google Ads setup guide](../setup/google_ads.md).
