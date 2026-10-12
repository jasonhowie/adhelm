# Uniform creative intelligence

Last updated: 2026-09-04

## Product outcome

The CLI can compare ad performance against the exact creative that served:
visual asset, color palette, platform copy, on-visual text, CTA, message, format,
and execution characteristics. The representation is uniform across providers
without claiming that unlike formats or unavailable provider metrics are equal.

Creative intelligence is local and deterministic by default. It must not require
a hosted vision/LLM service or vendor-operated media upload. An optional BYO
model may enrich explicit fields only after showing the media/text and endpoint
that would leave the machine; its output is provenance-labeled and never
silently substitutes for observed facts.

## Identity model

Never use a provider ad ID or mutable creative ID as the sole identity. Use four
stable layers:

1. `creative_family_id`: the marketer's cross-platform concept, such as one
   message/hook test.
2. `creative_version_id`: digest of the complete canonical creative manifest,
   including copy, CTA, destination contract, component order/pinning, and exact
   rendition references. Any material change creates a new ID even if a provider
   keeps the same ad/creative ID.
3. `asset_version_id`: content-addressed identity of one source media binary and
   its normalized intrinsic metadata.
4. `rendition_id`: an exact crop/aspect/locale/format/provider rendition of an
   asset version, with its own binary digest and transformation metadata.

Provider references are bindings to these product identities, not the identity
itself. A creative cannot be deployed by the CLI until its manifest validates
and every required binding can be written to the local creative JSONL stream.

## `CreativeAssetVersion`

```text
schema_version
asset_version_id
content_digest
media_type
mime_type
byte_size
width_px
height_px
aspect_ratio
duration_ms
frame_rate
locale
source_provenance
source_ref
authored_at
captured_at
availability_status
```

`source_provenance` is `CUSTOMER_LOCAL`, `PROVIDER_READ`, `GENERATED_LOCAL`,
`HUMAN_ANNOTATED`, or `IMPORTED_MANIFEST`. Provider-hosted assets are streamed
to local analysis through the configured provider/CDN boundary. The customer
chooses whether fetched bytes enter a content-addressed local creative vault or
are deleted after feature extraction; the digest, manifest, and features remain.
No asset is uploaded to the product vendor.

`availability_status` distinguishes `AVAILABLE`, `TRANSIENTLY_FETCHED`,
`NOT_AVAILABLE_FROM_PROVIDER`, `MISSING_LOCAL_ASSET`, `UNSUPPORTED_MEDIA`, and
`EXTRACTION_FAILED`. Missing data is never a silent null.

## `CreativeRendition`

```text
schema_version
rendition_id
source_asset_version_ref
source_relation_status
content_digest
rendition_role
provider_format
media_type
mime_type
byte_size
width_px
height_px
aspect_ratio
duration_ms
frame_rate
locale
transformation_spec
transformation_digest
provenance
captured_at
availability_status
```

Even when a source is used byte-for-byte, the creative manifest references an
explicit rendition whose digest may equal the source content digest. A crop,
resize, transcode, overlay, locale change, or provider-rendered output creates a
new `rendition_id`. When a provider exposes only final media and its parent
source cannot be proven, `source_asset_version_ref` is null and
`source_relation_status` is `UNKNOWN_SOURCE_ASSET`; the final rendition remains
independently content-addressed.

## `AdCreativeVersion`

```text
schema_version
creative_version_id
creative_family_id
rendition_refs[]
provider_format
locale
destination_ref
message
components[]
visual_fingerprint_refs[]
experiment_ref
serving_manifest_schema_version
serving_manifest_digest
created_at
provenance
```

`creative_version_id` is `creative:sha256:<serving_manifest_digest>`. The hash
domain is a canonical semantic `ServingCreativeManifest` containing provider
format, locale, destination contract, ordered rendition references, and exact
serving components including copy, CTA, slots, order, and pinning. It excludes
the ID/digest fields themselves, `creative_family_id`, message annotations,
fingerprints, experiment links, provenance, analyzer results, and timestamps;
changing those observations does not pretend the served creative changed. The
common JSONL `record_digest` separately protects the complete stored record and
its provenance.

The format-independent `message` uses closed vocabularies where cross-platform
grouping is useful and free text where the operator reads it:

```text
hook                 # accusation | diagnosis | proof | curiosity | offer |
                     # social_proof | demonstration | comparison | other
posture              # operator | creative | customer | expert | product | other
objection            # price | complexity | existing_system | trust |
                     # switching_cost | urgency | none | other
specificity          # named_number | concrete_example | general_claim
offer                # free_trial | free_signup | demo | assessment | content |
                     # contact_sales | learn_more | none | other
claim
mechanism
audience_frame
tone[]
```

