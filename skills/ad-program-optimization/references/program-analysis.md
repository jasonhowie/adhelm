# Program analysis

Apply only the sections that resolve the current campaign decision. This
reference adds analysis detail to the skill; it grants no additional data access
or execution authority.

## Compatible evidence

Compare compatible periods and grains, accounting for conversion lag,
restatements, schedule changes, seasonality, and recent campaign changes.
Compute ratios from compatible summed numerators and denominators; do not
average row-level rates or count campaign rollups together with their ads.
Keep currencies separate. Use proven campaign joins; name similarity and
conflicting mappings cannot establish authoritative totals. Attribution-system
disagreement is a diagnostic, not proof one failed. State incomplete query
coverage instead of presenting a complete audit.

A cheap click or platform lead is not automatically a qualified business
outcome. Missing or duplicate conversion events require measurement diagnosis;
healthy traffic metrics may still support narrower observations. GA4 key events
are not automatically Google Ads conversions or native signals for other
providers. Do not fill missing downstream outcomes with inferred revenue.

## Pacing and bidding

Compare spend to elapsed eligible delivery time, remaining budget, and schedule.
Label run-rate projections and assumptions. Distinguish exhausted budget from
limited demand, policy issues, or conversion readiness. A local monitoring
reminder is not a provider-enforced spend cap.

When reviewing a retained Meta pause proposal, distinguish configured campaign
status from its separately observed effective status. The proposal's zero
incremental spend means it authorizes no budget increase; it does not measure
residual delivery or billing. An expired proposal remains historical evidence,
and a new observation interval requires a new compiled plan. Do not infer a
provider revision, conditional write or confirmed pause from local digests.
Optional Meta plan files preserve the same historical review and fixed as-of
time. Reuse matching canonical inputs and settings; a requested regeneration
does not refresh campaign observations. After an output failure, recover the
requested file from retained history without repeating provider planning.

Compare cost/value outcomes only where definitions and signal quality support
them. Consider recent changes, delivery constraints, and observed response to
past spend before suggesting increases or reductions. Avoid universal bid
percentages, learning-period claims, or significance thresholds without an
applicable documented basis. Preserve provider-native bidding semantics.

## Targeting and placements

Use supported aggregate keyword, placement, or audience-category evidence to
find mismatches with the approved ICP and exclusions. Keep expansion bounded to
marketer-approved scope. Do not infer person-level or sensitive traits or
prepare audience files. Low observed volume alone does not prove an exclusion
will improve the primary business outcome.

## Creative and destination

Propose hypotheses about message, hook, CTA, format, or declared destination
alignment using available creative records and aggregate outcomes. Do not scrape
landing pages or infer their contents. Offer test directions for human review;
final production assets and landing-page implementation are outside this
skill's execution scope.

Bind comparisons to content-addressed creative versions and effective-dated
deployments. An asset, text, CTA, destination, component order/pinning, or
rendered-content change creates a new version; a pure association change
preserves it. Exclude unsplittable mixed-version windows from single-version
conclusions. Use component/rendition outcomes only when the provider exposes
that grain; never duplicate ad metrics across components. Ad outcomes cannot
identify the winning headline, color, card, or dynamic combination.

Preserve extractor/analyzer versions, provenance, confidence, and missing
fields; source-media observations cannot silently substitute for a changed
served rendition. Cross-provider and confounded comparisons are directional,
not causal. A creative feature correlation is a test hypothesis.

## Experiments and follow-up

Before implementation, define changed and held-constant dimensions, primary
metric, observation window accounting for conversion lag, minimum evidence
needed, budget limit, stop criteria, and confounders. Verify declared constants
against available configuration and creative manifests. A pre/post improvement
alone does not establish incrementality.

Report observed results separately from predicted effects. Preserve the original
baseline and record new evidence rather than silently replacing history. Recheck
after the declared interval only when authorized; proposing a follow-up does
not schedule it. Keeping settings unchanged while collecting sufficient evidence
is a valid outcome.
