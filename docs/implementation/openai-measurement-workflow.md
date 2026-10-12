# OpenAI conversion-settings measurement read workflow

`adhelm.openai_measurement_workflow.read_openai_conversions` is the read-only
workflow component used by `measurement openai-conversions`, composing of the fixed OpenAI Ads account discovery and conversion
setting inventory clients. It returns a typed `OpenAIAdsIdentity` and a complete
tuple of `OpenAIAdsConversionSetting` records. It does not append a ledger
record, register a CLI command, create an artifact, retain a raw provider
response, or invoke a remote mutation. The caller owns the observation time
interval and any durable `MEASUREMENT_OBSERVATION` receipt.

`OpenAIConversionsReadRequest` is the public, schema-versioned
(`schema_version: 1`) strict request model for the selected profile, page bounds,
and CI opt-in. Its defaults retain the existing 500-record/500-page-size bound
and disabled CI opt-in; callers can obtain its JSON Schema without configuration,
credential, or HTTP access.

The workflow first validates its public arguments, including the ten-page
complete-result bound, then reloads and revalidates the local configuration
before it can access credentials or construct HTTP. It selects exactly one
`openai_ads` profile with purpose `READ`; another provider, another purpose,
a duplicated profile, or an invalid model-copy fails closed. The profile must
declare a currency and the account discovered in this run must match the
configured raw account ID, time zone, and currency before the event-settings
route is called. Before credential access, the selected profile's account ID
must also use the same bounded opaque-ID grammar accepted from discovery, and
its timezone must be an IANA timezone. `principal_ref` and `client_ref` are
revalidated closed local declarations before that boundary.

OpenAI Ads documents account-scoped bearer API-key authentication, but the
reviewed source does not establish a stable OAuth scope vocabulary or a live
least-privilege scope. The workflow therefore verifies the configured scope
list structurally through the closed `Profile` model and uses local `READ`
purpose as its operation boundary. It deliberately does not compare the list
with an invented provider scope string or claim that the local declaration
proves provider-side authorization.

The workflow resolves one bounded printable-ASCII credential after profile
preflight and before constructing its HTTP client. Its getter returns only that
snapshot to account discovery and every conversion page, so a local credential
rotation cannot bind those reads to different bearers. One
`httpx.Client(trust_env=False)` and fixed-host `SafeHttpClient` serve both
requests. The workflow calls `GET /v1/ad_account` first, then supplies
`OpenAIAdsClient.is_current_identity` to the conversion reader. That verifier
requires the exact returned object plus an immutable detached discovery snapshot;
a lookalike model, a forged `model_copy`, an in-place altered cached object, or
an identity replaced by a later discovery is rejected before the conversion
reader retrieves a token or sends its request. The discovery adapter also
requires the returned account timezone to be IANA-valid before it caches that
identity. The reader can only issue the fixed current-account
`GET /v1/conversions/event_settings` route.

The conversion reader buffers a complete bounded list locally. A late page
failure, malformed page, identity mismatch, result cap, or pagination failure
raises an error and returns no partial tuple. Account and setting display names,
source IDs/names, campaign association values, tokens, and raw response bodies
remain transient inside the provider parsers and are not emitted by this
workflow.

`tests/test_openai_measurement_workflow.py` uses mocked HTTP to verify the
fixed same-session call sequence, selected-profile provider/purpose failure
before credential or HTTP access, model-copy scope rejection, raw account/time
zone/currency binding before conversion inventory, public page-bound rejection,
late-page no-partial behavior, and exact-object identity cache behavior.
`tests/test_openai_workflow_ultra_review.py` adds schema, semantic profile,
credential-snapshot, malformed-bearer, detached-cache, raw-account-display-name,
and provider-timezone regressions.

This is fixture evidence only. The OpenAI Conversion Setup reference documents
the route and request parameters, but does **not** publish a list-response
example. The closed list envelope and setting response shape used by the reader
remain unverified against a live provider response. The workflow does not
establish live access, API-key lifecycle, provider-side least privilege,
conversion delivery, browser firing, reporting semantics, or any write
capability.

The installed command adds canonical retention and redacted review through
[measurement history](measurement-history.md). The component itself performs
no ledger or artifact writes.