Enum version is part of the schema. Unknown values map to `other` with a
provider-specific raw value; they are never silently coerced.

## Copy and CTA components

Every exact text or CTA unit is a `CreativeComponent`:

```text
creative_version_id
component_id
component_role
ordinal
exact_text
normalized_text
language
provider_slot
pinned_position
start_ms
end_ms
normalized_cta_intent
cta_destination_ref
provenance
confidence_basis_points
extraction_status
```

`component_role` includes:

- `PRIMARY_TEXT`
- `HEADLINE`
- `LONG_HEADLINE`
- `DESCRIPTION`
- `ON_VISUAL_TEXT`
- `CAPTION`
- `VOICEOVER`
- `CTA_BUTTON`
- `CTA_VISUAL`
- `CTA_SPOKEN`
- `PATH_TEXT`

CTA stores both the exact label and a normalized intent such as `SIGN_UP`,
`BOOK_DEMO`, `CONTACT_SALES`, `LEARN_MORE`, `DOWNLOAD`, `INSTALL`, `BUY`, or
`OTHER`. A CTA can exist in platform chrome, on the asset, in speech, or in more
than one place; do not collapse them.

Exact company marketing copy may be kept locally after the sensitive-data
classifier passes. It is excluded from shared evidence by default and escaped
against spreadsheet formula injection in CSV. OCR/transcript text is treated as
untrusted before persistence.

## Visual fingerprint

`CreativeFingerprint` is a versioned description of the exact analyzed surface:

```text
fingerprint_id
creative_version_id
asset_version_id
rendition_id
fingerprint_schema_version
analysis_surface
analyzer_name
analyzer_version
sample_strategy
analyzed_at
palette[]
background_color
foreground_color
accent_color
cta_colors[]
mean_lightness
mean_saturation
contrast_score
colorfulness_score
text_area_basis_points
safe_area_coverage_basis_points
composition
visual_style[]
subject_presence[]
face_presence
product_ui_presence
logo_presence
captions_presence
opens_on
scene_count
motion_intensity
audio_type
voice_type
claim_lands_ms
standing_cta_from_ms
extraction_statuses[]
review_status
```

Palette entries use sRGB `#RRGGBB`, optional CIELAB coordinates, proportion in
basis points, and semantic role when proven. All numeric extraction methods,
video frame timestamps, quantization parameters, and threshold versions are
fixed by `analyzer_version`; results from different analyzer versions are not
silently compared.

`composition`, `visual_style`, and presence fields use versioned closed
vocabularies. `face_presence` is a boolean/aggregate presence result, never face
identity or biometric recognition. No analyzer infers protected traits,
diagnoses health/financial state, or identifies a person.

`analysis_surface` is `SOURCE_ASSET`, `FINAL_RENDITION`, or `PROVIDER_PREVIEW`.
Prefer the final rendered/provider surface for performance analysis. A source
asset fingerprint cannot silently stand in for a crop with different text or
CTA. `SOURCE_ASSET` requires `asset_version_id` and no `rendition_id`; final or
preview analysis requires `rendition_id` and retains the known parent asset
reference when available.

## Deterministic local analysis

Core v1 analysis runs locally and supports:

- images: content hash, dimensions/aspect, palette/color proportions,
  brightness/saturation/contrast/colorfulness, text-area estimate, local OCR,
  logo/UI/presence fields when supported, and safe-area/composition metadata;
- video: the image features above on a deterministic frame sample plus duration,
  frame rate, opening frame/type, scene/motion measures, caption/on-screen text
  timing, optional local speech transcript, audio/voice category, claim timing,
  and CTA timing; and
- text/search/chat formats: exact provider components, slot/order/pinning,
  normalized message/CTA fields, and `NOT_APPLICABLE` visual values.

Automatic extraction never overwrites a declared or human-reviewed value.
Conflicts are retained as separate observations with provenance and confidence.
Manual corrections append a new creative record and supersession relation;
they do not rewrite history.

If a packaged local extractor cannot support a field, report
`NOT_SUPPORTED_BY_ANALYZER`. If the provider does not expose the relevant asset
or component, report `NOT_AVAILABLE_FROM_PROVIDER`. A required feature is never
fabricated.

## Creative experiments

Creative comparisons are trustworthy only when the product knows what changed.
An optional `CreativeExperiment` records:

