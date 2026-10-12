# Creative history ultra review

## Scope

This independent review covers the closed creative record revalidators and the
ledger's creative-stream reconstruction boundary. It uses temporary local
ledgers, synthetic in-memory asset bytes, and the strict creative factories;
it does not call the image vault, a provider, or a credential store.

## Findings and repair

The first graph implementation checked relationships while appending, but did
not repeat that check after physically reconstructing a creative stream. A
historical valid-shaped `CREATIVE_VERSION` without its preceding family could
therefore pass `Ledger.verify()` and become part of a checkpoint. The ledger
now calls `validate_creative_history_graph()` after creative-stream scanning,
so `records()`, `verify()`, and `checkpoint()` reject the invalid prefix.

`CreativeVersionRecord.serving_manifest_schema_version` was also a separate
`Literal[1]` field, so raw JSON `true` and `1.0` normalized to `1`. It now has
an exact integer pre-validator alongside the inherited `schema_version` guard.

`join_performance_window()` initially trusted typed deployment instances. A
`model_copy()` could change a deployment's creative-version ID while retaining
the prior deployment digest, then produce an `EXACT` association. The helper
now reconstructs every supplied deployment before comparing intervals.

An `UNKNOWN_SOURCE_ASSET` rendition could also claim a final-surface fingerprint
from an unrelated local asset. The graph now requires the fingerprint asset's
content digest and intrinsic dimensions to equal the unknown rendition before
the analyzer evidence can bind. The rendition identity now includes those
intrinsics as well, so they cannot be changed under one rendition ID.

`TEXT_ONLY` versions initially accepted a copied visual rendition or image
analyzer reference. That makes a text-only declaration falsely carry visual
evidence. The version revalidator now rejects both. Conversely,
`PROVIDER_PREVIEW` was accepted for a fingerprint derived entirely from local
analysis even though no typed provider-preview capture evidence exists. The
current model truthfully supports only `SOURCE_ASSET` and `FINAL_RENDITION`.

The late-fingerprint graph check also used the analyzer references from the
last processed version rather than the fingerprint's own version. It now binds
the fingerprint to its referenced version's analyzer evidence, so an unrelated
later version cannot change the result.

M28's persisted fingerprint and deployment shapes now have explicit v2
boundaries. A new fingerprint includes its MIME type in the payload and in the
already-existing fingerprint identity domain. A new deployment includes its
schema version in its content-derived association identity. Historical M27
fingerprints and schema-less deployments remain readable only while scanning a
verified old stream: a v1 fingerprint obtains its omitted MIME solely from its
exact referenced asset-analysis record, and neither legacy shape is rewritten.
Direct revalidation, new ledger append, and performance joins fail with
`CREATIVE_HISTORY_MIGRATION_REQUIRED` until an explicit migration exists.

## Independent coverage

`tests/test_creative_history_ultra_review.py` verifies that:

- raw `true` and `1.0` cannot enter the serving-manifest schema field;
- `model_copy()` changes to the version ID, serving digest, or schema scalar
  fail closed during public revalidation; and
- an old valid-shaped graph-invalid record is rejected by `verify()`,
  `records()`, and checkpoint reconstruction after append-time validation is no
  longer available.
- a copied deployment cannot fabricate an exact performance-to-version
  association.
- an unknown-source rendition cannot attach an unrelated asset analysis as its
  final-surface fingerprint.
- unknown-source rendition IDs differ when their retained intrinsic dimensions
  differ.
- raw or constructed `TEXT_ONLY` versions cannot retain a rendition or an
  image-analyzer reference;
- a local analysis cannot be relabeled as a provider preview; and
- a fingerprint appended after another version still binds only to its own
  version's analyzer evidence.
- v2 fingerprint schema aliases and copied MIME changes cannot bypass its
  content identity;
- M27 fingerprint and deployment payloads can be parsed only by the historical
  scan boundary, while direct use, new append, and performance joins require
  explicit migration; and
- an M27 fingerprint whose identifier was derived with a different MIME cannot
  borrow the parent asset analysis that supplies the omitted v1 MIME.

The focused suite currently contains twenty parameterized cases. Registration
closure, locked append, and workflow receipt behavior are covered separately in
the creative-registration ultra review.
