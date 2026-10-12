# AdHelm

> AdHelm 0.1.0 lets a marketer direct their own AI agent through a local,
> free, Apache-2.0 CLI to connect their own ad and analytics accounts, inventory
> them, report aggregate performance, audit them, and receive evidence-backed
> recommendations, with append-only local history and redacted exports. It
> makes no changes to any advertising or analytics account.

AdHelm is a command-line tool built to be driven by your AI agent, such as
Claude Code or Codex. You describe the goal in conversation. The agent discovers
the installed commands, calls them, and reports back in the form you prefer. You
do not need to know shell syntax. Direct human use of the CLI is possible but
secondary.

Everything runs on your machine. There is no hosted service, telemetry, account,
license key, or paid tier. You register your own provider applications and keep
your own credentials.

## Status

`0.1.0` is the first public release and it is **read-only**. Every command that
would change an advertising or analytics account is listed by `adhelm discover`
with status `UNRELEASED` and is refused at runtime; see
[What 0.1.0 does not do](#what-010-does-not-do). The release is gated by the
[0.1.0 release definition](docs/release-0.1.0.md). It makes no `COMPLETE`,
live-parity, write-support or compliance claim.

## What you can do

For each provider below, in `0.1.0`:

- enroll your own application or key locally, check its status, refresh it where
  the provider supports refresh, remove it locally, and read the provider's
  revocation guidance;
- read the account identity, a fixed inventory and a fixed daily aggregate
  report;
- run audits that state, per dimension, what was and was not read, and receive
  recommendations that name their evidence and change nothing;
- keep an append-only local history, verify it, reconcile out-of-band changes,
  and export deterministic CSV and JSON projections from a rebuildable SQLite
  index; and
- ask for optional documents, spreadsheets or HTML reports when you want them.
  Compact conversational summaries are the default.

Two [bundled skills](docs/agent-skills.md) teach your agent the diagnose,
recommend and verify loop. `adhelm guidance catalog` lists them.

## Install

AdHelm needs Python 3.12 or newer and runs on macOS, Linux and Windows. CPython
3.12 is the tested and supported version.

```sh
uv tool install adhelm
```

or

```sh
pipx install adhelm
```

Check the install:

```sh
adhelm version
```

To work on AdHelm itself, see [CONTRIBUTING.md](CONTRIBUTING.md).

## First run

Ask your agent to start with `adhelm discover`. It returns the installed
commands, schemas, exit codes, release policy and provider labels, and it makes
no network or credential-store call. You can run the same commands yourself:

```sh
adhelm init
adhelm doctor
adhelm discover
adhelm guidance catalog
```

`init` creates local defaults without credentials. `doctor` reports local
readiness and does not contact any provider. Without `--data-dir`, local data
lives in the platform data directory: `~/.local/share/adhelm` on Linux,
`~/Library/Application Support/adhelm` on macOS and `%LOCALAPPDATA%\adhelm` on
Windows.

Then follow the setup guide for each provider you use. A guide covers which
developer app or key to create, the scopes it needs, the redirect configuration,
access-tier limits that matter for read-only use, the enrollment command, and how
to revoke. Secrets are entered through a local interactive handoff and stored in
your operating system's credential store. They are never accepted in command
arguments or JSON.

## Providers

Each provider carries exactly one release label. `LIVE_PILOTED` means the
provider is fixture-verified and was read-piloted on a live account, with a
redacted pilot record committed under [`docs/pilots/`](docs/pilots/README.md).
`EXPERIMENTAL` means it is fixture-verified only and has no live evidence. The
label states evidence and never a capability. `adhelm discover`, `adhelm doctor`
and every output that names a provider show the same label.

| Provider | Label | Read in 0.1.0 | Setup |
| --- | --- | --- | --- |
| Google Ads | `EXPERIMENTAL` | Account identity, campaign hierarchy, daily aggregate report, audit, recommendations | [setup](docs/setup/google_ads.md) |
| Meta Ads | `EXPERIMENTAL` | Account identity, campaign hierarchy, daily aggregate report, audit, recommendations | [setup](docs/setup/meta_ads.md) |
| LinkedIn Ads | `EXPERIMENTAL` | Account identity, campaign hierarchy, daily aggregate report, audit, recommendations | [setup](docs/setup/linkedin_ads.md) |
| TikTok Ads | `EXPERIMENTAL` | Account identity, campaign hierarchy, daily aggregate report, audit, recommendations | [setup](docs/setup/tiktok_ads.md) |
| Reddit Ads | `EXPERIMENTAL` | Account identity, campaign hierarchy, daily aggregate report, audit, recommendations | [setup](docs/setup/reddit_ads.md) |
| OpenAI Ads | `EXPERIMENTAL` | Account identity, campaign hierarchy, daily aggregate report, audit, recommendations | [setup](docs/setup/openai_ads.md) |
| GA4 | `EXPERIMENTAL` | Data API aggregate reporting, read-only Admin inventory, audit | [setup](docs/setup/ga4.md) |
| GTM | `EXPERIMENTAL` | Container inventory, dependency graph, read-only audit | [setup](docs/setup/gtm.md) |

`doctor` expects GA4. GTM is optional, and a provider you have not configured
does not block work on the others. Provider quotas, approvals and access tiers
belong to the provider. You must obtain API access yourself, and a provider can
change or withdraw its API.
For Google Ads, API access belongs to the Google Cloud project that owns your OAuth
client, at Test, Explorer, Basic or Standard; there is no developer token (Google retired
them on 2026-09-09), and reading a production account needs the project to be upgraded on
the Google Ads API Overview page in Cloud Console. AdHelm cannot observe the project's
level.

## What 0.1.0 does not do

- **No remote writes.** Pausing, creating, activating, maintaining, GA4 Admin
  edits and GTM staging, versioning and publishing are all refused with
  `WRITES_NOT_RELEASED` (exit code `3`) before any credential-store access,
  ledger `APPLY_STARTED` entry or network request. No configuration value,
  environment variable, flag or edited file enables them. Offline compile, plan
  and preview commands remain available and say that execution is unreleased.
  When your agent recommends a change, you make it by hand in the provider's own
  interface.
- **No CRM attribution.** Salesforce and HubSpot are not part of `0.1.0`.
- **`EXPERIMENTAL` providers have no live evidence.** Their reports and audits
  are tested against fixtures, and their totals have not been compared with the
  provider's own interface.
- **Some reads are bounded, not exhaustive, and some are narrower than the
  provider's API.** Where a provider documents no pagination, such as LinkedIn
  analytics, audits say so instead of presenting the result as complete. The
  [capability matrix](docs/capability-matrix.md) lists each provider's limits.
- **Money is read in a few currencies only.** Google Ads, LinkedIn Ads, Reddit
  Ads and OpenAI Ads project money exactly only for USD accounts. Meta Ads
  accepts AUD, CAD, CHF, DKK, EUR, GBP, HKD, JPY, MXN, NOK, NZD, SEK, SGD and USD.
  TikTok Ads accepts AUD, CAD, EUR, GBP, JPY and USD. A read that carries money
  for an account in any other currency is refused with `UNSUPPORTED_CURRENCY`
  rather than converted approximately, so check your account currency before you
  register an application.

## Privacy and safety

- The default runtime has no vendor proxy, hosted database, telemetry, crash
  upload, automatic updater, or remote license heartbeat.
- Interactive secrets live in the operating system credential store.
  Environment variables are for explicitly configured CI use only.
- Logs, plans, evidence and exports are local, redacted, deterministic and
  user-initiated. Raw provider responses are not persisted by default.
- Output your agent reads follows the same redaction rules, but your agent may
  forward what it reads to its model. Share only what you are comfortable
  sending there.
- Each configured provider still receives the requests you make of it. Local
  execution reduces vendor custody. It does not make you compliant with HIPAA,
  GLBA or any other regime, and it does not replace legal, provider-policy or
  security review.

## Documentation

- [0.1.0 release definition](docs/release-0.1.0.md) and
  [ADR 0013](docs/adr/0013-open-source-release.md), which decide this release
- [Product brief](docs/product-brief.md) and
  [definition of done](docs/definition-of-done.md) for the `v1.0.0` target
- [Bundled agent skills](docs/agent-skills.md)
- [Capability matrix](docs/capability-matrix.md), the evidence level of every
  provider operation
- [Direct and delegated authorization](docs/authorization-policy.md)
- [Reporting schema](docs/reporting-schema.md) and
  [local history ledger and exports](docs/local-ledger-and-exports.md)
- [Threat model](docs/threat-model.md) and [egress map](docs/egress-map.md)
- [Architecture decisions](docs/adr/)
- [AGENTS.md](AGENTS.md), the rules every contributor and agent follows

## Contributing and security

Contributions are welcome under the Developer Certificate of Origin; read
[CONTRIBUTING.md](CONTRIBUTING.md). Report a vulnerability privately as
[SECURITY.md](SECURITY.md) describes, never in a public issue. Participation is
governed by the [Code of Conduct](CODE_OF_CONDUCT.md). Changes are recorded in
the [changelog](CHANGELOG.md).

## License

AdHelm is licensed under the Apache License, Version 2.0; see the `LICENSE`
file. The license does not grant rights to the AdHelm name or marks, and a fork
that redistributes the software must not imply endorsement by this project.
