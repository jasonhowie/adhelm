# Local creative extraction

Reviewed: 2026-09-08
Status: **implemented local extractor; no model, OCR, transcription, or frame decode**

`src/adhelm/creative_extraction.py` adds deterministic local extraction beside the
existing creative identity and history boundary. It extracts only facts the
supplied bytes themselves prove. Everything the packaged runtime cannot observe
is an explicit status, never an inference, a default, or a silent null.

The module runs entirely offline. It opens no network connection, invokes no
subprocess, loads no model, reads no credential, and contacts no provider. Its
only dependency is the already-pinned Pillow decoder that
[`creative-registration.md`](creative-registration.md) already uses.

## Image structure

`extract_image_features(data)` returns an `ImageStructureFeatures` record.
`extract_vault_image_features(root, digest)` is the same extraction over bytes
read back through the existing private vault reader described in
[`creative-asset-vault.md`](creative-asset-vault.md).

The bytes are decoded exactly once. `adhelm.creative.decode_image` performs the
single bounded decode and `analysis_from_decoded_image` derives the shared
analysis fields, so the MIME type, dimensions, reduced aspect ratio, quantized
palette, lightness, saturation, contrast, and colorfulness are byte-identical
with `analyze_image`, which is now that same pair of calls.
`to_image_analysis()` reconstructs that exact `ImageAnalysis`, and a regression
asserts the canonical bytes are equal. The layout grid then walks the already
materialized pixels with a two-row rolling luminance buffer, so the extractor
costs one decode plus one linear pass and no second pixel materialization.

It adds:

- `aspect_ratio_class`: a closed `PORTRAIT_TALL` / `PORTRAIT` / `SQUARE` /
  `LANDSCAPE` / `LANDSCAPE_WIDE` bucket derived from integer thresholds on the
  exact pixel ratio.
- `layout_cells`: a fixed 4x4 row-major grid. Each cell carries only aggregate
  integers: mean luminance, luminance standard deviation, and edge density in
  basis points. Cell boundaries are integer floors of the exact dimensions, and
  every measure is computed with integer arithmetic (Rec. 709 weights scaled by
  10,000 and `math.isqrt`), so results do not depend on platform floating-point
  behaviour. The grid lets two creatives be compared for where their bright,
  busy, or empty regions sit — for example whether a text block or CTA moved
  from the bottom-left to the top-right — without reading any text.
- `text_presence`: `TEXT_LIKELY`, `TEXT_UNLIKELY`, or `NOT_EVALUATED`, with the
  heuristic score and `text_heuristic_version`. Version 1 takes the maximum over
  cells of `min(edge_density, luminance_stddev)` and compares it with a fixed
  threshold. High-frequency luminance transitions inside a locally
  high-variance region read as `TEXT_LIKELY`; a single hard boundary between two
  flat colour blocks does not. This is a coarse structural signal, not proof
  that text exists, and it never produces characters, words, or a language.
- `exif_orientation` and `exif_orientation_status` (`EXTRACTED`, `NOT_PRESENT`,
  or `INVALID` when the file states a value outside the defined 1-8 range),
  with `layout_measured_in: STORED_PIXEL_ORIENTATION`. The grid
  is spatial, so the orientation it was measured in is disclosed. The tag is
  never applied: rotating pixels would make the result depend on metadata rather
  than on the stored bytes, and two files with identical pixels would stop
  producing identical measurements.
- `parameters_digest`: the digest of every threshold, grid parameter, working
  budget, and cut point that can change a value, including the four
  aspect-class thresholds and the structure budget. Results from different
  `extractor_version` or `parameters_digest` values are not comparable and must
  not be silently mixed.
- `image_features_id`: `image-features:sha256:<digest of every other field>`, so
  a record cannot be relabelled with different content.

The layout has three states. An image narrower or shorter than the grid minimum
reports `NOT_EVALUATED_IMAGE_TOO_SMALL`; an image above `MAX_STRUCTURE_PIXELS`
(4,000,000) reports `NOT_EVALUATED_ABOVE_STRUCTURE_BUDGET` so one very large
creative cannot buy an unbounded second pass; both keep the shared analysis
fields and report `text_presence: NOT_EVALUATED` with no cells.

