# AdHelm 0.1.0 release definition

Last updated: 2026-10-10

This is the release gate for the first public, read-only AdHelm release. It is
decided by [ADR 0013](adr/0013-open-source-release.md). It does not replace
[`definition-of-done.md`](definition-of-done.md), which remains the `v1.0.0`
target. Nothing in this document permits claiming `COMPLETE`, `v1.0.0`, live
parity, write support or a compliance property.

## Release claim

> AdHelm 0.1.0 lets a marketer direct their own AI agent through a local,
> free, Apache-2.0 CLI to connect their own ad and analytics accounts, inventory
> them, report aggregate performance, audit them, and receive evidence-backed
> recommendations, with append-only local history and redacted exports. It
> makes no changes to any advertising or analytics account.

## In scope

| Area | Required for 0.1.0 |
| --- | --- |
| Providers | Google Ads, Meta Ads, LinkedIn Ads, TikTok Ads, Reddit Ads, OpenAI Ads, GA4, GTM |
| Per provider | Local auth enrollment with the user's own app or key, status, refresh where the provider supports it, local removal and revocation guidance; account identity; fixed inventory; fixed daily aggregate report; audit rules with per-dimension coverage; non-writing recommendations |
| Measurement | GA4 Data API aggregate reporting and read-only Admin inventory; GTM fixed inventory, dependency graph and read-only audit |
| History | Append-only JSONL ledger, `history verify`, reconciliation of out-of-band changes, deterministic CSV/JSON projections, SQLite index |
| Creative | Local registration and content addressing already shipped; provider creative capture where the provider read path already exposes it |
| Agent interface | `adhelm discover`, versioned schemas, bounded structured results, exit codes, both bundled skills, compact output with on-request artifacts |
| Platforms | macOS, Linux, Windows on CPython 3.12 |
| Distribution | PyPI wheel and sdist named `adhelm`, GitHub release, `uv tool install` and `pipx install` |

## Out of scope

Every remote mutation, including pause, create, activate, maintain, GA4 Admin
edits, GTM staging/version/publish, browser measurement traffic and recovery
that mutates a provider. Salesforce and HubSpot. Signed standalone binaries,
the OCI image, the native approval host as a distributed artifact, and the
30-day soak. Apple advertising, crawling, CRM discovery and every other
AGENTS.md exclusion stay excluded.

## Required behavior

### Writes refused

- One release-policy gate refuses every remote-mutation command before
  credential-store access, ledger `APPLY_STARTED`, or network egress, with a
  stable error code `WRITES_NOT_RELEASED`, exit code `3`, and a next action
  that names the release policy.
- `adhelm discover` lists write commands with status `UNRELEASED`. Offline
  compile, plan and preview commands remain `AVAILABLE` and say that execution
  is unreleased.
- Tests prove the gate fires for every registered execution leaf, including
  the merged Google activate/maintain and Meta create paths, with a fake
  credential store that fails the test if read.
- No configuration value, environment variable, flag or edited file enables
  writes in a `0.1.0` build.

### Provider status labels

Each provider carries exactly one release label, surfaced in `adhelm discover`,
`adhelm doctor`, and the README provider table:

