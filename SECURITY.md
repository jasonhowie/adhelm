# Security policy

AdHelm handles advertising-account credentials and account data, so security
reports are taken seriously.

## Reporting a vulnerability

Report a vulnerability privately through GitHub private vulnerability reporting:

<https://github.com/jasonhowie/adhelm/security/advisories/new>

Do not open a public issue, pull request or discussion for a suspected
vulnerability, and do not post it in any other public place.

A useful report includes:

- the AdHelm version (`adhelm version`), your platform and Python version;
- what you expected, what happened, and the smallest steps that reproduce it;
- the impact you believe it has, for example "a credential appears in the
  history export".

Do not send real credentials, tokens, account or property identifiers, customer
data or raw provider responses. Use placeholders or synthetic data. If a
reproduction needs a secret, say so and describe it instead.

## What is in scope

Vulnerabilities in the code and documentation in this repository, in particular:

- a way to make AdHelm perform a remote write in a build that has not released
  writes, including a bypass of the `WRITES_NOT_RELEASED` release-policy gate;
- a credential, token or other secret reaching output, logs, plans, evidence,
  the ledger, exports, artifacts or a command line;
- a failure of redaction or of the sharing boundary for output an agent reads;
- a way to forge, replay, reorder or silently alter the append-only history, or
  to make `history verify` pass over altered history;
- unsafe handling of input files, output paths, symlinks or the credential store;
- a way for provider-supplied or agent-supplied text to cause an action the user
  did not authorize; and
- a vulnerable dependency that affects AdHelm in a way a user can reach.

The [threat model](docs/threat-model.md) lists the assets and trust boundaries.

## What is out of scope

- Vulnerabilities in a provider's API, in your AI agent or its model, or in other
  software you run. Report them to their owners. AdHelm cannot control what a
  provider does with the requests you send it, or what your agent does with the
  output it reads.
- An attacker who already controls your operating-system account or can replace
  your local files. The threat model states what AdHelm does and does not defend
  against there.
- Results of missing provider configuration or of using a provider's access tier
  contrary to its terms.

## What to expect

AdHelm is maintained by volunteers, so there is no service-level agreement.
Reports are acknowledged, assessed and fixed on a best-effort basis, and the
advisory thread keeps you informed. We ask that you give us a reasonable time to
release a fix before you disclose publicly. We publish a GitHub security advisory
when a fix ships, and we credit you if you wish.

## Supported versions

Security fixes go to the latest release. Until a `1.0.0` release exists, that is
the latest `0.x` release.

## Testing in good faith

Test only against accounts you own or administer, and only with read access. Do
not access, modify or retain other people's data, and do not degrade a
provider's service. The maintainers will not take action against a researcher who
follows this policy and reports in good faith. This policy cannot grant
permission on behalf of a provider, whose terms still apply to your account.
