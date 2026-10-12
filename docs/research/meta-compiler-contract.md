# Meta Ads paused traffic compiler contract

This is an offline, fixture-only compiler. It makes no HTTP request, does not
upload an image, create a provider resource, request approval, or activate
delivery. Every result is `OFFLINE_CANDIDATE_NOT_EXECUTABLE`.

## Narrow v26.0 candidate

The compiler emits a local parent-handle hierarchy for one customer-supplied
existing image: a paused `OUTCOME_TRAFFIC`/`AUCTION` campaign with an exact
`lifetime_budget`, one paused `LINK_CLICKS` ad set (`IMPRESSIONS`,
`LOWEST_COST_WITHOUT_CAP`), one standalone single-image link creative, and one
paused ad. `adset_parent_campaign_handle`, `creative_consumed_by_ad_handle`,
`ad_parent_adset_handle`, and `ad_creative_handle` bind the local hierarchy.
They are logical compiler handles, never native Meta IDs; a future executor
must resolve every returned campaign, ad-set, and creative ID before forming a
dependent request. The campaign request alone owns the lifetime cap and
timezone-aware start/stop timestamps; the ad-set request deliberately has no
daily or lifetime budget or schedule field.

The input `lifetime_budget` is a typed `{minor_units, currency}` object. It must
be positive USD matching the declared account currency. The native integer
budget retains exactly those minor units; neither a bare input amount nor a
currency conversion is accepted.

It accepts only a customer-declared US country target and emits only Facebook
Feed placement fields. Every nested native structure is a closed typed model:
there is no map through which audiences, dynamic templates, custom code, or
other unreviewed payload fields can enter. It emits no automatic creative
features or transformation fields, audiences, special-ad categories, conversion
resources, pixels, datasets, uploads, generated copy/assets, or tracking specs.
Omission does not prove Meta will not transform the creative: transformation
and placement eligibility are explicit future readback requirements. The
standard review surface is `redacted_review`, which omits the image reference,
Page ID, destination URL, and exact copy.

The single supplied image must be the canonical identity `HERO` rendition of
the content-addressed local asset. The canonical manifest contains exactly one
native primary-text slot, headline slot, and `LEARN_MORE` CTA slot whose
destination reference matches the native link. The compiled result recomputes
that creative content binding when it is parsed, so a changed native image,
copy, Page, or destination cannot retain the original creative digest.

Meta's pinned official [Business SDK v26.0.0 release](https://github.com/facebook/facebook-python-business-sdk/releases/tag/26.0.0)
identifies commit `77aa285`. Its generated v26.0.0 schemas expose campaign
create fields including `objective`, `buying_type`, `lifetime_budget`,
`special_ad_categories`, `start_time`, `stop_time`, and `status` in
[`adaccount.py`](https://raw.githubusercontent.com/facebook/facebook-python-business-sdk/26.0.0/facebook_business/adobjects/adaccount.py).
They expose `PAUSED`, `IMPRESSIONS`, `LINK_CLICKS`, and
`LOWEST_COST_WITHOUT_CAP` in [`adset.py`](https://raw.githubusercontent.com/facebook/facebook-python-business-sdk/26.0.0/facebook_business/adobjects/adset.py),
and ad creation accepts `adset_id`, `creative`, `name`, and `status` in the
same pinned `adaccount.py`. The generated link-data schema includes
`image_hash`, `link`, `message`, `name`, and `call_to_action` in
[`adcreativelinkdata.py`](https://raw.githubusercontent.com/facebook/facebook-python-business-sdk/26.0.0/facebook_business/adobjects/adcreativelinkdata.py).

Meta's official Postman examples corroborate the campaign/ad-set sequence and
paused states, but their exposed examples use an older API version; they are not
treated as v26 schema authority: [ad set](https://www.postman.com/meta/facebook-marketing-api/request/exbh254/creating-adset-l2),
[single-image creative](https://www.postman.com/meta/facebook-marketing-api/request/2y4pkug/creating-standalone-adcreative-l-1),
and [ad](https://www.postman.com/meta/facebook-marketing-api/documentation/9jo4f5y/mapi-onboarding).

## Required promotion checks

The account timezone and USD currency in an input are customer declarations,
not identity proof. Timezone must be a valid IANA name, but still requires
account readback. The image hash is likewise only a customer-provided opaque
reference with local evidence. Before any future write, a planner must verify
account identity, currency and timezone readback, image readback and account
binding, Page and placement eligibility, canonical creative-ledger and
no-transformation bindings, destination/tracking contract, account
capabilities, conversion requirements, current remote state/drift, exact direct
or delegated authorization, and provider response/recovery behavior. The
lifetime budget is the candidate maximum spend; a daily budget is intentionally
absent because it does not establish a total spend bound.

The generated SDK establishes the enclosing `targeting` field but does not by
itself prove that a particular account can deliver the nested Facebook Feed
placement structure. SDK validation also cannot prove account entitlement,
Page/image eligibility, policy approval, conversion configuration, mutation
responses, idempotency, or runtime result. The v26 release adds Conversions API
parameter-builder capabilities that can generate or enhance identifiers; this
compiler neither depends on that SDK nor exposes any of those fields. Direct
Meta reference pages returned HTTP 429 during the read-contract review, so
fixture tests and the pinned generated SDK do not establish live provider
support.