## Attaching structure to a retained analysis

`AssetStructureRecord.from_asset_analysis(analysis, features)` binds features to
an existing `AssetAnalysisRecord`. It is deliberately a **sibling** record, not
a new field on `AssetAnalysisRecord`:

- `analyze_image` output, the retained analysis payload, its content-derived
  identity, and the closed creative history graph in
  [`creative-history-integrity.md`](creative-history-integrity.md) all stay
  byte-identical for existing callers and existing JSONL records;
- no schema upcast or migration is required for retained history; and
- the structure record can evolve its own `extractor_version` without touching
  the analyzer identity domain that creative versions already depend on.

The binding requires the exact same content digest, MIME type, dimensions,
palette, and colour metrics; a mismatch is `ASSET_STRUCTURE_MISMATCH` (exit 2).
The sibling record is not yet a ledger record type: appending it to canonical
creative history needs its own schema-versioned stream entry and graph rule.

## Video containers

`extract_video_metadata(data)` parses container metadata only. It decodes no
frame, reads no sample payload, and produces no picture, palette, or audio
measurement.

Supported containers:

- **ISO base media (MP4/MOV)**: `ftyp` brand, `moov`/`mvhd` timescale and
  duration, `trak`/`tkhd` display dimensions, `mdia`/`mdhd` media timescale,
  `hdlr` track kind, `stbl`/`stsd` coded dimensions, and `stbl`/`stts` sample
  counts and deltas. 32-bit, 64-bit `largesize`, and to-end-of-file box sizes
  are all handled.
- **Matroska/WebM**: the EBML header `DocType`, `Segment/Info` `TimestampScale`
  and `Duration`, and `Tracks/TrackEntry` `TrackType`, `DefaultDuration`, and
  `Video` `PixelWidth`/`PixelHeight`.

Reported facts and their statuses:

| Field | Statuses |
| --- | --- |
| `timescale`, `timescale_unit` | `EXTRACTED`, `DEFAULTED_BY_SPECIFICATION`, `NOT_DOCUMENTED_IN_CONTAINER` |
| `duration_ms` | `EXTRACTED`, `NOT_DOCUMENTED_IN_CONTAINER` |
| `video_track_status` | `EXTRACTED`, `NOT_PRESENT` |
| `video_track_selection_status` | `SINGLE_VIDEO_TRACK`, `FIRST_DECLARED_OF_MULTIPLE`, `NOT_APPLICABLE_NO_VIDEO_TRACK` |
| `video_width_px`, `video_height_px` | `EXTRACTED`, `NOT_DOCUMENTED_IN_CONTAINER`, `NOT_APPLICABLE_NO_VIDEO_TRACK` |
| `frame_rate_milli` | `EXTRACTED`, `VARIABLE_NOT_CONSTANT`, `NOT_DOCUMENTED_IN_CONTAINER`, `NOT_APPLICABLE_NO_VIDEO_TRACK` |
| `frame_count` | `EXTRACTED`, `NOT_DERIVABLE_FROM_CONTAINER`, `NOT_APPLICABLE_NO_VIDEO_TRACK` |
| `audio_track_status` | `DECLARED_PRESENT`, `DECLARED_ABSENT` |

A Matroska `Duration` of zero, including the zero a zero-width float encodes, is
`NOT_DOCUMENTED_IN_CONTAINER`, exactly as a zero `mvhd` duration is. The format
requires a positive duration, so a zero is an absent one and never an extracted
zero-length movie.

A Matroska file that omits `TimestampScale` takes the value the format defines.
That is reported as `DEFAULTED_BY_SPECIFICATION`, never as `EXTRACTED`: the
number is real but the container never stated it, and
`_MATROSKA_DEFAULT_TIMESTAMP_SCALE` is part of `parameters_digest` because
changing it would change every duration derived from such a file.
`video_track_count` and `audio_track_count` are always reported. When a
container declares more than one video track the extractor reads the first
declared one and says so through `FIRST_DECLARED_OF_MULTIPLE`; it never drops
the others silently.

