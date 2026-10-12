# Contributing to AdHelm

Thank you for helping. AdHelm touches advertising accounts and credentials, so
the bar for a change is evidence, not enthusiasm. This guide says how to propose
a change and what a reviewer will check.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Report a vulnerability privately as [SECURITY.md](SECURITY.md) describes, never
in a public issue or pull request.

## Before you start

Read [AGENTS.md](AGENTS.md). It holds the 29 non-negotiable rules that every
change must respect, whether a person or an AI agent writes it. A few that
matter most to contributors:

- Never commit credentials, tokens, private keys, customer exports or raw
  provider payloads. Examples contain placeholders only.
- Default tests use synthetic data and fake providers. A test must never read an
  operating-system credential store, contact a provider, or use a person's data.
- AdHelm `0.1.0` is read-only. Remote writes are refused by one release-policy
  gate, and no setting enables them. Do not add a path around that gate, and do
  not turn a failing check into a refusal to make it pass.
- No stubs, fabricated results or silent fallbacks. A capability is either
  evidenced at its declared status or fails explicitly before any side effect.

For anything larger than a small fix, open an issue first so that scope and
design are agreed before you write code. The [release definition](docs/release-0.1.0.md)
and [product brief](docs/product-brief.md) say what is in and out of scope.

## Set up

You need Python 3.12 and [uv](https://docs.astral.sh/uv/). From a clone:

```sh
uv sync --frozen --python 3.12.13
```

## Validate a change

Run these before you open a pull request. CI runs the same checks on Ubuntu,
macOS and Windows.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -n 2 --dist=loadfile --cov=adhelm --cov-report=term-missing
uv build
uv run python scripts/check_repository.py
```

While you work, run a focused subset such as
`uv run pytest tests/test_release_labels.py`. Run the full suite when you touch a
shared surface: the command registry and CLI, models, safety and approval code,
the ledger and projections, or file and credential handling.

## What a reviewer expects

- **Behavior tests, including the negative cases.** Each safety rule you touch
  needs a test that tries to break it: forged or stale input, drift, an
  ambiguous result, a credential that must not be read, an output that must not
  contain a secret. A test that only exercises the happy path does not show that
  a rule holds.
- **No weakened checks.** Do not loosen an assertion, delete a test, or add a skip
  without a stated reason. A platform-specific skip names the platform and why.
- **Claims that match evidence.** [`docs/capability-matrix.md`](docs/capability-matrix.md)
  records what each provider operation is proven to do. A change may promote a row
  only with the evidence that row's status requires. Fixture tests do not make a
  provider `LIVE_PILOTED`; only a redacted pilot record does.
- **Honest documentation.** Update the contract, schema and implementation notes
  that your change affects, and say what is not yet verified.
- **Small, focused changes.** One concern per pull request, with no unrelated
  refactoring. A change that is hard to review is hard to trust.
- **Idiomatic code.** Follow the surrounding code's structure and naming, keep
  types strict, and leave no dead code.

A maintainer reviews every pull request. Expect questions about edge cases and
about which test would fail without your change.

## Commits and pull requests

Write commit messages in the Conventional Commits style, for example
`fix: reject a repeated page token` or `docs: describe the pilot record`.
Describe what changed and why. In the pull request, list the checks you ran and
anything you could not verify.

## Developer Certificate of Origin

AdHelm uses the Developer Certificate of Origin (DCO) instead of a contributor
license agreement. Every commit must carry a `Signed-off-by:` line with your real
name and an email address you can be reached at:

```sh
git commit -s -m "fix: reject a repeated page token"
```

That line certifies the following, which is version 1.1 of the DCO from
<https://developercertificate.org/>:

> Developer's Certificate of Origin 1.1
>
> By making a contribution to this project, I certify that:
>
> (a) The contribution was created in whole or in part by me and I have the right
> to submit it under the open source license indicated in the file; or
>
> (b) The contribution is based upon previous work that, to the best of my
> knowledge, is covered under an appropriate open source license and I have the
> right under that license to submit that work with modifications, whether
> created in whole or in part by me, under the same open source license (unless I
> am permitted to submit under a different license), as indicated in the file; or
>
> (c) The contribution was provided directly to me by some other person who
> certified (a), (b) or (c) and I have not modified it.
>
> (d) I understand and agree that this project and the contribution are public and
> that a record of the contribution (including all personal information I submit
> with it, including my sign-off) is maintained indefinitely and may be
> redistributed consistent with this project or the open source license(s)
> involved.

A pull request is checked for the sign-off. To add a missing one to the commits
on your own branch, run `git rebase --signoff origin/main` and push the branch again.
If an AI agent helped prepare a change, the person who submits it makes the
certification and is responsible for the result.

Contributions are licensed under the Apache License, Version 2.0, as the project
is. You keep your copyright. The license does not grant rights to the AdHelm name
or marks.

## Reporting a bug

Use the bug report issue template. It asks for the AdHelm version, the provider,
your platform, and the output of `adhelm doctor`. `adhelm doctor` output is
designed to be safe to share, but read it before you post it. Never paste a
credential, token, account or property identifier, customer data or a raw
provider response into an issue.
