# Meta token CLI: independent review

This review used synthetic draft data, in-memory credential-store seams,
private workflow substitutes, and Typer's in-process runner. It did not open
an operating-system credential store, prompt a terminal, contact Meta, or
write a provider resource.

## Reviewed boundary

`tests/test_meta_token_cli_ultra_review.py` adds ten fake-only regressions to
the `auth meta-token` command surface. The tests verify that:

- token-bearing JSON fields are rejected by the closed draft schema before a
  prompt or protected-store access, and their values do not appear in output;
- token-like command flags are rejected by the CLI parser without exposing the
  supplied value or reaching a secret/store boundary;
- token-named environment values are ignored; noninteractive enrollment still
  requires the explicit terminal handoff;
- rotate, status, and inspect each prove their maximum result envelope fits
  before their respective prompt, protected-store, or fixed-inspection path;
- inspect passes the exact digest of the configuration it preflighted into the
  private fixed workflow; and
- the inspection projection retains only its safe digest, purpose, scope
  count, and expiry facts while omitting profile, account, application,
  principal, credential-binding, scope-name, and token text.

The command registration, static discovery, and draft schema are exercised by
the owner suite. This independent review also leaves the checked-in CLI
unconfigured for generic secret ingestion: the only permitted secret entry is
the two explicit hidden terminal prompts.

## Scope limits

The CLI's fixed `debug_token` inspection does not prove account access, Meta
app review, mutation permission, provider execution authority, or a provider
write. This review supplies no generic credential command, token file or
environment loader, browser handoff, token refresh/exchange, live Keychain
evidence, or live Meta evidence.
