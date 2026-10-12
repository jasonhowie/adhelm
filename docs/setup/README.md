# Provider setup guides

One guide per provider, each covering the developer app or key you create, the scopes
and access tiers that matter for read-only use, the redirect or callback
configuration, the exact `adhelm` commands to enroll and verify, and how to revoke.
They follow the bring-your-own-app rule in
[ADR 0013](../adr/0013-open-source-release.md): AdHelm has no vendor app, so every user
registers their own.

| Guide | Provider | Status |
| --- | --- | --- |
| [`google_ads.md`](google_ads.md) | Google Ads | Written |
| [`ga4.md`](ga4.md) | GA4 (Data API and Admin API) | Written |
| [`gtm.md`](gtm.md) | Google Tag Manager | Written |
| [`meta_ads.md`](meta_ads.md) | Meta Ads | Written |
| [`linkedin_ads.md`](linkedin_ads.md) | LinkedIn Ads | Written |
| [`tiktok_ads.md`](tiktok_ads.md) | TikTok Ads | Written |
| [`reddit_ads.md`](reddit_ads.md) | Reddit Ads | Written |
| [`openai_ads.md`](openai_ads.md) | OpenAI Ads | Written |

The file name is the provider ID `adhelm discover` uses (`google_ads`, not `google-ads`).
The providers differ in how a credential reaches AdHelm, and each guide follows its own:

| Provider | How enrollment works |
| --- | --- |
| Google Ads, GA4, GTM | Browser consent with your own Google Cloud OAuth client, through a loopback listener on a random port |
| LinkedIn Ads | Browser consent with your own LinkedIn app, through a loopback listener on a port you register |
| Reddit Ads | Browser consent with your own developer application, whose ID and secret you type at hidden prompts, through a loopback listener on a port you register |
| TikTok Ads | You complete TikTok's authorization yourself and paste the one-hour code into a hidden prompt; no browser handoff, no listener |
| Meta Ads | You generate two access tokens in Meta's tools and paste each into a hidden prompt; no OAuth flow |
| OpenAI Ads | You issue an account key in Ads Manager and paste it into a hidden prompt; no OAuth flow |

## How a guide is tested

A guide is executable. `tests/test_setup_guides.py` parses each guide with
`tests/guide_harness.py` and runs every command in a `guide:run` block, in order,
through the real command line application against a fake provider
(`tests/guide_fakes.py`). The fake replaces only what would leave the process: the
operating-system credential store, the system browser, the loopback listener that would
receive the browser's redirect, the terminal check for a browser handoff (AdHelm's own
check that the command asked for `--interactive` still applies), the hidden secret prompt
and the provider's HTTP APIs, including its token endpoint. The real token transport and
read adapters run against it. No test reaches a provider, a real credential store or a
real data directory, and the suite's network block stays on.

The harness fails a guide that:

- shows an `adhelm` command that exits differently from what the guide says, or that
  uses a flag or file the command does not have;
- prints a credential (access token, refresh token, client secret, developer token) in
  any command result, or leaves one in the data directory;
- sends the fake provider a request a read-only guide must never send (a host outside
  the provider's API, a method other than `GET`, or a `POST` that is not a documented
  read-only query or the provider's token endpoint), or one the fake refuses: no
  credential the enrollment issued or the guide stored, a token granted for a different
  scope than the API needs, an authorization or token request for a client, redirect,
  code or secret the reader did not enter, a code used twice, or a Google Ads request
  whose `developer-token` header is not the value the guide stored (stricter than
  Google, which ignores the header; it proves the stored slot is the one sent);
- runs a command outside the enroll-and-read allowlist (`GUIDE_COMMANDS`), including
  anything that plans, compiles, executes or recovers, or a command that is a declared
  remote mutation;
- contains the shape of a real credential, a shell operator, a `--data-dir` flag, or an
  `adhelm` command in a code block the harness does not run;
- lacks any section the [release definition](../release-0.1.0.md#bring-your-own-app-setup)
  requires, a `Sources` section, or a fetch date on any cited page;
- links to a page that is not in `Sources`, or to a repository file that does not exist.

Only `guide:run` blocks are executed. A command named in running prose, such as
`adhelm auth google-oauth reconnect` or `adhelm schema ga4-report`, is not run and
nothing keeps it from drifting; a guide author checks each one by hand against
`adhelm --help` when writing it.

The harness cannot check a provider's console or a live account. Console steps are
therefore cited to the provider's own documentation, each source with the date it was
fetched, and the guide says where AdHelm's behavior and the provider's documentation
differ.

## Writing a guide

Required sections, in any order with `Sources` last: `Create the developer app`,
`Scopes`, `Redirect and callback`, `Access tiers and limits`, `Enroll`, `Verify`,
`Revoke`, `Sources`. A `Sources` entry reads
`- [Title](https://...) (fetched YYYY-MM-DD; page last updated YYYY-MM-DD)`.

A fenced block is executable when its info string carries a `guide:` word, starting in
the first column:

````text
```sh guide:run
adhelm init
adhelm doctor # exit 3
```
````

- `guide:run`: one `adhelm ...` command per line, run in order. The harness prepends
  `--data-dir` for a temporary directory, so do not pass it. Keep each command on one
  line (a line continuation breaks in PowerShell). A trailing `# exit N` says the command
  is expected to exit `N`; the default is `0`.
- `guide:file=NAME`: a file the reader creates in the working directory, such as a
  profile draft. Its content is written verbatim before the commands after it run, so
  the text a reader copies is the text that was tested.
- `guide:expect`: a JSON object the previous command's result must contain. Objects match
  on the keys given, a list matches when each element given matches some element of the
  result, and anything else must be equal. State stable fields only; do not copy digests,
  timestamps or counts that depend on the fake.

Files the reader gets from the provider, such as the downloaded OAuth client file, are
supplied by the provider fake and must be used by a command in the guide. A profile the
reader writes, such as the draft or profile JSON in most guides, is a `guide:file`.

Where a provider's console or documentation and AdHelm's behavior differ, say so in the
guide. A guide may not claim a console step without citing the page that documents it,
and where the pages do not document something a reader needs, such as the account's
role requirements or whether a provider accepts a loopback redirect, it says that the
pages are silent rather than filling the gap. Commands a guide shows that AdHelm
refuses by design, such as `auth tiktok-token refresh` in the TikTok guide, belong in a
`guide:run` block with their exit code, so the guide fails when the behavior changes.

To add a provider, subclass `FakeProvider` in `tests/guide_fakes.py` (its credential
store, hidden prompt, HTTP boundary and request log are shared; each subclass adds the
provider's own authentication and the seams its enrollment would use to leave the
process) and register the fake in `FAKES`, add the hosts, `POST` paths and
documentation sites the guide may touch to the tables in `tests/test_setup_guides.py`,
add any new command it needs to `GUIDE_COMMANDS` in review, add the shapes of the
provider's real credentials to `_CREDENTIAL_SHAPES` in `tests/guide_harness.py` (the
fake supplies the values it hands out through `secrets()`, but the guide text is
checked against the shapes before any fake exists), and write the guide.