`timescale_unit` is explicit because the two container families disagree:
ISO base media reports ticks per second, Matroska reports nanoseconds per tick.
`video_dimensions_source` records whether dimensions came from the ISO sample
description, the ISO track header, or the Matroska video element, because those
can differ. An MP4 whose `stts` deltas are not all equal reports
`VARIABLE_NOT_CONSTANT` and no rate; its frame count is still the exact sum of
its sample counts. A Matroska frame count is never derived, because that needs a
cluster scan the extractor does not perform. Audio presence is a container
declaration only; no audio is read, decoded, or characterized.

### Bounded, fail-closed parsing

The parser never trusts a declared size. It caps total bytes (64 MiB), total
elements (4,096), nesting depth (8), track count (64), `stts` entry count
(4,096), and derived frame count (10,000,000). Box and element walks are
structural, not open recursion, so the depth cap is a defensive guard that no
input can reach through this module; it is therefore not part of
`parameters_digest`. A `size == 0` box is honoured only at the top level, where
ISO/IEC 14496-12 defines it, so a nested one cannot swallow its siblings and
turn a malformed structure into a confident absence claim.

Every structure the formats define as exactly one per parent is read as exactly
one, and a duplicate is malformed rather than a quiet first-wins or last-wins
choice: `moov`, `mvhd`, `trak`'s `tkhd` and `mdia`, `mdia`'s `mdhd`, `hdlr` and
`minf`, `minf`'s `stbl`, `stbl`'s `stsd` and `stts`, and in Matroska `Segment`,
`Info`, `Tracks`, `DocType`, `TimestampScale`, `Duration`, and a `TrackEntry`'s
`TrackType`, `DefaultDuration`, `Video`, `PixelWidth`, and `PixelHeight`.

Extracted values share one domain with the declarations in `video_creative.py`,
which imports these exact bounds: at most 8,192 pixels per side, at most 240,000
milli-fps, at most 24 hours (`MAX_EXTRACTED_DURATION_MS` *is*
`_MAX_DURATION_MS`), an ISO timescale within uint32, and a Matroska timestamp
scale of at most one second per tick. A value outside that domain is never
clamped. It is refused in one of two ways, before any canonicalization:

- `VIDEO_CONTAINER_OUT_OF_DOMAIN` when a real container could state it and the
  product simply does not carry it — an 8,193-pixel track, a 25-hour movie; and
- `VIDEO_CONTAINER_MALFORMED` when the value is past a structural ceiling that
  no container describing video reaches — a 65,535-pixel track, a frame rate of
  four billion, a timestamp scale of 2^63.

Typed refusals, all exit code 2:

- `CREATIVE_VIDEO_TOO_LARGE` — empty or beyond the byte bound;
- `UNSUPPORTED_VIDEO_CONTAINER` — not ISO base media or EBML;
- `VIDEO_CONTAINER_TRUNCATED` — a box, element, or sample table claims bytes
  beyond the buffer;
- `VIDEO_CONTAINER_MALFORMED` — invalid sizes, versions, a duplicated
  exactly-once structure, a missing required structure, or a value past a
  structural ceiling;
- `VIDEO_CONTAINER_OUT_OF_DOMAIN` — a structurally valid value beyond the shared
  product domain; and
- `VIDEO_CONTAINER_TOO_COMPLEX` — a bounded budget was exhausted.

A refusal returns no record. There is no partial or best-effort result.

## Declared versus extracted video

`video_creative.py` keeps its declaration semantics from
[`video-creative-declaration.md`](video-creative-declaration.md).
`observe_declared_video_container(asset, data)` produces a
`DeclaredVideoContainerObservation` that holds the declared values and the
extracted `VideoContainerMetadata` in separate fields.

