# OpenAI Ads conversion-setting read contract

Research date: 2026-09-04.

## Decision

The OpenAI Ads API documents one account-scoped, read-only conversion-definition
operation that fits the local inventory boundary:

```text
GET https://api.ads.openai.com/v1/conversions/event_settings
```

The fixed reader in `adhelm.providers.openai_conversions` sends only this route.
It accepts only the documented `limit` (1–500), `after`, and `order` (`asc` or
`desc`) controls; it deliberately never sends `before`, a caller-controlled
path, or a free-form filter. It contains no create, update, archive, pixel,
Conversions API key, upload, or event-delivery operation.

The primary source is OpenAI's current [Conversion Setup API
reference](https://developers.openai.com/ads/api-reference/conversion-setup).
That reference says this route lists conversion definitions for the **current
ad account**, gives the documented pagination parameters, and shows the
conversion-setting resource fields used in this contract.

## Exact identity and session boundary

The route itself has no account path parameter. The reader therefore requires
all of the following before it retrieves a token or sends HTTP:

1. The query account reference must equal the supplied identity's account
   reference.
2. The identity's opaque `account_client_ref` must regenerate the canonical
   `openai_ads.account.<id>` reference.
3. A required injected `identity_session_verifier` must return the literal
   boolean `True` for the exact discovered identity object. A callback failure,
   false result, or caller-constructed lookalike fails closed.
4. Every returned setting's `ad_account_id` must exactly equal that identity's
   `account_client_ref` before the setting enters a local model.

The existing `OpenAIAdsClient` owns the current identity-discovery cache, while
this deliberately isolated reader cannot inspect its private state without
changing that existing adapter. A later composition point must pass a verifier
that proves object identity from the same configured local read profile and
same discovery session; a callback that merely compares strings is not enough.
There is intentionally no CLI, workflow, persistence, or compiler wiring in
this slice. That prevents a caller-made `OpenAIAdsIdentity` from becoming an
account proof merely because it has canonical-looking fields.

## Closed output contract

The provider's documented create response shows these setting fields: `id`,
`name`, `event_type`, `custom_event_name`, `attribution_window_days`,
`ad_account_id`, `source_ids`, `sources`, `campaigns`, `archived`, and
`version`. The fixed list reader accepts only that closed item field set and a
bounded standard list envelope (`object`, `data`, `first_id`, `last_id`, and
`has_more`). Any extra or missing required setting field, malformed list
envelope, duplicate setting ID, inconsistent page ID, unsupported nested source
field, or unbounded association list fails closed.

The public record retains only these scalar facts:

| Retained fact | Rule |
| --- | --- |
| Account and setting references | Canonical local refs plus opaque provider IDs; the returned account ID must match discovery. |
| Event type | One of the current documented standard events only. |
| Attribution window | Exactly `30`, the current documented setting value. |
| Status | Local `NOT_ARCHIVED`/`ARCHIVED` normalization of the documented `archived` boolean; it does not assert source health or conversion delivery. |
| Source count | Exactly one, as required by the documented setup contract. Source IDs and source names are discarded. |
| Campaign count | Bounded count only. The docs show a `campaigns` field but do not specify an association element schema, so no campaign identity or linkage is inferred. |
| Version and API provenance | Positive exact integer within the local JavaScript-safe integer bound and fixed provider API version `v1`. |

Display names, source IDs, source names, custom event names, raw campaign
associations, raw payloads, tokens, and the original response are never
returned or persisted. They remain transient only long enough for closed-schema
and sensitive-data screening. A source must appear exactly once in `source_ids`
and exactly once in `sources`, with the same opaque ID; that proves only the
provider response's internal consistency, not pixel health or browser delivery.

## Supported event boundary

OpenAI's current [Supported Events
reference](https://developers.openai.com/ads/supported-events) documents these
standard event names:

```text
app_installed, app_opened, appointment_scheduled, checkout_started,
contents_viewed, items_added, lead_created, order_created, page_viewed,
registration_completed, subscription_created, trial_started
```

The reader rejects `custom`, every unknown event type, and a non-null
`custom_event_name`. This follows the current conversion-optimized campaign
guidance: an optimization goal must be an active standard event setting, and
custom events are excluded from that goal. The reader does not claim that a
standard setting is active in a conversion-delivery sense; it represents only
the documented archived state and response binding.

## Event and person-data exclusion

The same Conversion Setup reference documents `GET /conversions/events`, but
states that it returns recent testing events and shows an `event_data_json`
field. It can contain browser or server-side event information and is not an
inventory or attribution read. This repository never calls that endpoint,
accepts `event_data_json` as no response field, or accepts event timestamps,
action sources, Pixel IDs, or event delivery data. It also never creates a
Pixel, creates a Conversions API key, sends conversion events, or writes an
event setting.

## Bounds and failure behavior

- The fixed HTTPS host is `api.ads.openai.com`; redirects are prohibited by the
  shared transport.
- JSON is limited to 8 MiB; each response page to 500 settings; a complete
  read to ten pages and 5,000 settings.
- The request limit is reduced to the remaining result budget. If the provider
  says more results exist after that budget, the read returns an explicit
  `RESULT_LIMIT_REQUIRED` error rather than a partial inventory.
- Pagination requires forward progress, matches `first_id` and `last_id` to
  the returned page, rejects repeated cursors and duplicate setting IDs, and
  stops after ten pages.
- Provider responses are parsed in memory, screened for prohibited data, and
  not logged or saved as raw JSON. Sanitized errors do not interpolate request
  values, response values, or credentials.

The official list endpoint documents the route and parameters but does not
publish a list-response example. The envelope and closed setting response shape
above are fixture contracts derived from the documented setting response and
the established OpenAI Ads list convention in this repository. This is a
read-only implementation, not live feasibility evidence. A separately
authorized account test must confirm the live schema, current least-privilege
scope, and same-profile discovery composition before this capability can be
promoted or connected to a compiler or execution workflow.

Hermetic tests cover canonical and session binding before egress, response
account mismatch, every documented standard event, custom/unknown rejection,
unknown/event-payload fields, bad IDs/source self-consistency, pagination caps,
and redirects. No provider credentials, customer account, event, or API call
was used for this research or test suite.
