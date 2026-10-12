# Installed smoke exit-code regression

The exact M37 source commit `9a073d10180b85a4280457c189e5366ced3d54cb`
passed formatting, lint, strict typing, the full Python suite, the ultra regression
suite, the repository contract check, and reproducible wheel/source builds.
Validation then stopped at the new installed execution refusal smoke.

The CLI correctly returned `NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` with
`ExitCode.PRECONDITION` (integer value 3). The smoke wrapper used an exact
`type(...) is int` check and incorrectly rejected this `IntEnum`. The repair
accepts integer subclasses and continues to reject booleans, floats, wrong
values, and unexpected normal returns. Two focused subprocess regressions
exercise a matching and mismatching integer enum.

The original console output and its digest are preserved in
`/private/tmp/adhelm-m37-installed-smoke-harness-failure.json`. That failed run is
not represented as a completed validation. A new exact-commit validation must
finish the installed checks. Only unconfigured default bridge source and wheels
are executed by this validator; configured artifacts remain inspection-only.

Review note, 2026-09-06: the `d8c9ecd` wrapper change preserves exact numeric
exit matching while admitting `IntEnum` values and rejecting the `bool` and
floating-point aliases that Python otherwise treats as comparable integers. The
matching and mismatching enum subprocess cases cover the intended boundary; no
acceptance bypass was found.
