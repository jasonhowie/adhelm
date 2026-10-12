# GA4 Admin key-event CREATE guarded adapter

`adhelm.providers.ga4_admin_key_event_create_adapter` is an internal,
development-fixture-only adapter for one retained
`Ga4KeyEventCreatePlannedAction`. It has no CLI command, generic provider
client, token argument, or persistent bearer representation.

`_configured_ga4_create_adapter(directory, action, ledger, *, fresh,
fingerprint_key, clock)` is intentionally private. Before it resolves the
single-use local OAuth handoff, it revalidates the exact action, the current
relevant retained sources, the configured OAuth EDIT profile and target, the
current configuration digest, and the current private fingerprint key. It
repeats those checks after OAuth consumption, before each inventory read, and
immediately before consuming the SafetyKernel permit. The runtime holds the
bearer only in a nonserializable, closed object and clears it when the context
ends. It opens `httpx.Client(trust_env=False, follow_redirects=False)` only
inside that context and fixes egress to `analyticsadmin.googleapis.com`.

The adapter implements `GuardedAdapter`. `apply()` always fails with
`PERMIT_REQUIRED`; `apply_guarded()` requires a durable kernel `APPLY_STARTED`
event and consumes the exact one-use permit immediately before the sole fixed
POST to:

`/v1alpha/properties/<configured-property-id>/keyEvents`

The body is reconstructed from the retained compiler candidate. There is no
provider idempotency key, dry-run, conditional create, delete compensation, or
blind retry. A lost response, 5xx, malformed response, failed independent GET,
or failed full collection read remains ambiguous and needs reconciliation and a
new plan.

Before POST, the adapter obtains a full fixed inventory under the private
snapshot and requires its keyed content, full inventory digest, semantic
projection, and count to equal the action baseline. It parses the POST result,
GETs the returned resource by its provider-assigned ID, then lists the full
collection again. The returned GET must match the POST result; the full list
must be exactly baseline members plus that result, with the Standard capacity
count advancing by one; and the semantic projection must equal the approved
after state. IDs, create times, custom/deletable fields, raw event names,
bearers, raw responses, and provider payloads are never persisted in the
adapter's proof. It returns only opaque/evidence digests.

The fresh OAuth association and property/stream GETs remain local configured
binding and resource-read evidence. They do **not** prove a currently granted
`analytics.edit` token scope, CREATE permission, reservation, provider-origin
semantics, browser or network firing, processed reporting, or execution
authority. The action remains
`DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`; approval acceptance and durable
execution workflow integration are separate ownership.

Fake-only tests cover the fixed POST, returned-resource GET, full after-list
verification, direct/forged permit refusal, source drift before POST, and
SafetyKernel durable-start/real permit flow. No test invokes Google, a browser,
or an OS credential store.
