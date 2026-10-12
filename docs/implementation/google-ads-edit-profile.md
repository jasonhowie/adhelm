# Google Ads OAuth EDIT profile

The local Google desktop OAuth declaration now accepts a separate Google Ads
`EDIT` purpose with exactly `https://www.googleapis.com/auth/adwords`. Google
uses that same API scope for reads and writes. The purpose separates local
profiles and protected bundles; it is not a provider-enforced read-only scope
or a grant to mutate advertising resources. See the provider's
[REST authentication contract](https://developers.google.com/google-ads/api/rest/auth).

An EDIT draft and an `OS_OAUTH_GOOGLE` EDIT profile require explicitly supplied,
nonnull account currency and IANA timezone. Validation checks field presence
before defaults are applied. The draft's Google Ads EDIT schema branch and the
profile's conditional schema require both fields and exclude null currency.
Existing READ declarations retain their nullable currency and UTC default,
including the same canonical data bytes. The account and optional manager-login
IDs remain ten hyphen-free digits; the OAuth credential reference must be a
nonreserved local slot.

Explicit local enrollment continues through the reviewed desktop-client flow
and derives the OIDC principal from its verified grant. The saved result says
`READY`, with account access and execution
authority false. Reconnect
retains the configured purpose, principal, client and scope; it does not promote
a READ bundle to EDIT. Even with identical Google scopes, the protected bundle
still binds its exact local profile and purpose.

This declaration supports the separate private EDIT observation slice. It adds
no campaign mutation, approval subject, native route, generic query, caller
bearer override or provider permission claim. No real browser, OS credential
store, OAuth endpoint or customer account was exercised during implementation.

## Validation

The root profile/setup/credential/workflow group passed 127 tests, including
new omission/null, READ canonical-data continuity, credential alias, scope,
identifier and same-scope cross-purpose bundle cases. Fake enrollment retains
the EDIT profile while leaving resource access unverified.
The independent session/profile results are recorded in
[the ultra review](google-ads-edit-observation-ultra-review.md).
