# Meta campaign pause reference — 2026-09-05

Meta's [campaign-management guide](https://developers.facebook.com/docs/marketing-api/get-started/manage-campaigns),
retrieved through Context7's official-documentation index, describes pausing an
existing campaign with a POST to its campaign node and `status=PAUSED`. The
response is a boolean success acknowledgement. The indexed example uses v25.0;
it is not evidence that v25.0 is the current version. Direct retrieval of the
campaign reference returned HTTP 429.

The current official [SDK API configuration](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/apiconfig.py)
declares Graph v26.0 and SDK v26.0.1. The official generated
[Campaign object](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/campaign.py)
exposes `account_id`, `configured_status`, `effective_status` and `status`, and
implements node updates as POST with a status enum containing PAUSED. No SDK
code was copied or added as a dependency. These mutable source URLs describe
what was reviewed today, not an immutable upstream provenance attestation.

The [authorization guide](https://developers.facebook.com/docs/marketing-api/get-started/authorization),
also retrieved through Context7, distinguishes customer-owned-account access
from reviewed access to others' accounts. The official
[SDK introduction](https://github.com/facebook/facebook-python-business-sdk/blob/main/README.md)
identifies `ads_management` for advertising management. A local EDIT label is
not proof of that provider permission or required app review.

Implementation implications: use an account-owned campaign node, separate
configured state from delivery state, bind a complete allowlisted baseline,
require an exact plan/credential identity and kernel permit, and verify with
independent reads after the success acknowledgement. A campaign pause affects
delivery of its children; it does not authorize changing their configured
statuses or claim immediate zero spend. A lost response cannot be settled from
a later paused observation alone. This research establishes no live-tested
capability or installed native approval support for Meta.

A separate authorization prerequisite is now identified: the official
[debug-token reference](https://developers.facebook.com/docs/graph-api/reference/debug_token),
retrieved through Context7, describes GET `/v26.0/debug_token` with the inspected
credential in `input_token`. It returns app/user identity, validity, expirations
and granted scopes, and requires an associated app token or app-developer user
token. This needs its own narrow private credential transport and redacted
response handling; the current generic privacy-validated report transport must
not be relaxed to accept token-bearing query parameters. This boundary must be
implemented and reviewed before a configured Meta mutation workflow can claim
current OAuth principal, application or permissions. No such request was sent
to Meta during this review.
