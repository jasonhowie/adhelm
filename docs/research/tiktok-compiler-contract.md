# TikTok Ads v1.3 offline compiler contract

Reviewed 2026-09-04. This is a narrow, fixture-only v1.3 declaration compiler
for one regular website-traffic campaign, one ad group, and one in-feed video
creative. It sends no request, creates no resource, uploads no media, and does
not establish an account's eligibility to create any resource.

## Reviewed local candidate

The compiler fixes campaign CBO off (`budget_optimize_on=false`) and places the
whole USD lifetime cap on its only ad group. The v1.3 ad-group reference
documents `BUDGET_MODE_TOTAL` with `SCHEDULE_START_END`; its `budget` is a
major-unit numeric value, while start and end are UTC strings in
`YYYY-MM-DD HH:MM:SS` form. The brief accepts `total_budget: Money`, requires a
positive USD value matching the account, and derives the exact major-unit value
as a two-decimal `Decimal` for its canonical JSON field fragment. It does not
emit a fictional `budget_minor_units` provider field, and it never derives a
lifetime cap from a daily budget.

The fixed field fragments preserve only the reviewed values: website promotion,
normal TikTok-only placement, US location `6252001`, unrestricted gender, the
complete reviewed adult age range, `CLICK`/`CPC`, `BID_TYPE_NO_BID`, smooth
pacing, and `DISABLE` at campaign, ad-group, and creative levels. The current
reference also documents a 2028-01-01 schedule-start upper bound, which is
enforced by this frozen contract. Current account-time eligibility remains a
readback requirement before any future egress.

For the regular ad route, the local creative fragment uses the documented
creative field names `identity_type`, `identity_id`, `ad_format`, `video_id`,
`ad_text`, `call_to_action`, `landing_page_url`, and `operation_status`. This
slice fixes one customized identity (`CUSTOMIZED_USER`), one video format
(`SINGLE_VIDEO`), and `LEARN_MORE`. It retains no `advertiser_id`,
`campaign_id`, or `adgroup_id`: TikTok's regular ad create body requires
provider IDs and a `creatives` list, so the fragment is deliberately not an
executable request.

## Declared video boundary

`CustomerDeclaredVideoAsset` is a closed customer declaration of one
`video/mp4` content digest, declared byte size, dimensions, duration, and frame
rate. Its content-addressed identity excludes locale and evidence provenance,
because neither changes the declared media bytes. A
`CustomerDeclaredVideoRendition` binds that asset identity, provider format,
and locale. The serving manifest directly references this VIDEO rendition; an
image asset cannot stand in for the serving video. Content, duration, or
dimension changes therefore change the declared asset ID, rendition ID, and
manifest/creative identity.

The declaration carries only `CUSTOMER_DECLARED` provenance and
`DECLARED_UNVERIFIED_NO_LOCAL_DECODE` status. It explicitly records that no
frames, palette, OCR, transcript, or audio analysis were produced and that no
component-level performance is inferred. It accepts no path, bytes, media
decoder, subprocess, uploader, or claimed available/provider-source asset.
Changing an evidence reference alone does not create a new media-byte identity.

The declared `video_id` and identity are opaque account-bound references, not
proof that either is usable. Promotion must require a verified canonical video
asset history and current provider readback before a guarded executor can bind
native IDs or construct a real request.

## Revalidation and privacy boundary

The compiled graph retains its source brief and validates the hierarchy handles,
native names, major-unit total, UTC schedule strings, destination policy and
destination digest, creative text/CTA/manifest, video declaration and
rendition, evidence references, account bindings, and input/reference binding
digests after serialization. Unknown native fields fail. The redacted review
contains only local handles, status, content digests, and maximum spend; it
does not expose destination URLs, copy, native video or identity references, or
evidence references.

The compiler is `OFFLINE_CANDIDATE_NOT_EXECUTABLE` and
`FIXTURE_ONLY_NOT_LIVE_VALIDATED`. A future promotion must run the full guarded
path: account/currency/identity readback; media, custom-identity, schedule,
objective, targeting, placement, creative-transformation, destination/tracking,
and native-conversion checks; verified video history and ledger checks;
provider-ID binding; drift rejection; digest-bound authorization; durable
`APPLY_STARTED`; guarded apply; and readback verification. None of these facts,
including conversion availability, policy approval, delivery, or native request
acceptance, is proved here.

## Primary sources

- [TikTok Marketing API v1.3 ad-group create reference](https://business-api.tiktok.com/gateway/docs/index?doc_id=1739499616346114)
- [TikTok-maintained v1.3 ad create route](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AdApi.md)
- [TikTok-maintained regular Ad Create body](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AdCreateBody.md)
- [TikTok-maintained regular creative fields](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AdcreateCreatives.md)
- [TikTok-maintained identity API reference](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/IdentityApi.md)
- [TikTok-maintained file/video route list](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/FileApi.md)
- [TikTok account identity/read contract](tiktok-ads-read-contract.md)