| Label | Meaning | Evidence |
| --- | --- | --- |
| `LIVE_PILOTED` | Fixture-verified and read-piloted on a live account | Redacted pilot record per [Live read pilot](#live-read-pilot) |
| `EXPERIMENTAL` | Fixture-verified only | Fixture tests; the label and its meaning appear in every output that names the provider |

A provider missing required fixture-verified read, report or audit coverage
cannot ship under either label; it blocks the release.

The label is a statement about evidence and never a capability. It appears, with
its meaning, in every output that names the provider, including refusals and the
`compile`, `plan`, `execute` and `recover` commands. The single exception is a
closed success receipt sized byte for byte to the 1 KiB minimum output bound, which
has no room for it: the unreleased execution result and the GA4 key-event create
plan receipt. They are listed in `BOUND_SIZED_RECEIPT_OUTCOMES`
(`src/adhelm/release_labels.py`), a test pins the list, and the same commands'
refusals and catalogs are labelled. Adding to the list changes this gate.

### Live read pilot

A pilot runs on an account the owner administers (accounts of the owner's own
business projects are acceptable for `0.1.0`; they remain excluded from `v1.0.0`
evidence). It records, locally and redacted:

1. Enrollment through the documented bring-your-own-app flow.
2. `doctor` and account identity.
3. Inventory and a seven-day aggregate report.
4. An audit run with its coverage table.
5. `history verify` passing on the resulting ledger.
6. A spot comparison of report totals against the provider UI within the
   provider's documented precision, or an explicit recorded mismatch.
7. Exact CLI version and commit.

The committed pilot record contains pseudonyms, totals-match status and
digests only; no account identifiers, names, tokens or raw payloads.

### Bring-your-own-app setup

Each provider has a setup guide in `docs/setup/<provider>.md` covering: which
developer app or key to create, required scopes, redirect or callback
configuration, access-tier limits that matter for read-only use (for example,
Google Ads needs Explorer or higher access (AdHelm recommends Basic) for the
Google Cloud project that owns your OAuth client, because Google retired developer
tokens on 2026-09-09, to read production accounts;
LinkedIn's Development tier allows reads on any ad account you administer and
edits on up to five), the exact `adhelm` enrollment command, and how to revoke.
Each guide is validated by running its commands against the fake provider in CI,
and reviewed against primary provider documentation with the fetch date
recorded.

### Platform

The full offline suite passes on `ubuntu-latest`, `macos-latest` and
`windows-latest` CI runners. Windows uses real Windows implementations for
selected-file input, credential locks and artifact writes (R02.1–R02.3 in the
[Windows diagnosis](implementation/windows-portability-diagnosis.md));
refusal-only behavior does not count as support. A fresh-machine install of
the built wheel, `adhelm discover`, `adhelm doctor` and an offline fixture
report pass on all three platforms in CI.

### Public tree

- The exported tree passes a secret scan (`gitleaks` or equivalent) and a
  denylist scan with zero findings for: the name of the owner's separate private
  project (case-insensitive) outside a single provenance note, the owner's
  personal name and email, absolute home paths, account or property identifiers
  from the pilot, and the retired product names in ADR 0013's rename table (the
  set `scripts/check_repository.py` rejects) outside historical ADR text.
- `LICENSE`, `NOTICE`, `README.md`, `SECURITY.md` (GitHub private
  vulnerability reporting), `CONTRIBUTING.md` (DCO), `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1) and issue templates exist.
- The dependency license inventory shows no license incompatible with
  Apache-2.0 distribution.

### Release engineering

- `pyproject.toml`: name `adhelm`, version `0.1.0`, Apache-2.0 metadata,
  classifiers, project URLs, `requires-python = ">=3.12"`.
- A tag-triggered GitHub Actions workflow builds the sdist and wheel once,
  runs the install smoke on three platforms, generates checksums, a CycloneDX
  SBOM and GitHub build provenance attestations, and publishes to PyPI through
  trusted publishing in a protected `pypi` environment.
- `CHANGELOG.md` has a `0.1.0` entry that states the read-only scope, the
  provider labels, and the known limitations below.

How the workflow builds, checks and publishes, what each check requires and what
it does not cover are in
[release engineering](implementation/release-engineering.md).

## Known limitations to publish

- No remote writes; write commands are visible but unreleased.
- No CRM attribution.
- Providers labelled `EXPERIMENTAL` have no live evidence.
- Users must obtain provider API access themselves; quotas and access tiers
  are the provider's.
- Some reads are bounded rather than exhaustive where a provider documents no
  pagination, and audits mark that coverage explicitly.
- Money is read in a few currencies only. Google Ads, LinkedIn Ads, Reddit Ads
  and OpenAI Ads project money exactly only for USD accounts. Meta Ads accepts
  AUD, CAD, CHF, DKK, EUR, GBP, HKD, JPY, MXN, NOK, NZD, SEK, SGD and USD, and
  TikTok Ads accepts AUD, CAD, EUR, GBP, JPY and USD. A read that carries money
  for an account in any other currency is refused with `UNSUPPORTED_CURRENCY`.

## Gate states

| State | Meaning |
| --- | --- |
| `IMPLEMENTING` | Required 0.1.0 code, tests, docs or export work remains |
| `READY_FOR_USER_AUTHORIZATION` | All offline gates pass on the exported tree; owner actions remain (repository creation, live pilots, PyPI trusted publisher, tag) |
| `RELEASED_0_1_0` | The owner pushed the exported tree, ran the pilots they chose, tagged `v0.1.0`, and the workflow published to PyPI |

The build session stops at `READY_FOR_USER_AUTHORIZATION`. The owner performs
every external action: registering the name, creating the public repository,
configuring trusted publishing, running the pilots they choose, and pushing and
tagging the release.
