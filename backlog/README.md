# Phase 2 deployment backlog

Last updated: 2026-09-04

> **Status:** `POST_V1_BACKLOG`
>
> **Implementation:** not implemented; documentation only
>
> **V1 completion requirement:** none

Phase 2 explores offering the same paid-media operating engine through a remote
MCP service. The first track is assisted installation in a customer's cloud
account. The follow-on track is a service operated in our cloud account for
customers who prefer to delegate hosting.

These are product roadmap tracks. They are distinct from the numbered v1
engineering phases of the project's build plan, including its
existing "Phase 2 — Read/audit/report plane."

## Roadmap

| Track | Outcome | Prerequisite | Detailed backlog |
| --- | --- | --- | --- |
| Phase 2A | Assisted customer-hosted MCP: setup wizard, tested agent configuration, diagnostics, and an operations handoff | Proven core capabilities; explicit promotion of the deployment work | [Customer-hosted MCP](phase-2-customer-hosted-mcp.md) |
| Phase 2B | Vendor-hosted MCP: dedicated customer instances first, then shared infrastructure only if justified and independently validated | Reuse the 2A engine/interface and operational evidence; approve the changed custody and service contract | Vendor-hosted MCP (not published) |

The intended order is v1 -> 2A feasibility and deployment -> 2A pilot and
handoff -> 2B custody/service design -> 2B dedicated pilot -> optional shared
service. A narrowly scoped private pilot is a validation stage, not a claim of
full provider or deployment parity. Broader release claims require their own
capability and evidence matrix.

## Meaning of "security lite"

The user's term means the customer relinquishes control over hosting. Use
**vendor-hosted** or **managed hosting** as the working product terminology.
This backlog does not define a weaker security mode or weaker authorization.

| Property | Customer-hosted, Phase 2A | Vendor-hosted, Phase 2B |
| --- | --- | --- |
| Cloud account and infrastructure administration | Customer | Vendor |
| Provider credential custody | Customer-controlled secret store | Vendor-controlled, customer-scoped secret store |
| Canonical history and selected media | Customer-controlled storage | Vendor-controlled storage under explicit service terms |
| Updates, backup operation, and incident response | Customer owner, with contracted assistance | Vendor owner |
| Provider account permissions and business decisions | Customer | Customer |
| Exact direct/delegated authorization, spend bounds, drift rejection, verification, and recovery | Required | Required |
| Agent/model data flow | Customer selects agent; allowlisted results may reach its model | Same boundary, plus explicit vendor custody |
| Exit path | Customer retains installation and data | Verified export, reauthorization, migration, and documented deletion |

A dedicated instance in our account is vendor-hosted. Installation assistance
in the customer's account is customer-hosted; retained vendor administrator
access must be separately disclosed, bounded, and revocable.

## Scope and promotion rules

- This folder records future requirements. It authorizes no deployment,
  credentials, provider access, purchases, customer outreach, or implementation.
- The current [v1 definition of done](../docs/definition-of-done.md), required
  provider set, GA4/GTM gates, local runtime, pricing hypothesis, and
  release evidence remain authoritative. Hosting is not a new v1 dependency.
- Promote each track with an explicit product-owner scope decision, selected
  deployment target and agent hosts, dated provider/access research, custody
  and threat-model decisions, assigned owners, acceptance evidence, and a
  separate release claim. Hosting a proven connector does not automatically
  establish hosted authorization rights or operational parity.
- Preserve the shared safety kernel, deterministic schemas, canonical history,
  regulated data restrictions, and customer-selected agent boundary. Any
  hosting-specific deviation from a local-only requirement needs a documented
  future contract decision before implementation; these documents identify
  those decisions rather than silently changing v1.
- Inherit [direct and policy-based authorization](../docs/authorization-policy.md).
  Covered work does not need a fresh human confirmation; every plan still needs
  a one-use, digest-bound authorization. Activation and GTM publish remain
  separate operation kinds requiring their own direct or explicit delegated
  authority. A policy cannot authorize hosting promotion or change data custody.
- Compact chat and artifacts on-request are the proposed hosting defaults.
  Hosting does not require a report for every call or introduce an embedded model. See
  the [lifecycle contract](../docs/lifecycle-contract.md) for canonical review
  and execution requirements; adapt installed agent guidance when available.
- CRM metadata discovery, website crawling, server-side GTM, and the
  [conversion architecture design](../docs/future-conversion-architecture.md)
  remain separate backlog work. They are not prerequisites for either hosting
  track and are not promoted by this roadmap.
  The [v1 aggregate CRM and native conversion scope](../docs/conversion-integrations.md)
  does not promote discovery or either hosting track.

All milestone checkboxes are uncompleted work. Timelines, infrastructure sizes,
pricing, service levels, supported clouds, and named-agent compatibility remain
uncommitted until measured and approved for the relevant track.
