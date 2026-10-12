# Bundled agent skills

Last updated: 2026-10-10

## Purpose and current status

The product includes versioned skills for the customer's agent to help improve
paid-media programs and conversions through the typed local CLI. The marketer
directs business goals; skills help the agent choose analyses, interpret
evidence, and prepare supported offline work. In AdHelm 0.1.0 that work is
read-only: the skills diagnose, recommend and verify, and the marketer makes every
change to an account by hand (see
[0.1.0 release behavior](#010-release-behavior)).

The repository contains authored skill source bundled into the development
wheel and an executable development CLI. Its installed `discover` output
distinguishes available local commands, fixture-tested read subsets, and missing
provider capabilities. There is no live-verified skill/connector workflow yet.
Frontmatter validation and offline exercises do not establish provider or
named-host compatibility.

## Package

| Skill | Use |
| --- | --- |
| [ad-program-optimization](../skills/ad-program-optimization/SKILL.md) | Pacing, budget/bid/targeting/placement diagnosis, creative comparisons, prioritized actions, and experiments |
| [conversion-optimization](../skills/conversion-optimization/SKILL.md) | Declared outcome meaning, signal-health diagnosis, aggregate funnel evidence, conversion experiments, and GA4/GTM measurement recommendations the marketer applies by hand |

Each folder is a portable unit with `SKILL.md` and only the local references its
workflow needs. It must not depend on this repository's absolute path, a global
skill installation, or reading every product document at runtime. Keep skill
descriptions precise so the host can select relevant guidance; keep detailed
procedures in conditionally loaded references. Do not load both skills or every
reference for every request.

The release bundles these folders in the wheel (its `bundled_skills` directory) and
serves them through `guidance catalog` and `guidance show`, each with its version
and document digest. A skill discovers the
actual installed command/capability contract and never invents executable names,
flags, or a fallback API implementation. If the CLI is missing or incompatible,
the agent explains the limitation and may provide clearly labeled advisory
analysis of user-supplied safe evidence. It cannot claim live inspection,
compiled plans, or applied/verified changes.

Customers choose whether and where to install the package in their agent host.
The product never silently changes global agent configuration, installs another
CLI/MCP server, authenticates a provider, or launches an agent. Discovery and
skill selection do not grant authority for provider reads or writes.

Discover the installed release policy, provider labels and commands separately
from provider support and credential scopes. Discovery, a label or a profile never
supplies provider permission or approval.

## 0.1.0 release behavior

Decided by [ADR 0013](adr/0013-open-source-release.md) and gated by
[`release-0.1.0.md`](release-0.1.0.md). Both skills, at guidance version `0.3.0`,
state the following and nothing in them may contradict it:

- **Writes are unreleased.** A write command fails closed with
  `WRITES_NOT_RELEASED` (exit `3`) before configuration, credentials, history or
  network ([release-policy gate](implementation/release-policy-gate.md)). Nothing
  an agent, a plan, an artifact or the marketer supplies lifts it. The skill does
  not probe the gate, retry, rephrase, or search for another route. It hands the
  marketer a specific manual step for the provider's own interface, then offers
  to verify by reading again; a hand-made change appears later as an out-of-band
  change with actor `UNKNOWN`, never as something the CLI applied.
- **Planning stays available** and says execution is unreleased. A plan, digest,
  receipt or artifact is never an applied or approved change.
- **Provider labels** (`LIVE_PILOTED`, `EXPERIMENTAL`;
  [provider release labels](implementation/provider-release-labels.md)) are
  reported with their meaning when a provider is discussed. A label is evidence,
  not a capability.
- **No edition, paid tier, vendor-registered app or CRM attribution.** The
  Account Auditor and Operator editions are removed; Salesforce and HubSpot are
  outside the release.
- Prefer READ profiles. The skills ask for an EDIT-purpose profile only when the
  marketer wants an offline plan that needs one.

How the skills and scenarios are validated is in
[skills and guidance](implementation/skills-and-guidance.md).

## Output and token discipline

Skills honor the effective output policy in
[`lifecycle-contract.md`](lifecycle-contract.md#output-and-artifact-preferences):
compact conversation by default, optional artifacts off/on-request/rules,
selected purposes/stages/formats, local destination, and per-task overrides.
They do not produce documents, spreadsheets, or HTML simply because a skill
loaded. They retrieve bounded fields/windows, reuse relevant report references,
and load detail progressively. They must not suppress uncertainty or the facts
needed for direct or delegated authorization to meet a presentation budget.

This reduces avoidable context and generation. It is not a guaranteed model
token allowance, support-cost reduction, or billing cap for an external host.

## Decision and execution boundaries

- Recommendations identify evidence, observation versus inference, assumptions,
  confidence, expected impact with its basis, spend implications, and the next
  decision. Agent interpretation stays distinguishable from deterministic CLI
  findings. No fabricated lift, universal performance threshold, or causal claim.
- Diagnose signal reliability before relying on conversion outcomes. Compare
  compatible windows, currencies, counting units, and attribution definitions.
  Preserve exact creative versions and provider-reported metric grain.
- Skills use only supported operations. For a release that executes writes, that
  means the common approval, drift, pre-egress ledger, verification, ambiguity,
  and recovery flow, with each exact plan bound to direct authorization or a
  user-configured delegation policy; skills cannot widen scope or reset
  cumulative limits. AdHelm 0.1.0 releases no write, so its skills stop at the
  offline plan.
- Discover actual CRM aggregate and provider-native conversion capabilities
  before use; newly scoped adapters are not automatically implemented. Skills
  do not extend v1 to CRM records/discovery, website crawling, person-level
  uploads, enhanced identifiers, or arbitrary provider code. User-declared conversion meaning and safe event
  contracts remain prerequisites, not facts inferred from a button or stage name.
- Conversion, landing-page, offer, and creative hypotheses can be discussed from
  approved evidence. A proposed site or sales-process change is not an operation
  this CLI can claim to have implemented. Optional artifact settings govern its
  human handoff; mandatory typed plans and records retain their own contracts.

## Validation and release evidence

Validate skill frontmatter, local references, version metadata, portable
packaging, and compatibility discovery. Use bounded offline scenarios to assess
behavior with missing CLI/access, stale or conflicting evidence, insufficient
conversion semantics, mixed attribution/currency, unavailable creative grain,
output preferences, and suggested approval/retry bypasses. Scenario fixtures
are explicitly synthetic and do not become provider evidence.

Default tests do not call a model or provider. The offline deterministic
scenarios in `tests/test_skill_scenarios.py` pin the installed CLI's answers and
the handling the guidance states for them, including a negative scenario where an
agent asks to apply a recommendation and receives the unreleased refusal with a
manual next step. They do not show that a model follows the guidance.
Customer-authorized named-host
usability validation separately demonstrates skill selection, useful analysis,
preference-respecting output, direct/delegated authorization, supported execution, and honest
verification. The release manifest records tested host and CLI/skill versions.
The 0.1.0 release records no named-host validation and claims no named-host
compatibility. An authored skill cannot promote any `UNRESEARCHED` provider
capability.
