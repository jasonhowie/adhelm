# Local credential boundary ultra review

Reviewed: 2026-09-04. This review used synthetic in-memory credential stores
and non-TTY test streams only. It did not read a real credential, invoke an
interactive terminal, contact a provider, or make a network request.

## Findings and remediation

| Severity | Finding | Remediation and regression |
| --- | --- | --- |
| P1 | Reserved export-key references and credential-slot aliases were compared as exact strings. `SafeRef` permits uppercase values, so a case-insensitive OS store could use `EXPORT-PSEUDONYMIZATION-V1` as a provider credential or collapse `primary` and `PRIMARY` into one slot. | `Profile`, setup-wide validation, and credential revalidation now casefold reserved/OS-store slot references. `test_casefolded_export_key_reference_is_rejected_before_store_access`, `test_casefolded_primary_and_secondary_aliases_are_rejected`, and `test_casefolded_cross_profile_os_store_alias_fails_before_backend_use` cover model, forged-object, and configuration boundaries. |
| P1 | `store` treated an existing empty keychain value as missing because it tested truthiness. It could overwrite an occupied local slot without the explicit rotate operation. | Store now tests `existing is not None`. `test_store_refuses_an_existing_empty_slot_without_prompt_or_overwrite` proves no prompt, store mutation, or accidental replacement occurs. |
| P1 | A generated export pseudonymization key was returned immediately after `set_password`. A dropped or competing store write could make one export use a key unavailable to later local projections. | The reserved key slot is locked before backend access and its generated value must be read back exactly before use. `test_export_key_requires_durable_exact_readback_after_initialization`, `test_export_key_rejects_malformed_store_values_without_leaking_them`, and `test_export_key_busy_lock_stops_before_opening_the_credential_backend` cover dropped, replaced, malformed, and busy-slot behavior. |
| P1 | Two cooperating setup clients could both pass a read/check/write window because keychain APIs do not provide portable compare-and-swap. | A private per-OS-user, casefolded slot lock now spans setup backend access, prompt, reread, mutation, and verification. `test_separate_processes_serialize_an_empty_simulated_os_store` exercises independent spawned processes; `test_concurrent_setup_is_busy_before_prompt_or_keychain_access` verifies the CLI returns busy before either side effect. The lock rejects symlink, FIFO, permissive, hard-linked, and non-sticky writable-ancestor paths. |
| P1 | Credential access caught only `KeyringError`; an unexpected approved-backend exception could escape the local boundary with private implementation detail. | The credential accessor and export-key accessor preserve structured AdHelm errors and sanitize every other backend exception. `test_unexpected_os_store_exceptions_are_sanitized_at_every_credential_boundary` covers both paths. |

## Verified behavior

- Default CLI machine mode requests an explicit terminal handoff before opening
  a store or prompting. `--interactive` still requires TTY input and output;
  `GetPassWarning` is an error, so `getpass` cannot fall back to echoed stdin.
- Status output contains only a configuration digest, provider, slot, local
  state, and explicit `provider_authentication_verified=false`; it excludes
  secret values, configured aliases, and backend exception details.
- Store/rotate bind a selected configured profile and slot, reread before a
  mutation, and verify local readback. Remove-local verifies local absence and
  explicitly makes no provider-revocation claim. A post-mutation verification
  or lock-cleanup failure is emitted as `LOCAL_CHANGE_UNVERIFIED` with exit 6;
  it is never reported as a pre-side-effect failure.
- The execution credential accessor revalidates a supplied `Profile` at its
  boundary, preventing `model_copy` validator bypass from resolving the export
  pseudonymization key as a provider bearer credential.
- macOS/Linux credential mutation and export-key initialization hold an
  exclusive no-follow per-user lock independently of the application data
  directory. Unsupported platforms fail closed before those local mutations.

## Retained limit

The coordinator serializes cooperating AdHelm processes, but it cannot restrain
an arbitrary same-UID process that bypasses it and modifies either the OS-store
entry or the private lock path. OS credential APIs also provide no portable
compare-and-swap against that external intervention. Readback makes such a
disagreement fail closed when observed; it cannot promise immutable local state
after the operation returns.

## Validation

```text
uv run pytest -q tests/test_credential_setup.py tests/test_credential_cli.py \
  tests/test_credential_boundary.py tests/test_credential_ultra_review.py \
  tests/test_credential_lock.py
# 34 passed

uv run ruff check src/adhelm/models.py src/adhelm/credentials.py \
  src/adhelm/credential_lock.py src/adhelm/credential_setup.py \
  src/adhelm/credential_cli.py tests/conftest.py \
  tests/test_credential_boundary.py tests/test_credential_cli.py \
  tests/test_credential_lock.py tests/test_credential_setup.py \
  tests/test_credential_ultra_review.py
uv run ruff format --check src/adhelm/models.py src/adhelm/credentials.py \
  src/adhelm/credential_lock.py src/adhelm/credential_setup.py \
  src/adhelm/credential_cli.py tests/conftest.py \
  tests/test_credential_boundary.py tests/test_credential_cli.py \
  tests/test_credential_lock.py tests/test_credential_setup.py \
  tests/test_credential_ultra_review.py
uv run mypy src/adhelm/models.py src/adhelm/credentials.py \
  src/adhelm/credential_lock.py src/adhelm/credential_setup.py \
  src/adhelm/credential_cli.py
git diff --check
# clean
```