```text
experiment_id
hypothesis
comparison_group
variant_id
changed_dimensions[]
held_constant_dimensions[]
primary_metric
minimum_observation_contract
started_at
ended_at
confounders[]
```

Examples of dimensions include `HOOK`, `HEADLINE`, `PRIMARY_TEXT`, `CTA_INTENT`,
`PALETTE`, `BACKGROUND`, `FACE_PRESENCE`, `PRODUCT_UI`, `DURATION`, `OPENING`,
and `PLACEMENT`. The CLI validates declared held-constant fields against the
actual creative manifests and reports discrepancies before launch.

Cross-platform creative comparisons are directional because delivery systems,
formats, audiences, and attribution differ. The report exposes those
confounders and never calls a color, word, or CTA causal without an appropriate
experiment.

## Effective-dated deployment

An ad-to-creative association is a `CreativeDeployment`:

```text
deployment_id
provider
account_ref
campaign_ref
ad_group_ref
ad_ref
provider_creative_ref
creative_family_id
creative_version_id
rendition_refs[]
effective_from
effective_to
observed_from
observed_to
association_status
source
evidence_refs[]
```

There is one deployment per ad-to-creative-version association interval, not
one deployment per rendition. `rendition_refs[]` is the exact set bound through
that creative version (and must agree with its serving manifest). Creating one
row per image/card and copying the same ad-level metrics to each would duplicate
performance and is prohibited.

Any asset, platform copy, CTA, destination, component order/pinning, or provider
rendering transformation creates a new `creative_version_id`, even if the
provider retains the same ad ID. A pure ad-to-creative association change keeps
the existing creative version and closes/opens `CreativeDeployment` intervals.
If the exact effective time is unknown, use the observation interval; do not
invent it.

Performance joins use the deployment interval valid for each metric window. A
window crossing more than one version is split when daily/provider granularity
allows it; otherwise it is labeled `MIXED_CREATIVE_VERSION` and excluded from
single-version conclusions.

## Dynamic and responsive formats

Responsive search ads, dynamic creative, automated combinations, and provider
optimization—as well as multi-image/carousel formats—can expose a pool rather
than one served composition. Record:

- the complete eligible component pool and its creative version;
- asset/component IDs, slot, order/pinning, eligibility, and change history;
- provider-reported combination or component exposure/performance only when
  actually available; and
- `CREATIVE_NOT_EXPOSED` or `COMBINATION_UNKNOWN` when it is not.

Do not attribute an ad-level conversion to a particular RSA headline, color,
CTA, or dynamic combination without provider-reported exposure at that level.
Ad-level metrics remain one ad-level row with no served rendition ID; they are
never cloned across every eligible rendition. Provider-exposed rendition/card
metrics retain the provider's exact metric grain and additive/non-additive
semantics.

## Uniform reporting

Creative reporting joins the normalized performance stream to exact deployment,
version, component, and fingerprint records. At minimum it can group or filter
by:

- provider, campaign/ad group/ad, placement, format, locale, and rendition;
- creative family/version and experiment/variant;
- hook, posture, objection, specificity, offer, claim/mechanism category;
- exact or normalized headline/body/description/CTA component;
- palette/dominant/background/accent/CTA color;
- aspect, duration, opening, motion, captions, voice/audio, face/UI/logo
  presence, text coverage, claim timing, and CTA timing; and
- spend, impressions, clicks, CTR, video/engagement metrics, provider results,
  GA4 key events, and explicit data-quality/join statuses.

The command can compare concepts or features only when the necessary data and
exposure granularity exist. Otherwise it returns a clear insufficiency status
instead of a ranked winner.

## Privacy and evidence

- Analyze only customer-owned or provider-returned advertising creative.
- Scan exact text, OCR, transcripts, destinations, and metadata for prohibited
  personal/sensitive data before ledger append or export.
- Do not perform face recognition, demographic/protected-trait inference, or
  health/financial inference from people or scenes.
- Keep source media, extracted text, and exact copy local. Shared evidence uses
  digests and normalized descriptors unless the user explicitly previews and
  includes marketing content.
- Never include authorization-bearing asset URLs, cookies, request headers, or
  raw provider responses in manifests, JSONL, CSV, logs, or evidence.
- Label every field `DECLARED`, `PROVIDER_READ`, `LOCAL_EXTRACTED`,
  `HUMAN_ANNOTATED`, or `MODEL_INFERRED`, with analyzer/model version and
  confidence where relevant.

The canonical records and CSV projections follow
[`local-ledger-and-exports.md`](local-ledger-and-exports.md).
