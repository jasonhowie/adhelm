# OpenAI account-key profile ultra review

## Scope

This review covered the closed `OpenAIAccountKeyProfile` configuration union,
credential setup and retrieval, installed configuration/schema paths, and the
fixed OpenAI conversion, inventory, and report readers. It used fixture
transports and fake credential stores only.

## Reproduced findings and fixes

| Finding | Reproduction | Fix |
| --- | --- | --- |
| A constructed OpenAI key profile could be selected by report or inventory code before whole-configuration revalidation, allowing its malformed fields to reach private sharing-key setup. | Inject a `Configuration.model_construct()` containing an account-key profile with `account_ref="bad.account"`, then invoke inventory or report with `export_key()` and HTTP construction set to fail. | `validate_configuration()` now detaches models with serialization warnings promoted to errors. Inventory and report revalidate before artifact preflight, key access, credentials, or HTTP. |
| `Literal[1]` accepted JSON `1.0` and `true` because Python considers both equal to integer one. | Submit account-key JSON with `schema_version` set to `1.0`, `true`, or `"1"`. | The new profile has an exact-type before validator; only integer `1` is accepted. Legacy profile serialization was not changed. |
| The new shape was accepted by `config add-profile` but had no named installed schema. | Request `schema openai-account-key-profile`. | The CLI registers the closed account-key schema and an end-to-end schema/config-add regression verifies it. |
| The generic safe-reference grammar permitted account IDs and timezones that the fixed OpenAI reader would reject later. | Supply `bad.account` or `Not/AZone` in an account-key profile. | The account-key profile now requires the provider's bounded native account-ID grammar and an IANA timezone at local configuration validation. |

## Verified boundaries

- Account-key profiles have a required discriminator, fixed `openai_ads`
  provider, `NOT_DOCUMENTED` scope status, no OAuth principal/client/scopes,
  and no secondary credential field.
- Constructed wrong provider/kind/account values, or a non-READ profile in the
  conversion reader, fail before credential access and HTTP construction.
- The account-key primary credential supports the existing explicit-CI gate;
  secondary lookup/setup fails before an OS-store call. Case-folded OS-store
  aliases and a constructed reserved sharing-key reference fail closed.
- Actual CLI schema, profile addition, and auth-status paths omit raw account,
  profile, and credential-slot aliases from agent-visible output. Fixture
  conversion, inventory, and report paths accept the valid profile while their
  output redacts account references and never returns the fixture credential.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_openai_account_key_profile.py \
  tests/test_openai_account_key_profile_ultra_review.py \
  tests/test_openai_measurement_workflow.py \
  tests/test_openai_workflow_ultra_review.py \
  tests/test_credential_setup.py \
  tests/test_credential_cli.py \
  tests/test_credential_boundary.py \
  tests/test_inventory.py \
  tests/test_inventory_new_providers.py \
  tests/test_openai_inventory_money.py \
  tests/test_reporting.py \
  tests/test_report_cli_providers.py
```

The account-key work remains fixture evidence. A configured key's real
provider permissions, account ownership, supported scope vocabulary, and live
read feasibility still require separate customer-authorized validation.