An extracted value never replaces a declaration, never changes
`video_asset_version_id`, and never upgrades
`DECLARED_UNVERIFIED_NO_LOCAL_DECODE` into a decoded claim. Each comparable
fact carries `DECLARATION_MATCH`, `DECLARATION_WITHIN_TRUNCATION`,
`DECLARATION_MISMATCH`, or `NOT_COMPARABLE_NOT_IN_CONTAINER`, and
`declaration_status` summarizes them as `DECLARATION_MATCH`,
`DECLARATION_MISMATCH`, or `DECLARATION_PARTIALLY_UNVERIFIED`. A declared
`video/mp4` asset backed by a Matroska container is a mismatch, not a corrected
MIME type. The observation is local evidence; `execution_eligibility` remains
`REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET`.

`duration_ms` and `frame_rate_milli` are floors of exact integer ratios, so a
truthful declaration can sit exactly one unit above the extracted value. That
single unit of the extractor's own truncation is
`DECLARATION_WITHIN_TRUNCATION`, distinct from both an exact match and a real
disagreement; anything further apart is `DECLARATION_MISMATCH`.

The record re-derives everything it asserts. A `model_validator` recomputes
`video_asset_version_ref` from the declared fields it holds and recomputes every
per-field comparison and the summary from those declared fields and the
`extracted` record, so a persisted or replayed observation cannot state a
verification result its own contents do not produce.

### Decode failure is one class, not a list of exception types

