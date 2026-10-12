# GA4 Admin key-event create contract

This is a research and offline-compilation record. The compiler is not an installed
CLI command or schema. It is not live write or verification evidence.

## Primary API contract

The official [v1alpha `properties.keyEvents.create` reference](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents/create) defines one fixed write route:

```text
POST https://analyticsadmin.googleapis.com/v1alpha/{parent=properties/*}/keyEvents
scope: https://www.googleapis.com/auth/analytics.edit
```

`parent` is the exact property resource name. The [KeyEvent resource reference](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents) says that `eventName` is immutable, `countingMethod` is required, and `defaultValue` is optional. The compiler emits only those writable fields. It excludes output-only `name`, `createTime`, `deletable`, and `custom`, and it has no event-rule or generic parameter surface.

GA4's [event collection limits](https://support.google.com/analytics/answer/9267744?hl=en) cap event and parameter names at 40 characters, cap events at 25 parameters, and explain that an overlong marked key event will not be reported as one. The compiler accepts the deliberately narrower lower-ASCII event/parameter-name subset used by the local `EventContract`, with those bounds at its input and wire boundaries.

`defaultValue.numericValue` is an API JSON number. Input is a `Decimal`, then accepted only when finite and exactly representable by an IEEE-754 binary64 number; this avoids silently rounding a canonical decimal before a future API call. Accepted values are normalized to that exact binary64 value so cosmetic forms such as `1` and `1.0` produce the same immutable candidate. Signed zero is rejected because canonical JSON collapses it to unsigned zero. `currencyCode` uses the ISO-4217 uppercase contract. For example, `1.25` is admissible and `0.1` is rejected because it is not an exact binary64 value.

## Candidate boundary

`compile_ga4_admin_key_event_create` rebuilds its strict customer `EventContract` input at its public boundary, binds each local property/Web-stream reference to its declared native numeric ID using the configured local reference function, and accepts only the documented counting method and optional default value. A key event is property-scoped; the stream declaration is retained as an identity/verification prerequisite and is never invented as a request path. A mismatched local reference cannot label a target property. The compiler rejects its bounded sensitive-name terms in event and field names and scans semantic text for literal secret/person-level data; that is a fail-closed screen, not a claim to infer every semantic risk.

The result is frozen, content-addressed, and permanently marked `OFFLINE_CANDIDATE_NOT_EXECUTABLE`. It contains the sole POST target and a typed request body, along with explicit prerequisites for verified current property/stream identity, source-event collection eligibility and reserved-name review, existing key-event inventory and duplicate check, immutable-name semantics, privacy/consent review, exact direct/delegated authorization, pre-apply drift recheck, configuration readback, approved browser/network observation, GA4 Realtime, and processed reporting. The KeyEvent reference uses `click` as an example and can represent a default GA event, so this compiler does not falsely reject every collection-reserved/default name without a verified source-event context.

The future write is a `LIVE_CONFIGURATION_WRITE`: GA4 has no draft/workspace equivalent. It affects future matching event occurrences and does not backfill historical events. It neither creates a Google Ads conversion/import nor a native signal or optimization association for any advertising provider. Those require distinct typed operations and authorization.

The compiler has no transport, credential access, approval evaluator, browser activity, provider call, or mutation method. A later execution workflow must follow `doctor -> inventory -> immutable plan -> authorization -> reread/drift rejection -> durable pre-egress journal -> apply -> readback -> browser/realtime/processed verification -> evidence`.
