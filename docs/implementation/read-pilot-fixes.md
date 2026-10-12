# Read-pilot fixes for 0.1.1

This patch addresses issues #2 through #7. Verification is offline; the issue
reports are observations supplied by the owner, not new live-pilot evidence.
Remote writes remain blocked by the unchanged release-policy gate. Provider
labels remain `EXPERIMENTAL` until a valid pilot record ships.

## Google Ads v25

Inventory uses `campaign.start_date_time` and `campaign.end_date_time`, parsed
strictly in the account timezone. Sub-day precision reaches the normalized UTC
snapshot. Ambiguous/nonexistent local times fail explicitly. Date-only local
model fields remain available for existing callers. The state-shape marker is
`2026-10-12.1`; old snapshots are not rewritten. A schema-shape change establishes
a new schedule baseline without inventing an external edit from a precision
change; comparable budget and status changes are still recorded. Creation readback selects the
repeated `campaign.asset_automation_settings` message, not nonselectable child
paths. All write execution remains unreleased.

`tests/fixtures/google_ads_v25_fields.json` is a bounded extract from the official
v25 field-reference pages, fetched on 2026-10-12. Each source has its URL and
SHA-256 digest; no account metadata or provider response is included. The
regression test checks field literals across every Google Ads implementation
module against that independently published field set. This verifies field
existence/selectability, not every GAQL field combination or live behavior.

Reports request the primary and all-conversion count/value pairs without
conversion-action segmentation. Primary and all totals overlap. Conversion row v2 counts retain
an exact canonical decimal string; legacy integer counts are supplied only
when integral. Native values remain in reviewed extensions with the rounding
policy for the USD minor-unit projection. Attribution and conversion-action
configuration are still unavailable, and the audit cannot infer native signal
health from these aggregates. Missing metrics do not become zero. Both v1 and v2 remain readable in 0.1.1;
an explicit upcaster produces a v2 view without rewriting v1 history. Readers
on 0.1.0 must upgrade to read v2 rows or consume the CSV projection.

Campaign names are requested only with `include_campaign_names: true` in the
Google report input. This is an explicit one-read disclosure to the chosen
agent. The same response binds name, ID and observation time; no name-only
join occurs. The names are retained locally, screened, and pseudonymized in
default output and exports, including marketing-text exports. The retained
report view requires `--include-campaign-names` to display them again. An old
report without labels needs a new read.

## Diagnostics and enrollment

HTTP diagnostics retain a numeric status and bounded provider code/request ID.
Google Ads codes use a closed reviewed vocabulary. Meta and LinkedIn numeric
codes and TikTok business codes are not interpreted beyond their documented
shape. Unknown codes/envelopes keep the generic failure. Provider prose, raw
response bodies and arbitrary headers are never exposed. Error parsing is
bounded to 16 KiB. Cloud-project access classification also retains the existing
64 KiB bounded scan; both parsers share one read of the response stream. This
does not change the original status, retry policy or ambiguous-write outcome. Error classification retains these structured facts.

Interactive Google, LinkedIn and Reddit authorization writes the validated
URL and wait/cancel instructions only to a local terminal on stderr. It never
prints the PKCE verifier, returned code, tokens or client secret. `--no-browser`
skips launching a browser, not the local callback or its security checks.
Noninteractive enrollment still fails before the handoff. Cancellation closes
the listener through the existing context manager. The CLI does not log or
persist the displayed URL.

## Maintenance release

The package and README-link target are 0.1.1. `RELEASED_0_1_0` continues to name
the published read-only milestone, separately from the package version; it is
not a claim that 0.1.1 has been tagged or published. The artifact checker
inspects wheel and sdist constants without executing them, and the installed
smoke checks actual version output. Publishing requires the owner's release
tag and existing protected workflow.

## Primary references

- [v25 campaign fields](https://developers.google.com/google-ads/api/fields/v25/campaign)
- [v25 metrics](https://developers.google.com/google-ads/api/reference/rpc/v25/Metrics)
- [Google Ads conversion reporting](https://developers.google.com/google-ads/api/docs/conversions/reporting)
- [Google Ads structured errors](https://developers.google.com/google-ads/api/docs/best-practices/understand-api-errors)
- [Google developer-token sunset](https://developers.google.com/google-ads/api/docs/api-policy/developer-token)

Other provider error vocabulary remains constrained by the corresponding
[provider research contracts](../provider-contracts.md). No new account,
permission, API write or external service is introduced.