`decode_image` is the single Pillow entry point in the product. Every exception
raised inside it becomes a typed refusal: `CREATIVE_IMAGE_TOO_LARGE` for
Pillow's decompression-bomb guard, and `INVALID_CREATIVE_IMAGE` for everything
else. Only `AdhelmError` (a bound this code already decided) and `MemoryError`
(the host's condition, not a claim about the file) pass through.

Naming individual exception classes does not work here. Pillow's plugins signal
a broken file with whatever their format parser happens to raise — a corrupt PNG
chunk checksum surfaces as a bare `SyntaxError` from `PngImagePlugin.crc`, which
is neither an `OSError` nor a `ValueError` — so any list of classes leaves the
next corrupt file escaping untyped. A deterministic mutation regression covers
this: for every checked-in fixture, each byte position (or a seeded 500-position
sample for the larger ones) is flipped and the result must be either a record or
an `AdhelmError`. That is 5,991 mutations across the 25 fixtures, and it runs in
about three seconds.

## Sensitive-data screening

Every extracted record is a `StrictModel`, so `adhelm.privacy.validate_data` runs
over its complete JSON projection before the instance exists. The only free-text
value either extractor can take from untrusted bytes is `container_brand` (an
ISO `ftyp` major brand or an EBML `DocType`), which is length-bounded, restricted
to printable ASCII, and screened; a hostile label fails closed with
`SENSITIVE_DATA_RISK` instead of reaching a record. No record contains pixels, a
thumbnail, media bytes, sample payloads, OCR text, or a transcript, so a
persisted record or export cannot carry creative content that was never read.

## What this explicitly does not do

- **No OCR.** On-visual text is `NOT_EXTRACTED_NO_LOCAL_OCR` for both images and
  video. The text-presence heuristic is a structural signal only.
- **No transcription.** Audio is `NOT_EXTRACTED_NO_LOCAL_TRANSCRIPTION` and
  `NOT_ANALYZED_NO_AUDIO_EXTRACTION`.
- **No frame decode.** Video is `NOT_DECODED_NO_LOCAL_DECODER`; palette, scene,
  and motion measures over video remain `NOT_ANALYZED_NO_VIDEO_DECODE`.
- **No identity, biometric, or protected-trait inference** (AGENTS rule 12).
  `face_presence_status` is `NOT_EVALUATED_NO_BIOMETRIC_INFERENCE`, and
  `identity_inference_status` and `protected_trait_inference_status` are
  `NOT_EVALUATED_PROHIBITED`. These are permanent product boundaries, not
  missing features.
- **No logo or brand detection.** `NOT_EVALUATED_NO_LOCAL_DETECTOR`.
- **No component-level performance.** `component_performance_status` stays
  `NOT_INFERRED`; ad-level delivery is never attributed to a region, colour, or
  heuristic finding.
- **No provider claim.** Extraction proves local bytes, not that a provider
  served them, that a rendition is current, or that a deployment exists.

## Evidence limits

- The layout grid and text heuristic describe the analyzed surface only. A
  source asset's grid cannot stand in for a provider crop with different text
  placement; the `analysis_surface` rules in
  [`creative-intelligence.md`](../creative-intelligence.md) still apply.
- `TEXT_LIKELY` is not proof of text, and `TEXT_UNLIKELY` is not proof of its
  absence. Dense textures, fine patterns, and screenshots can score high;
  large, smooth, low-contrast lettering can score low.
- Container metadata is what the file's own headers declare. A container can
  disagree with its coded streams; only a decoder could settle that, and there
  is none.
- Where a container declares several video tracks, the reported dimensions,
  rate, and count describe the first declared one. `video_track_count` and
  `video_track_selection_status` disclose that; they do not identify which
  track a provider would actually serve.
- The EXIF orientation tag is recorded and never applied, so the grid describes
  stored pixels. Two files with identical stored pixels and different
  orientation tags display differently and still share one layout.
- `frame_count` is a container sample count, not a proof of rendered frames.
  An MP4 duration derives from `mvhd` and can differ from the sum of sample
  durations in a malformed or edited file.
- Results are comparable only within one `extractor_version` and
  `parameters_digest`. A parameter change is a new version, not a silent
  re-measurement of retained history.
- The golden fixtures are synthetic. They prove determinism, statuses, and
  bounded refusal; they do not prove behaviour on real provider media, which
  belongs to the H02 provider capture work.

## A build-owner-approved optional local dependency

Runtime dependencies are fixed here; nothing in this ticket adds one. If a build
owner later approves optional local packages, they would close specific gaps:

- a local OCR engine (for example a Tesseract binding) could replace
  `NOT_EXTRACTED_NO_LOCAL_OCR` with exact `ON_VISUAL_TEXT` components, their
  bounding regions, and a CTA label read from the asset. Its output would be
  untrusted text: screened before persistence, provenance `LOCAL_EXTRACTED`,
  with its own engine name, version, language pack, and confidence, and it would
  never overwrite a declared or human-reviewed value.
- a local video demuxer/decoder (for example a bundled FFmpeg) could replace
  `NOT_DECODED_NO_LOCAL_DECODER` with a deterministic frame sample and would
  make per-frame palette, opening-frame, scene-count, motion, caption timing,
  and CTA timing real rather than status fields.
- a local speech recognizer could replace
  `NOT_EXTRACTED_NO_LOCAL_TRANSCRIPTION` with a timed local transcript.

Each would be optional, absence-tolerant (the current statuses remain the
truthful answer when it is not installed), locally executed with no upload, and
recorded with its own analyzer name and version so results across
configurations are never silently compared. None of them changes the biometric,
identity, or protected-trait prohibition, which no dependency can lift.

## Golden fixtures

`tests/fixtures/creative/` holds the checked-in fixtures, a `manifest.json` of
their exact sizes and digests, and `expected-extraction.json` with the exact
record or typed refusal for every fixture.
`tests/fixtures/creative/generate.py` regenerates all of them: images are drawn
pixel by pixel with Pillow, and every video fixture is a hand-built ISO base
media or EBML byte structure, so no encoder, decoder, or network is involved.
Regenerating with a different Pillow release can change compressed image bytes,
so the checked-in files and their digests — not a regeneration — are the golden
inputs.

Fixtures cover a solid image, a four-quadrant layout, a striped text-like band,
a JPEG gradient, a lossless WebP banner, an image below the grid minimum, a
truncated PNG, constant- and variable-rate MP4s, a 64-bit `largesize` movie box,
an MP4 with an unknown duration, an MP4 whose dimensions come only from its track
header, an MP4 with two declared video tracks, an MP4 whose duration floors one
millisecond below a truthful declaration, MP4s that are truncated, claim an
oversized box, carry an invalid box size, hide a `size == 0` box inside `moov`,
omit `moov`, or exceed the element budget, a WebM with audio and a constant
rate, a WebM that omits `TimestampScale`, a video-only Matroska without duration
or rate, a WebM element claiming bytes beyond the buffer, and a non-video
container.

## Validation

```text
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest -q -n 8 --dist=loadfile tests
uv run python scripts/check_repository.py
```
