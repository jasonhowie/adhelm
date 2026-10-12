# OpenAI Ads account-key profiles

`OpenAIAccountKeyProfile` is a closed configuration shape for OpenAI Ads
account keys. It requires `profile_kind=OPENAI_ADS_ACCOUNT_KEY` and records
`provider_scope_status=NOT_DOCUMENTED`; it does not represent OAuth scopes,
principal, or client application metadata. `purpose=READ` is a local workflow
gate and makes no provider least-privilege claim.

Use `adhelm schema openai-account-key-profile` for the installed closed input
shape. Its `schema_version` is exactly the JSON integer `1`; `1.0`, `true`, and
string substitutes fail. Its native account identifier is bounded ASCII
letters, digits, underscores, and hyphens, and its timezone must be an IANA
timezone. These checks bind local configuration to the fixed OpenAI Ads reader;
they do not establish provider account ownership or key permissions.

[`examples/openai-account-key-profile.json`](../../examples/openai-account-key-profile.json)
is a nonsecret starting shape. Replace the placeholder native account ID and
declared currency/timezone with metadata that matches the customer's account.
The example's `credential_ref` is an OS-store slot name, never the key itself.

`config add-profile` accepts this shape and `schema config` includes it in the
closed configuration union. The command output exposes only provider, purpose,
and the fact that no credential was stored. `auth status` similarly omits local
profile and credential-reference aliases. Credential setup preserves the
existing explicit CI opt-in, rejects the reserved export-sharing-key reference
and case-folded OS-store aliases, and exposes only a primary slot for this
profile; a Google-style secondary slot is rejected before a prompt or store
access.

Legacy `Profile` serialization remains unchanged. Configuration requires the
account-key discriminator, so an absent discriminator cannot silently become an
account-key profile. Public read entry points detach and revalidate the full
configuration before artifact preflight, sharing-key access, credential lookup,
or HTTP construction; constructed or copied malformed profiles fail closed.

The fixed OpenAI conversion read, inventory, and report compositions accept a
valid account-key profile in fixture tests. They still perform their own
same-session identity and currency/timezone checks. This local profile is not
provider-side permission evidence, does not authorize a mutation, and has no
live-provider validation claim.
