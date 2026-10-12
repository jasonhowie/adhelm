# Repeatable development validation

Run `scripts/validate_development.py` using the locked Python development
environment, with `uv` already installed and the required build dependencies
cached. The script defaults to `HEAD`; `--revision` resolves to one exact commit.
It creates a fresh local output directory and archives that commit, so uncommitted
work is excluded. `--output` requires a new destination. The default reproducible
timestamp is the source commit time; `--source-date-epoch` can select an explicit
release-engineering timestamp.

`--pytest-workers` (default `1`), `--pytest-timeout` (default `900` seconds) and
`--ultra-timeout` (default `450` seconds) select the test parallelism and the two
pytest deadlines; each must be an integer of at least 1. More than one worker adds
`-n <workers> --dist=loadfile` to both the full-suite and ultra-regression runs, as
CI already does; one worker keeps the exact serial commands. The chosen values are
part of the evidence: `validation.json` records `pytest_configuration`, the measured
`check_seconds` per check, and the `pytest_summary` counts parsed from the full-suite
summary line (`null` when no summary line is present). A run that exceeds a deadline
raises before any record is written and is recorded as INCOMPLETE/TIMEOUT, never as a
pass; choose an appropriate deadline for the suite instead of reinterpreting a timeout.

The isolated source import, Ruff, format check, strict mypy, full test suite,
independent ultra regressions, and repository contract check must all pass.
Two offline wheel/source builds must match byte-for-byte. The wheel is then
installed without dependency changes into a separate local directory; validation
checks actual installed discovery, compiler schema, packaged typing marker,
and every named guidance section from both bundled skills.

The output also includes a CycloneDX 1.5 runtime-lock graph exported by `uv`,
bound to the exact source commit, lockfile SHA-256, and reproducible timestamp.
Its deterministic serial number identifies those inputs. This describes the
cross-platform Python runtime lock graph; it is not the inventory of a standalone
executable, operating-system image, or future bundled native libraries. It is
neither a license audit nor a vulnerability scan. Release artifacts still need
their own final SBOM, dependency/license scans, provenance, signing, and platform
evidence.

The separate offline dependency-license scanner also runs twice against the
archived lock and version-matched installed metadata. Both outputs must match
exactly and carry the archived lock hash. Validation records the evidence hash,
complete graph counts and explicit review-required states. This neither turns
metadata into verified wheel provenance nor makes a legal compatibility decision.
See [M21 license evidence](dependency-license-evidence-m21.json).

Only after all checks succeed does the script write `validation.json`. It reports
`IMPLEMENTING` and no signing/publication. It never commits, tags, signs, publishes,
creates customer configuration, accesses provider credentials, or performs a
provider workflow. The archived test suite blocks socket connections by default.
Development tooling must already be present; the script does not download a
toolchain or refresh the lockfile.

`scripts/validate_native_approval.py` independently archives the requested exact
commit on macOS, builds the Swift foundation product and executes its XCTest
suite. It cross-checks every completed case against the final suite count and
hashes the retained transcript. The native fixture set includes the Python
model's seven-field approval configuration, canonical bytes and digest at
minimum/default/maximum limits and with Unicode references. The Python suite
regenerates those fixtures independently, while Swift consumes them.

Native validation also executes the unconfigured foundation twice, with default
launch arguments and synthetic runtime overrides. Each invocation must finish
within five seconds with exit 78, no stdout and the exact unconfigured-host
diagnostic. Its separate hashed transcript proves those refusals only. This
check does not start an installed approval listener, enroll a key, prompt for OS
authentication, or verify a deployed host's code signature. Failed build/test
transcripts and failed launch results are retained before the validator raises;
none produces a passing validation record.

The native validator also builds the separate installed-host and Core-only
build-inspector targets. Before executing either, it requires the exact hash
of the checked-in unconfigured metadata source. Eight launch cases exercise
default and synthetic environment input, with and without arguments: no-argument
launches must refuse with exit 78; supplied arguments must refuse with exit 64.
The configured artifact host is never executed by this validator. Its separate
build workflow uses only the Core-only inspector to compare compiled metadata.
See [native artifact architecture](../adr/0003-native-approval-host-artifacts.md).
