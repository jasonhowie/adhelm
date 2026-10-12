# Skills and agent guidance (ticket O3.4)

Decision: [ADR 0013](../adr/0013-open-source-release.md) decisions 1, 7, 9 and 10.
Gate: [`release-0.1.0.md`](../release-0.1.0.md) ("Agent interface").
Contract: [`agent-skills.md`](../agent-skills.md).

AdHelm 0.1.0 ships two bundled skills, `ad-program-optimization` and
`conversion-optimization`, and the `guidance catalog` / `guidance show` commands that
serve their authored text. This ticket changes the text, not the commands: the
`guidance` output is the skill documents, so updating the documents updates it. The
guidance version of each skill moved from `0.2.x` to `0.3.0`, which
`tests/test_guidance.py` pins.

## What changed in the text

- **Edition and paid language removed.** The read-only edition paragraphs, the
  `OPERATOR_EDITION_REQUIRED` and `DEVELOPMENT_ONLY_SETUP_REQUIRED` references, the
  `NATIVE_*_APPROVAL_BRIDGE_*` refusals that the release-policy gate now pre-empts,
  "signed release" wording and the old product name are gone.
- **A "What this release does and does not do" section** in each skill states that
  0.1.0 is read-only; that write commands are `UNRELEASED` and fail with
  `WRITES_NOT_RELEASED` (exit `3`) and nothing lifts that; that planning commands
  stay available and say execution is unreleased; that each provider carries a
  `LIVE_PILOTED` or `EXPERIMENTAL` label which the agent states with its meaning;
  and that CRM attribution is not part of the release.
- **A "Hand changes back to the marketer" section** in each skill replaces the old
  execute-and-recover guidance. On a request to apply a change or a recommendation
  the agent says the release cannot, does not probe or retry the refusal or look for
  another route, gives a specific manual step for the provider's own interface, and
  offers to verify by reading again. The verification claim is the one the build
  supports: a later read and audit records a hand-made change as an out-of-band
  change with actor `UNKNOWN` and an observation interval; the agent does not say the
  CLI applied or verified it or who made it.
- **Execution paragraphs rewritten.** `execute openai-pause`,
  `execute meta-campaign-pause` and `execute ga4-key-event-create` are described as
  registered and refused. The doctor-to-recovery flow is kept as the design contract
  for a later release and stated as unimplemented past the plan. Delegation and
  composite-apply guidance is removed because no release behavior uses it.
- **GTM reference.** Staging, versioning, publishing and browser tests are described
  as out of the release, with no command for them and a prohibition on improvising
  one. The read surface (`measurement gtm`, `measurement gtm-graph`) is named, and
  deletion advice still requires a `PROVEN_UNREFERENCED` proof.
- `NEEDS_SITE_INSTRUMENTATION` is now described as the agent's finding label for a
  proposed contract, since the CLI does not emit it. The previous text told the agent
  to "surface" it as if it were CLI output.

## Scenarios and their limits

`tests/test_skill_scenarios.py` calls no model, so it cannot show that an agent
behaves. It pins what an agent's behavior depends on:

| Check | What it proves |
| --- | --- |
| Stale vocabulary | No edition, Auditor/Operator, paid, signed-release, old-name or origin-repository wording in any bundled document, and none of the phrases that described capabilities 0.1.0 lacks (runtime verification, configured CRM aggregates, repair through a supported plan, reviewed tag/event or native signal setup). |
| Named commands | Every backticked span that begins with a registered top-level command resolves in the registered Typer tree, a group followed by a word that is not an option or placeholder is rejected as an invented subcommand, and every `--option` in the span belongs to the resolved command (or is a global option). A span of two or more bare lowercase words whose first word is not a registered command (for example `apply recommendation`) is rejected. Single-word and placeholder fragments such as `catalog` or `show <record-digest>` are not checked. |
| Named schemas and codes | Every schema is listed by `discover`; every status or error code appears in `src/`. This is what caught the unemitted `NEEDS_SITE_INSTRUMENTATION`. |
| Served text | `guidance show` returns exactly the authored files and descriptions name AdHelm. |
| Scenario clauses | For eight requests (apply a recommendation, enable writes, which providers to trust, prepare a plan, create the GA4 key event, publish GTM, audit and recommend, connect a CRM) the named document states the required handling. These are phrase checks, so a reworded sentence must update the clause. |
| Negative scenario | A fixture ledger yields a read-only audit recommendation (`NOT_EXECUTED`, `NOT_A_PLAN`, no authorization, and text that names no authorization or execution path and points at the marketer's manual step), then `execute plan` is refused with `WRITES_NOT_RELEASED`, exit `3`, a manual next step naming the provider's own interface and the release policy, with the credential store, ledger, kernel and egress guarded and the data directory unchanged. |
| Every refusal | The same holds for every registered execution leaf, and the three provider-naming ones carry the current label and its meaning from `release_labels`, so the check does not depend on whether a pilot record is committed. |
| Verify by reading again | After a second retained read shows the campaign paused, the audit reports drift with actor `UNKNOWN`, a non-null ordered observation interval, no exact change time, and a campaign change confined to `normalized_state.status`, no longer proposes the pause, and `history verify` passes. |

Scenario data is synthetic and is not provider evidence. No capability-matrix row
moves: skills and guidance are not provider capabilities and promote nothing.

## Known limits

- The skills still describe many development-era read commands at length. They were
  not shortened because that was outside the ticket; a later pass can trim them.
- The PAUSE recommendation text of the OpenAI, Google Ads and Meta audits was
  corrected in this ticket. It no longer says that a separate authorization would
  stop spend. It states that execution is unreleased in 0.1.0 and that the pause is
  the marketer's manual step, and it still names the offline `plan ...` route.
- The GTM audit findings still say a repair or deletion needs "a separate
  authorization". These are findings about GTM configuration, not recommendations,
  and the wording is a follow-up for the coordinator.
- Named-host usability validation is not part of 0.1.0.
