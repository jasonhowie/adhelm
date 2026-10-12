# GA4 key-event CREATE action contract

`adhelm.providers.ga4_create_contract` is a pure reconstruction boundary for one
GA4 Admin key-event `CREATE` action. It has no Ledger, configuration, OAuth
lifecycle, transport, provider, CLI, authorization, or mutation dependency.
It turns already selected local facts into a closed `CompositePlan`; it cannot
make those facts current, authorize the plan, or send a request.

## Inputs and source bindings

The builder accepts three typed local inputs plus fresh source input and the
private fingerprint key:

- `Ga4KeyEventCreateSelectionInput` holds a retained candidate, the exact
  retained conversion contract, candidate/contract record digests, a final
  checkpoint digest, and the UTC evaluation time. It recompiles the complete
  fresh `Ga4KeyEventCandidateInput` and requires byte-for-byte equality with
  the selected candidate.
- `Ga4KeyEventCreateObservationInput` holds the retained EDIT-inventory
  observation envelope facts, its complete normalized inventory and inventory
  digest, the configuration/profile digests, and the UTC read interval. Its
  inventory is revalidated under the current private fingerprint key before it
  is used.
- `Ga4KeyEventCreateExecutionBinding` holds only nonsecret local OAuth/profile
  association facts: the configured GA4 Admin `EDIT` profile, exact
  `analytics.edit` scope, account/principal/client references, configuration
  digests, and the fixed property/web-stream target. It records
  `LOCAL_SEALED_OAUTH_ASSOCIATION_ONLY`,
  `NOT_PROVEN_BY_RESOURCE_READ`, and `NOT_PREPROVABLE`; these are deliberate
  limits, not permission evidence.

The builder requires the selection and observation to use one final checkpoint
digest. It requires the observation interval to precede evaluation, the
conversion declaration to be effective at evaluation, and expiry to be 60--900
seconds after evaluation. It also binds profile/configuration/target facts
across the observation and execution binding.

The customer-declared destination profile is retained through the selected
candidate. It intentionally need not equal the technical GA4 Admin EDIT
credential profile. The action declares
`DECLARED_DESTINATION_SELECTION_ONLY` and
`destination_to_execution_profile_mapping=NOT_EVIDENCED`; no name-based or
account-label mapping is inferred.

## Exact plan

The resulting `Ga4KeyEventCreatePlannedAction` retains:

- a separate `LOCAL_LEDGER_WORKSPACE` binding;
- candidate/contract/observation/checkpoint/configuration/profile source
  digests and UTC interval facts;
- digest-only fixed native POST target and body bindings;
- the M23 semantic before/after state and the independent full inventory digest;
- a one-subplan `CompositePlan` for `ga4_admin`, `CREATE`, `v1alpha`, the
  fixed GA4 capability version, exact account identity and one
  `analytics.edit` scope;
- zero incremental spend in the observed property currency; and
- fixed `analyticsadmin.googleapis.com` egress with
  `RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION` and no compensation action.

The reconstructed plan identifier commits the complete closed workspace,
candidate, selection, observation, execution, native-request, semantic-state,
and expiry surfaces. A copied action therefore cannot change a configuration
digest, broad inventory-drift digest, checkpoint, read interval, or OAuth
association while keeping the same authorizable `CompositePlan` digest.

GA4 assigns the created resource identifier. The semantic state therefore
contains an HMAC-based collection transition over request-controllable event
semantics and preserves multiplicity, while the full inventory digest remains
separate for broad drift detection. No future resource identifier, delete, or
automatic compensation is invented.

The action status remains
`DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`. It also records that current
state must be reread before egress, configuration readback is required after a
write, native conversion mapping is not created, and browser, network,
realtime, processed-report, and semantic source mapping evidence are absent.

## Revalidation and retention boundary

`revalidate_ga4_key_event_create_planned_action(value)` accepts only an exact
bounded JSON-shaped mapping or the exact typed action. It checks original
scalar types before canonicalization, closes every object shape, rebuilds the
candidate and immutable `CompositePlan`, and rejects copied or constructed
models whose plan, controls, state, native digests, or source bindings no
longer agree. It establishes only self-consistency of retained local facts.

`validate_ga4_key_event_create_plan_binding(...)` additionally recompiles the
fresh candidate and reconstructs the current-keyed EDIT inventory semantic
state. A matching local HMAC proves neither provider origin, current remote
state, complete OAuth grant, create permission, name reservation, execution
authority, or a mutation outcome.

The action includes the local compiler candidate and exact native body only for
local retention/reconstruction. It has no agent-facing serialization API.
Downstream plan records, CLI projections, artifacts, and exports must apply the
existing redacted sharing boundary before output, so raw event names, native
property/stream identifiers, body values, opaque markers, and fingerprint keys
do not reach agent-visible output.

## Remaining gates

Before a remote CREATE can exist, a configured workflow and guarded adapter
must independently obtain a current protected OAuth binding, validate the
configured profile and account association, reread the exact property, stream,
and complete inventory, revalidate drift and capacity, obtain exact current
authorization, append the required safety history, dispatch one fixed guarded
POST, reconcile an ambiguous outcome without blind retry, and verify readback.
The later readback must compare the semantic post-state without claiming that a
key-event alone proves browser firing, GA4 receipt, processed reporting, or a
Google Ads/other-provider conversion mapping.
