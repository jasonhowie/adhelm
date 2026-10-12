# Funnel analysis and experiments

Use this reference only when the question requires aggregate funnel comparison,
landing/creative hypotheses, or experiment planning.

## Analyze the supported aggregate grain

Examine privacy-safe cuts relevant to the question: campaign, approved
landing-page reference, device, placement, creative version, or declared funnel
stage. Each rate needs its numerator, denominator, counting unit, window, and
quality caveats. Incompatible event/session/user counts do not establish a
coherent funnel or journey; never reconstruct individual paths. Preserve
zero-denominator and missing states. Compare matched windows and account for
conversion lag, traffic mix, seasonality, and concurrent changes.

Bind creative analysis to its exact content-addressed version and deployment
window. Ad-level delivery cannot identify a winning headline, color, or CTA
without component exposure. Avoid summing overlapping grains or duplicating
ad metrics across assets.

Use marketer-declared offer, destination, and CTA descriptions plus approved
creative descriptors to discuss message continuity and landing hypotheses.
Do not treat these declarations as observed site behavior. Site and sales-process
implementation remains outside the CLI's executable scope.

## Design a decision the available traffic can support

Specify the changed dimension, control/comparison, primary metric, quality
guardrails, minimum observation and conversion-lag requirements, allocation and
budget constraints, evaluation point, and stopping/continuation rule.

Choose observation requirements from baseline volume and the decision's useful
effect size; do not prescribe a universal sample size. If traffic cannot support
the decision, recommend a longer window, narrower question, or more reliable
signal and mark the conclusion inconclusive. Preserve conversion-quality
guardrails so cheaper intermediate actions cannot silently replace the declared
business outcome.

Distinguish randomized evidence from directional before/after or cross-platform
comparisons. Record confounders and hold the declared measurement/counting
contract constant; changing it breaks a simple before/after comparison.
Recommend further observation when results cannot distinguish the hypothesis
from normal variation. Do not call a directional result causal.
