# LinkedIn existing-dark-image-post compiler contract

Last reviewed: 2026-09-04. API version: `202608`. Status: fixture-only offline
candidate compiler; it is not a creation, upload, post-publication, activation,
or provider-write capability.

## Narrow route

The compiler accepts exactly one customer-declared, existing `urn:li:ugcPost:*`
or `urn:li:share:*` dark image post and compiles a three-node local graph:

```text
local campaign-group handle -> local campaign handle -> local creative handle
                                              \-> existing customer post URN
```

It represents the later REST creation order as a draft campaign group, a draft
engagement `SPONSORED_UPDATES` campaign, then a draft creative with
`content.reference` to that post. The group has a complete native create body.
The campaign and creative have explicit local-parent templates instead: their
native fields omit the required `campaignGroup` and `campaign` fields until a
future guarded adapter creates and reads back the actual
`urn:li:sponsoredCampaignGroup:*` and `urn:li:sponsoredCampaign:*` values. The
complete native models accept only those real URNs; local handles are never
serialized as invented wire keys. A future adapter must bind, revalidate, and
authorize those complete bodies after each readback. This output is therefore a
typed offline template, never an executable request or plan.

The fixed versioned transport contract for a later guarded adapter is
`Linkedin-Version: 202608` plus `X-Restli-Protocol-Version: 2.0.0`, with the
documented collection paths `/adAccounts/{accountId}/adCampaignGroups`,
`/adAccounts/{accountId}/adCampaigns`, and `/adAccounts/{accountId}/creatives`.
The compiler does not send those requests.

The brief represents the group total, campaign daily budget, and CPC unit cost
as separate positive `Money` values, each with `minor_units` and `currency`.
They must use the declared USD account currency. The group uses
`objectiveType: ENGAGEMENT`, a fixed UTC epoch-millisecond
inclusive-start/exclusive-end schedule, `status: DRAFT`, and exact USD group
`totalBudget`. It intentionally omits `budgetOptimization`: outside `DYNAMIC`,
the group total budget is documented as the maximum across associated campaigns
and creatives. This is the output's `maximum_spend`; the campaign daily budget
and CPC unit cost are constraints, not a derived total spend bound.

The campaign is fixed to US/en, `CPC`, `NONE` optimization, disabled audience
expansion, CTV, and offsite delivery; it declares `NOT_POLITICAL`. The
`associatedEntity` is the same organization URN as the post author. It uses
only US location and `en_US` interface-locale targeting facets. The compiler
does not set a campaign `format`: LinkedIn documents that a campaign without an
ad format has it set by its first creative, while the reviewed format tables
contain contradictory terminology for single-image cases. It does not claim a
format assertion it cannot source reliably.

The customer-declared post contains the organization Page author, its matching
DSC ad-account URN, commentary, image URN/title/alt text, `PUBLIC` visibility,
`NONE` feed distribution, `PUBLISHED` lifecycle, `isDsc: true`, `STANDARD`
single-image DSC type, and active DSC state. Its canonical digest binds every
one of those declarations. The compiler requires the DSC account to equal the
campaign account, and its creative-content digest binds the post URN, Page
author, account, local creative version, and image content digest. It explicitly
declares both CTA and landing page as `NOT_APPLICABLE` for this engagement-only
route. The creative refers to the existing post URN, never to the image URN,
inline content, or a post-publish operation. Post creation is excluded: a new
post could not safely be treated as a draft creative resource.

## Required promotion checks

Before any future write, a guarded adapter must verify the required ad-account
and Page scopes/roles, current account and currency, group
`allowedCampaignTypes`, remote group/campaign/post/Page/image ownership and
eligibility, image availability, organization association, audience minimum,
supported locale/targeting, post visibility/distribution and dark-post state,
final creative format/rendering, and native conversion needs. It must create
and read back each parent URN before assembling the next complete native body,
then revalidate every bound field. It must also perform creative-ledger
validation, destination/tracking validation when a different objective is
introduced, pre-write drift rejection, exact authorization, durable
`APPLY_STARTED`, post-write readback, and recovery. Draft status defers some
provider validation and is not proof that the later candidate is eligible or
servable.

The compiler rejects a non-US/English target, non-USD or nonpositive budget
`Money`, a daily budget above the group total, a post whose DSC account differs
from the campaign account, changed post/Page/account or creative-content
binding, CTA or landing-page field, non-millisecond-aligned or invalid schedule,
non-draft native state, local handles in complete native payloads, and unknown
fields. All output has
`OFFLINE_CANDIDATE_NOT_EXECUTABLE` and
`FIXTURE_ONLY_NOT_LIVE_VALIDATED` boundaries.

## Primary sources

- [Marketing API versioning](https://learn.microsoft.com/en-us/linkedin/marketing/versioning?view=li-lms-2026-08): latest version is 202608; each versioned call requires the version header.
- [Campaign groups](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaign-groups?view=li-lms-2026-08): draft state, schedule semantics, group total-budget maximum, account currency, and campaign-type readback.
- [Campaigns](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaigns?view=li-lms-2026-08): required native fields, political-intent declaration, targeting criteria, draft validation deferral, native budget semantics, and format constraints.
- [Creatives](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-creatives?view=li-lms-2026-08): required campaign relation, post references, draft intended status, and creative format relation.
- [Post schema](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/post-api-schema?view=li-lms-2026-08): post author/commentary/distribution/lifecycle requirements and CTA/landing-page meaning.
- [Image Ads](https://learn.microsoft.com/en-us/linkedin/marketing/usecases/ad-tech/image-ads-usecase?view=li-lms-2026-08): existing and direct-sponsored image-post paths, creative references, and Page/ad-account role prerequisites.
- [Images API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/images-api?view=li-lms-2026-08): image ID, availability, ownership, and access constraints.

## Deliberate gaps

The official campaign material does not establish a conflict-free, fixed
single-image campaign-format enum for this route: image-oriented documentation
uses terminology that conflicts with the campaign format matrix's
`STANDARD_UPDATE`/`SINGLE_VIDEO` entries. The compiler avoids emitting one.
The campaign objective schema lists `WEBSITE_VISITS` while samples use
`WEBSITE_VISIT`; this route uses `ENGAGEMENT` and cannot validate either
website objective. Existing-post readback, image/Page/account ownership,
audience size, provider eligibility, provider-returned parent URNs, exact
request serialization after those URNs are resolved, and all remote lifecycle
behavior remain unverified.
