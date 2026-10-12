# Egress map

Last updated: 2026-09-04

## Default

No command has ambient network permission. A transport receives an allowlist
compiled from the installed capability, selected profile, exact plan, and
current authorization. DNS redirects, HTTP redirects, upload/download hosts,
browser subresources, and update endpoints are separately checked. Raw request
or response bodies are never evidence.

| Destination class | Initiator and allowed purpose | Allowed data | Required gate | Evidence retained |
| --- | --- | --- | --- | --- |
| Google Ads API | Google adapter read/write | approved fields, operational IDs, OAuth bearer in transport only | capability, profile, identity; exact authorization for writes | endpoint class, API version, status, request/result digests |
| Meta Graph/Marketing API | Meta adapter read/write/media | same; customer creative only for approved upload | same | same |
| LinkedIn Marketing API/media upload | LinkedIn adapter read/write/media | same | same plus app/account/page role | same plus `Linkedin-Version` |
| LinkedIn OAuth 2.0 authorization/token endpoints | LinkedIn local enrollment and refresh | authorization-code grant fields; client secret and tokens in transport only, never in plans, logs, or evidence | explicit local browser handoff and a configured LinkedIn Ads profile | endpoint class, grant type, status; no token or secret material |
| TikTok API for Business production or sandbox | TikTok adapter read/write/media | same | environment pinned in profile; writes authorized | same plus API environment |
| TikTok API for Business auth-code exchange endpoint | TikTok local enrollment only | app ID, app secret and the one-hour single-use authorization code in a fixed JSON body, in transport only, never in plans, logs, or evidence | explicitly declared API environment, an explicit local `--interactive` handoff, and a configured TikTok Ads profile | endpoint class, API environment, status; no token, secret, authorization code, or response text |
| Reddit OAuth 2.0 authorization/token endpoints | Reddit local enrollment and refresh | authorization-code and refresh grant fields; client ID as the HTTP Basic user name and application secret as its password, and tokens, in transport only, never in plans, logs, or evidence | explicit local browser handoff and a configured Reddit Ads profile | endpoint class, grant type, status; no token material |
| Reddit Ads API | Reddit adapter read/write/media | same | OAuth scope and account identity; writes authorized | same |
| OpenAI Ads API | OpenAI Ads adapter read/write/media | same | account-scoped API key handle and feature access; writes authorized | same |
| GA4 Data API | read-only reporting profile | allowlisted aggregate query fields and property ID | read capability and property identity | query contract digest, quota/quality metadata |
| GA4 Admin API | read/edit profile | allowlisted key-event or event-rule fields | separate edit profile and exact authorization | resource before/after digests and API maturity |
| GTM API v2 | inventory, dedicated staging, version/publish/restore | allowlisted config; custom code never leaves local inspection | distinct read/edit/version/publish profile and operation authorization | graph/version/digest/status metadata |
| Provider creative CDN | explicit asset retrieval for local analysis | signed request mechanics in memory; approved advertising media | provider profile, asset ref, retention choice | content digest and sanitized provenance, never signed URL |
| Approved public validation site | browser navigation on declared nonsensitive path | no entered values or scraped text | browser plan, host/path and consent contract | host/path class, no URL values |
| Approved measurement endpoints | denied/granted browser test | declared low-cardinality fields and provider cookies only when consent permits | exact endpoint/payload-name allowlist | domain, field names, occurrence counts |
| Optional signed update manifest | user-enabled check only | tool/platform/current version and ordinary network metadata | saved opt-in; opt-out always available | manifest/signature result |
| Timestamp/notarization/signing services | release process only | artifact digest or platform-required signed payload | accepted signing ADR and release authorization | service, public identity, artifact digest, result |
| Customer-selected agent/model | outside automatic CLI egress; agent may forward returned output | only bounded redacted agent-visible result | customer-selected host and sharing policy | disclosure and local output digest where configured |

Exact hosts and paths are versioned capability data, not hard-coded in this
document. A capability update that adds a domain requires review, tests, and a
new manifest digest. Provider redirects to undeclared hosts fail closed.

## Explicitly absent

There is no vendor proxy, telemetry, crash upload, hosted database, remote
license check, automatic updater, document SaaS upload, generic webhook, shell,
SQL/SOQL, browser, external CLI, or MCP passthrough. Optional user-managed tools
are not installed, authenticated, upgraded, or invoked automatically.

## Browser and conversion limits

Browser verification uses synthetic events on declared public marketing routes.
It never submits a form or reads entered values/DOM text into the product.
Denied consent must emit no disallowed measurement request. Native conversion
setup is limited to reviewed tags/templates and safe event contracts; no
person-level, offline-upload, enhanced-identifier, or server relay fallback is
allowed.

The authoritative data rules are
[`measurement-control.md`](measurement-control.md) and
[`conversion-integrations.md`](conversion-integrations.md).
