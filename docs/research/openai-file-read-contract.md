# OpenAI Ads provider-file readback research contract

Research date: 2026-09-04.

## Decision

There is no documented OpenAI **Ads** file-metadata, file-list, or file-content
read operation to implement. This repository must not invent a `GET` route,
reuse the unrelated general OpenAI Files API, or turn a fabricated fixture into
provider proof. Therefore this research adds no `openai_files` adapter, CLI
operation, compiler integration, HTTP fixture, or persistence path.

The existing OpenAI Ads compiler remains an
`OFFLINE_CANDIDATE_NOT_EXECUTABLE`. Its
`requires_account_bound_file_readback` precondition remains deliberately
unmet. A local opaque file reference and local creative evidence can establish
only a local assertion; they cannot establish that an Ads account currently
holds a particular provider file or that its bytes equal a local asset.

## Primary-source result

The current [Ads documentation index](https://developers.openai.com/ads/llms.txt)
describes the Files reference as upload and creative reuse. The current
[Ads Files reference](https://developers.openai.com/ads/api-reference/files)
documents these file-related operations:

| Documented operation | Route | Relevant documented result | Readback value |
| --- | --- | --- | --- |
| Upload remote image or binary file | `POST /upload` | A reusable `file_id` | The response example contains only `file_id`; it is not metadata or a content proof. |
| Upload a custom-audience file | `POST /uploads` | A `file_id` for audience use | Out of scope: this involves customer-list material and is not creative-file verification. |
| Use an existing image in an ad creative | `POST /ads` | Creative accepts `file_id` | This is a mutation and does not retrieve metadata or bytes. |

The Ads documentation does **not** document any of the following:

- `GET` metadata by Ads file ID;
- account-scoped file listing;
- cursor, `limit`, ordering, or idempotent page semantics for provider files;
- file status, MIME type, byte size, intrinsic dimensions, content URL, or
  provider content digest in a read response;
- an Ads route that establishes a returned `file_id` belongs to the discovered
  ad account;
- a supported download/read-content endpoint from which this client could
  calculate an asset hash.

The general [OpenAI Files API](https://developers.openai.com/api/reference/cli/resources/files)
does document `GET /files` and `GET /files/{file_id}`, but it belongs to the
general OpenAI API surface and its object/purpose model. The Ads primary source
does not establish that its host, authentication, IDs, or account ownership are
interchangeable with Ads. Sending an Ads credential or `file_id` there would be
unapproved egress, so it is excluded.

## Proof boundary

The local compiler's `OpenAIAdsFileReferenceEvidence` binds these local facts:

- the compiled account reference;
- an opaque provider `file_id` supplied as local evidence;
- the local asset content digest; and
- an evidence reference in local history.

Those fields are deliberately useful for binding the offline candidate to its
declared local creative chain. They do not prove any provider fact. In
particular, none of the following may be treated as a content match:

- matching file ID text;
- a customer-declared filename, MIME type, byte size, or dimensions;
- a locally recorded upload receipt containing only `file_id`;
- a creative inventory response that happens to repeat the same `file_id`; or
- a local asset digest supplied alongside any of those values.

The current Ads upload response example does not expose a provider hash. It
therefore cannot prove that a provider object has the local asset's exact bytes,
even if a caller declares matching metadata. The repository must retain the
result as `NOT_PROVABLE_FROM_DOCUMENTED_ADS_FILE_READBACK`, rather than
rounding it into a verified or account-bound status.

## Why no adapter or fake HTTP test was added

An adapter needs a reviewed fixed route, closed response schema, and bounded
response behavior before it can send a token. The primary source supplies none
for Ads file reads. A mock `GET /files/{id}`, `GET /ad_account/files`, or
download URL would make an unsupported assumption look tested and could later
be wired to production by mistake. It would violate the repository rule
against stubs and fabricated results.

Likewise, pagination tests cannot be written honestly without a documented
list endpoint and page schema. The absence is an intentional explicit
capability blocker, not a silent fallback. Existing compiler tests continue to
assert the offline candidate boundary; no new provider fixture or customer
probe was used for this research.

## Promotion requirements

A future implementation of metadata/content **readback for a preexisting file**
needs new official Ads documentation, or separately authorized provider
feasibility evidence, establishing the following before its adapter is written:

1. A fixed HTTPS Ads `GET` route for exactly one file ID, or an account-scoped
   fixed list route with documented cursor and limit bounds.
2. A closed response schema with an opaque file ID and an account-bound
   ownership fact. If list-based, the response must include enough parent
   binding to reject cross-account files before it can satisfy a compiler
   prerequisite.
3. A provider cryptographic content digest, or a separately reviewed bounded
   file-content read route. Filename, MIME type, byte size, and dimensions do
   not substitute for a hash.
4. If content is readable, an explicit fixed-host transport contract: redirects
   prohibited, a documented maximum byte count, declared MIME allowlist,
   streamed hashing, no raw-content logging or persistence, and a direct
   SHA-256 comparison against the local asset digest.
5. A composed discovery-session verifier from the existing Ads identity client,
   so a caller-constructed `OpenAIAdsIdentity` cannot trigger a provider-file
   read. The verifier must run before token retrieval and HTTP egress.
6. A separate least-privilege Ads read profile, fixture coverage for malformed
   IDs, unknown fields, pagination limits, identity mismatch, redirects,
   response-size limits, and digest mismatch, followed by authorized live
   feasibility evidence.

The current preexisting-reference compiler must therefore block plan promotion
before every provider mutation. No code, raw provider data, customer records, credentials,
or material from the maintainer's separate workspace was copied. Earlier
clean-room provenance remains context only in
[openai-clean-room-reference.md](openai-clean-room-reference.md); current
official Ads documentation governs this conclusion.

## Separate future path: guarded upload association

The missing GET contract must not be turned into an invented requirement that
every new upload return byte-for-byte storage proof. Independent ultra review
identified a different, narrower association claim that a future implementation
could support using separately reviewed write and paused-ad read contracts:

1. Hash the exact local asset bytes and durably record the intended upload under
   its verified account session before mutation egress.
2. Send that upload only through an exact authorized, bounded write operation.
   Bind the acknowledged returned file ID to this particular request and account.
3. Create a paused ad through its own guarded parent/result bindings, then verify
   its documented creative readback under the same account references that file.

Together those observations can support `PROVEN_UPLOAD_ASSOCIATION`. They do
not support provider-storage or delivered-rendition byte equality. Preserve
`PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ` separately, and never mark the
rendition equal to the local asset from a repeated ID. Shared evidence retains
local hashes and redacted/HMAC receipt references, not raw provider payloads or
cleartext file IDs.

This is an implementation design boundary, not shipped upload functionality or
live feasibility evidence. Fixed upload/paused-ad contracts, limits, durable
request provenance, ambiguous-upload reconciliation, parent binding, direct or
delegated authorization, drift rejection, and live evidence still need their
own implementation and review. No current candidate is promoted by this note.
