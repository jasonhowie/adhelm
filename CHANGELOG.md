# Changelog

All notable changes to AdHelm are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - Unreleased

### Removed

- The Google Ads developer token. Google retired developer tokens on 2026-09-09:
  API access now belongs to the Google Cloud project that owns the OAuth client,
  at Test, Explorer, Basic or Standard, so a new user can no longer obtain the
  token 0.1.0 asked for. AdHelm no longer asks for, stores, reads or sends one:
  the `developer-token` header is gone from every request, enrollment no longer
  prompts for a second credential, `auth store`, `rotate` and `remove-local` lose
  their `--slot` option (there is only the profile's own credential), and
  `auth google-oauth status` loses the `developer_token_slot_*` fields and its
  `INCOMPLETE_COVERAGE` outcome.

### Changed

- The access level AdHelm reports is the Cloud project's, not a token's:
  `auth google-oauth status` returns `cloud_project_access_level`, always
  `NOT_OBSERVABLE` because no reviewed read returns it, and the Google Ads audit
  finding `GA_POLICY_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED` is now
  `GA_POLICY_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE`.
- A Google Ads read that Google refuses with `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION`
  is reported as `GOOGLE_ADS_CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION`, with a next
  action to apply for Explorer access for the project on the Google Ads API Overview
  page (AdHelm recommends Basic, which is applied for from Explorer after brand
  verification). `AUTHORIZATION_ERROR` is reported as `GOOGLE_ADS_AUTHORIZATION_ERROR`
  and cites Google's documented known issue for projects upgraded after 2026-09-09.
  For a `401` or `403` only, the shared transport now scans a body of at most 64 KiB
  for one of those two codes. Screened diagnostic codes and request IDs are extracted
  only from bodies within 16 KiB; provider prose is never retained.
- The Google Ads setup guide follows the Cloud Console flow.

### Fixed

- Google Ads reads work for a user who has no developer token.
- Google Ads v25 inventory uses the current campaign date-time fields and
  preserves account-local time precision. A pinned official field inventory
  checks field names across read and unreleased planning paths.
- Google Ads reports request conversions, conversion value, all conversions and
  all-conversion value. Exact fractional counts and source values survive local
  history and deterministic exports; attribution remains explicitly unknown.
- Opt-in campaign names make Google Ads report rows recognizable without
  reversing opaque references. Default exports continue to pseudonymize names.
- Interactive Google, LinkedIn and Reddit enrollment prints a local browser URL,
  bounded callback wait and cancellation instructions to stderr. `--no-browser`
  supports manual navigation on the same machine. JSON stdout stays unchanged.
- Provider HTTP errors retain bounded codes and request identifiers where the
  provider exposes a reviewed shape. Raw error prose and payloads remain private.
- Built wheel and sdist checks reject inconsistent package versions and a stale
  release state. Google OAuth guidance describes Cloud-project API access after
  the developer-token sunset.

Remote writes remain unreleased, all providers remain `EXPERIMENTAL`, and no new
live-validation or named-agent compatibility claim is made.

### Upgrading from 0.1.0

0.1.0 users: re-enroll Google profiles (`adhelm auth google-oauth reconnect --profile
<id> --client-config <file> --interactive`, as the same Google user); an old
developer-token entry in the OS credential store is inert and can be deleted. The
retired `secondary_credential_ref` in `config.json` is dropped on load. Until a profile
is reconnected, its reads stop with `GOOGLE_OAUTH_PROFILE_BINDING_STALE`.

## [0.1.0] - Unreleased

The first public release. It is read-only: it makes no change to any advertising
or analytics account. It is gated by the
[0.1.0 release definition](docs/release-0.1.0.md), is not `v1.0.0`, and makes no
`COMPLETE`, live-parity or compliance claim.

### Added

- A local, free, Apache-2.0 command-line tool, `adhelm`, driven by the user's own
  AI agent. `adhelm discover` lists the installed commands, versioned schemas,
  exit codes, release policy and provider labels without network or
  credential-store access, and results are bounded and structured.
- Read-only support for Google Ads, Meta Ads, LinkedIn Ads, TikTok Ads, Reddit
  Ads, OpenAI Ads, GA4 and GTM. For each provider: local enrollment with the
  user's own application or key, status, refresh where the provider supports it,
  local removal and revocation guidance; account identity; a fixed inventory; a
  fixed daily aggregate report; audit rules with per-dimension coverage; and
  recommendations that change nothing.
- GA4 Data API aggregate reporting and read-only Admin inventory, and GTM fixed
  inventory, dependency graph and read-only audit.
- An append-only JSONL history with `history verify`, reconciliation of
  out-of-band changes, deterministic CSV and JSON projections, and a rebuildable
  SQLite index.
- Local creative registration with content addressing, and provider creative
  capture where the provider read path already exposes it.
- Two bundled agent skills for the diagnose, recommend and verify loop, served by
  `adhelm guidance catalog` and `adhelm guidance show`. Compact output is the
  default, and optional documents, spreadsheets and HTML reports are produced on
  request.
- Provider release labels, `LIVE_PILOTED` and `EXPERIMENTAL`, shown in
  `adhelm discover`, `adhelm doctor` and every output that names a provider. A
  provider is `LIVE_PILOTED` only when a redacted live read pilot record for this
  exact version is committed under `docs/pilots/`. The label each provider
  carries in this release is listed under
  [Provider labels in this release](#provider-labels-in-this-release).
- `adhelm pilot run`, which the owner of an account uses to record a live read
  pilot. It runs read commands only.
- Support for macOS, Linux and Windows on CPython 3.12.
- Distribution as a PyPI wheel and sdist named `adhelm`, and as a GitHub
  release, installable with `uv tool install adhelm` or `pipx install adhelm`.
- One release-policy gate refuses every remote-mutation command with
  `WRITES_NOT_RELEASED` (exit code `3`) before credential-store access, ledger
  `APPLY_STARTED` or network egress. No configuration value, environment
  variable, flag or edited file enables writes. Offline compile, plan and preview
  commands remain available and state that execution is unreleased.
- The default runtime has no vendor proxy, hosted database, telemetry, crash
  upload, automatic updater or license heartbeat. Interactive secrets are stored
  in the operating system credential store.

### Provider labels in this release

`EXPERIMENTAL` means the provider is fixture-verified only and has no live
evidence. `LIVE_PILOTED` means it is also read-piloted on a live account, with a
redacted pilot record committed under `docs/pilots/`. A label states evidence and
never a capability. The label of each provider in this release:

- Google Ads: `EXPERIMENTAL`
- Meta Ads: `EXPERIMENTAL`
- LinkedIn Ads: `EXPERIMENTAL`
- TikTok Ads: `EXPERIMENTAL`
- Reddit Ads: `EXPERIMENTAL`
- OpenAI Ads: `EXPERIMENTAL`
- GA4: `EXPERIMENTAL`
- GTM: `EXPERIMENTAL`

### Known limitations

- No remote writes; write commands are visible but unreleased.
- No CRM attribution.
- Providers labelled `EXPERIMENTAL` have no live evidence.
- Users must obtain provider API access themselves; quotas and access tiers are
  the provider's.
- Some reads are bounded rather than exhaustive where a provider documents no
  pagination, and audits mark that coverage explicitly. The
  [capability matrix](docs/capability-matrix.md) lists each provider's limits.
- Money is read in a few currencies only. Google Ads, LinkedIn Ads, Reddit Ads
  and OpenAI Ads project money exactly only for USD accounts. Meta Ads accepts
  AUD, CAD, CHF, DKK, EUR, GBP, HKD, JPY, MXN, NOK, NZD, SEK, SGD and USD, and
  TikTok Ads accepts AUD, CAD, EUR, GBP, JPY and USD. A read that carries money
  for an account in any other currency is refused with `UNSUPPORTED_CURRENCY`.
