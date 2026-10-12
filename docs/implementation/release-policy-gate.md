# Release-policy gate (ticket O3.2)

Decision: [ADR 0013](../adr/0013-open-source-release.md) decisions 7 and 9.
Gate: [`release-0.1.0.md`](../release-0.1.0.md#writes-refused).

AdHelm 0.1.0 ships its write code and refuses it at runtime. One module,
`src/adhelm/release_policy.py`, is the only place that decides the refusal. It
replaces the configured `auditor`/`operator` edition (`src/adhelm/edition.py`,
`Configuration.edition`, `OPERATOR_EDITION_REQUIRED`), which are deleted.

## The gate

`require_writes_released()` takes no argument and raises `WRITES_NOT_RELEASED`,
exit `3` (`ExitCode.PRECONDITION`), with a next action that names the 0.1.0
release policy and the manual alternative. It reads no configuration, environment
variable, flag or file, and `tests/test_release_policy.py` pins that: the module
imports only `functools`, `collections.abc`, `typing`, `adhelm.errors` and, lazily,
the CLI's own failure envelope.

`remote_mutation_leaf("<command path>")` declares a CLI command as a remote
mutation. The declaration does three things:

- it runs the gate before the command body, so a declared command refuses before
  it parses its input, loads configuration, checks the output bound, resolves a
  native bridge, opens a ledger, reads a credential or opens an egress path;
- it registers the path, which `discover` renders as
  `release_policy.unreleased_commands` with status `UNRELEASED`;
- it tags the registered callback, so a test finds every declared leaf from the
  Typer command tree instead of from a hand list.

Declared leaves in this build: `execute openai-pause`,
`execute ga4-key-event-create`, `execute meta-campaign-pause`, `execute plan`
and `recover attempt`. `execute plan` is the only route to the merged Google Ads
PAUSE/CREATE/ACTIVATE/UPDATE, Meta CREATE and OpenAI CREATE adapters;
`recover attempt` obtains an approval and reads through the same adapters, so it
is gated too. `recover catalog` is a local history read and stays available.

## What stays available

Compile, plan and preview commands (`compile *`, `plan *`, `artifacts plan`,
`artifacts ga4-key-event-plan`, `artifacts meta-campaign-pause`,
`artifacts google-ads-campaign-pause`) have no remote effect: none of them
changes a provider account. They are not all offline. Some, such as
`plan google-ads-campaign-pause create` and `plan meta-campaign-pause create`,
read a fixed baseline from the provider with credentials and HTTP to build the
plan. `discover` lists them under
`release_policy.planning_without_remote_effect` with status `AVAILABLE`,
execution `UNRELEASED` and `remote_reads: SOME_COMMANDS_READ_PROVIDER_STATE`.
The `execution_command` field their outputs and static artifacts already carried
now reads `UNRELEASED` (it read `DEVELOPMENT_ONLY_SETUP_REQUIRED`), so every
plan or preview states that execution is unreleased.

`execution_plan.execution_refusal_gates` and `attempt_recovery.refusal_gates`
now begin with `RELEASE_POLICY_WRITES_RELEASED` (never satisfied), and the
derived first refusal code is `WRITES_NOT_RELEASED`.

## Scope and limits

The gate is called in four places, each through the module attribute
(`release_policy.require_writes_released()`), so the `simulate_released_writes`
fixture substitutes all of them at once and nothing in the installed package,
configuration, environment or command line can:

- the CLI decorator on each declared leaf, which refuses before input,
  configuration or data-directory work;
- the entry of each retained workflow that used to check the edition:
  `execute_retained_execution_plan_with_installed_approval`,
  `record_retained_attempt_recovery_with_installed_approval`,
  `execute_retained_openai_pause_with_installed_approval`,
  `execute_retained_ga4_key_event_create` and its `_with_installed_approval`
  sibling, and `execute_retained_meta_campaign_pause_with_installed_approval`;
- `SafetyKernel.execute` and `SafetyKernel.record_attempt_recovery`, before the
  plan is revalidated, a coordinator lock is taken or the journal is written.
  Every adapter egress passes the kernel, so a route that is not a CLI command
  (a future command, a skill or agent harness importing the package, a helper the
  command scan does not follow) still reaches no `APPLY_STARTED`.

`tests/test_release_policy.py` calls the kernel and every workflow entry directly
with a real retained plan and checks `WRITES_NOT_RELEASED`, an unchanged ledger and
no adapter, credential or lock use. A structural test pins that each
`execute_retained_*`/`record_retained_*` function opens with the gate call and that
the set of modules building a `SafetyKernel` is the known one. The command scan
(every registered command that reaches a workflow is declared) remains as a
backstop; it only follows helpers in a callback's own module, which is why the
kernel and workflow checks exist.

- Tests of the withheld bodies (the kernel, the retained workflows and the
  command bodies) apply the `simulate_released_writes` fixture from
  `tests/conftest.py` through a module-level
  `pytestmark = pytest.mark.usefixtures("simulate_released_writes")`. The fixture
  substitutes the gate function for one test. Nothing in the installed package,
  configuration, environment or command line can do that.
  `test_release_policy.py` never uses it.
- The `SafetyKernel` no longer re-reads configuration to check an edition at
  each step. The release policy is a property of the build, not of the data
  directory, so it is checked once at kernel entry and cannot change between
  steps.
- A stored `config.json` that still carries an `edition` key is rejected by the
  closed `Configuration` schema (`CONFIGURATION_INVALID`). There is no
  migration: the rename ticket (O4.1) moved the default data directory (see
  [`rename-to-adhelm.md`](rename-to-adhelm.md)) and the owner re-enrolls.
- `digest(Configuration(...))` changed because the `edition` field left the
  model. The golden value in `tests/test_openai_account_key_profile.py` was
  recomputed; no other pinned digest depended on it.

## The word "edition"

No `edition`, `auditor` or `operator` identifier that means the product edition
remains in `src/` or `tests/`. The deliberate exceptions are:

- `SOURCE_EDITION_UNAVAILABLE` (`src/adhelm/reporting.py`, `src/adhelm/audit.py`) is a
  schema-visible missing-data status about a provider data source's own product
  tier. It is unrelated to the removed product edition and is kept.
- Negative assertions in `tests/test_release_policy.py` that the configuration
  schema, environment variables, flags and `discover` carry no edition, and the
  comment in `tests/test_openai_account_key_profile.py` that records why the
  golden `Configuration` digest was recomputed.

## Releasing writes later

Lifting the gate is a source change to this module together with the approval
host work ADR 0013 decision 8 defers. It is not a setting.
