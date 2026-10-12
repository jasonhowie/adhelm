# Creative identity and history integrity

The creative history boundary records local creative facts as a closed graph.
It does not create a provider creative, prove a local asset remains in a vault,
prove a deployment is current, or establish performance below an ad-level
association.

## Closed identities and raw input

`adhelm.creative` revalidates each creative payload from its original
JSON-shaped form before it reaches the ledger.  Schema versions and secondary
serving-manifest versions must be their exact documented integer, rather than
`true` or an integer-looking float.  Creation and association timestamps must
be aware UTC values.  Asset, analysis, rendition, version, fingerprint, and deployment IDs
are recomputed from their exact intrinsic facts; a record cannot relabel an
existing identity with different content, dimensions, transformation, serving
manifest, or deployment association.

For a source-proven rendition, the retained asset, content digest, dimensions,
and identity transformation must agree.  A source-unknown rendition does not
claim an asset parent.  Its identity includes its retained content digest and
dimensions so two incompatible intrinsic descriptions cannot share an ID.  A
final fingerprint for such a rendition still needs matching local asset content
and dimensions; this is local relationship evidence only, not an assertion
that a provider supplied the asset.  `PROVIDER_PREVIEW` is not retained by this
foundation: it needs a separate typed provider-capture evidence contract.

### M27 payload compatibility

M27 asset, rendition, asset-analysis, and version payloads retain their v1
field shapes and the identity formulas produced by their existing factories.
The newer raw checks reject malformed values; they do not reinterpret a valid
v1 record or rewrite its payload.

Two persisted surfaces need an explicit boundary:

- `ImageFingerprintRecord` v2 adds the required `mime_type` that was already
  part of its fingerprint digest. A physical v1 record is read only through
  the creative graph, where its exact referenced asset-analysis supplies that
  missing MIME value and its original fingerprint ID is recomputed. Its raw
  payload and envelope digest remain unchanged. It cannot be appended as a new
  record or used through the v2 standalone revalidator. A legacy
  `PROVIDER_PREVIEW` fingerprint requires migration because v1 had no typed
  capture evidence.
- `CreativeDeployment` v2 adds `schema_version: 2` and includes it in the new
  content-derived deployment-ID domain. A schema-less M27 deployment is
  readable as an explicit legacy shape for history/graph reconstruction, with
  its stored bytes intact, but cannot be appended again or used for an exact
  current performance join. It needs an explicit migration to receive a v2
  identity.

New writes reject either legacy shape with
`CREATIVE_HISTORY_MIGRATION_REQUIRED`; the runtime never silently upgrades or
rehashes old JSONL records.

Creative-version identity intentionally excludes later annotations such as a
message, analyzer references, provenance, and creation timestamp.  A retained
duplicate version may therefore enrich those annotations, but its family,
serving manifest, rendition order, destination, component facts, and serving
digest cannot change.  A `TEXT_ONLY` manifest has no rendition or image-analyzer
evidence; a media version requires its declared rendition graph.

## Graph and fixed registration closure

`validate_creative_history_graph` runs when a creative record is appended and
again whenever the creative stream is physically scanned.  It requires exact
asset-analysis links, version/family/rendition/analyzer relationships,
fingerprint links, and deployment family/version/ordered-rendition membership.
Physical JSONL rehashing cannot bypass that graph check during `Ledger.verify`,
`Ledger.records`, or a checkpoint.

`adhelm.creative_registration_closure` has two closed construction paths:

- `build_creative_registration_closure` consumes already-retained local image
  asset and analysis evidence, then derives a source-proven rendition, serving
  version, and final fingerprint.
- `build_text_creative_registration_closure` derives a family-plus-text-only
  serving version without inventing media evidence.

Both builders accept distinct family and version timestamps.  Reusing a family
requires its exact original label, provenance, and timestamp; a later material
version retains its own declared evidence time.  Neither builder makes a
provider, deployment, availability, permission, or current-state claim.

`Ledger.append_creative_registration` is the sole multi-record registration
operation.  Under one creative-stream lock it validates the exact durable
source asset and analysis records, checks the complete proposed graph, and
appends only the fixed dependency order.  Existing identical records support a
recoverable staging continuation; conflicting identities fail before a child
write.  After the first child append begins, any later local write, scan, lock,
or checkpoint failure is reported as
`CREATIVE_REGISTRATION_UNVERIFIED` (exit 6).  That result deliberately makes
no rollback claim and requires inspection before a deliberate retry.

## Performance associations

`join_performance_window` reparses supplied deployments before it uses them.
An `EXACT` association needs one deployment for the same provider, account,
ad, and full observation-window containment.  A partial overlap is not exact.
At ledger retention, each supplied performance creative family, version,
rendition, and deployment reference must agree with the selected creative
high-watermark and the deployment's provider/account/ad association.  The
association remains ad-level evidence; it does not attribute a component,
message, palette, or analyzer finding.

## Remaining limits

`AVAILABLE` describes the retained local observation at its recorded time; it
does not prove the current vault file still exists.  A creative deployment is a
historical association, not a current provider read or an activation claim.
The foundation has no provider creative upload, serving verification, browser
rendering, native provider identifier proof, component performance inference,
or full creative-intelligence coverage.  Those require their separate
configured reads, authorization, guarded mutation, and verification paths.

## Focused verification

The creative, ledger, closure, projection, strict-input, and provider compiler
fixtures exercise raw scalar rejection, content-derived identities,
source/analysis/rendition/fingerprint graph binding, staged closure recovery,
post-append ambiguity, physical history revalidation, scoped performance joins,
and legacy compiler use of valid derived asset identities.

The focused M28 validation was:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_assets.py tests/test_creative.py tests/test_creative_registration.py \
  tests/test_creative_registration_closure.py tests/test_creative_history_ultra_review.py \
  tests/test_ledger.py tests/test_projections.py tests/test_creative_projection_ultra_review.py \
  tests/test_strict_json_inputs_ultra_review.py tests/test_openai_compiler.py \
  tests/test_openai_compiler_ultra_review.py tests/test_meta_compiler.py \
  tests/test_meta_compiler_ultra_review.py tests/test_linkedin_compiler.py \
  tests/test_reddit_compiler.py
```

It passed 129 tests. The source checks were Ruff format and lint, `mypy` on
`creative.py`, `creative_registration_closure.py`, and `ledger.py`, plus
`git diff --check`.
