"""Deterministic local control plane. No startup network or model dependencies."""

from typing import Final

__version__ = "0.1.0"

#: The gate state this build reports in `adhelm version`, `discover` and `doctor` and in
#: `scripts/check_repository.py`. The states are defined in the "Gate states" table of
#: `docs/release-0.1.0.md`. Change it here and nowhere else: a test fails on any other module
#: under `src/adhelm`, or `scripts/check_repository.py`, that spells a gate state. The
#: development-evidence scripts (`validate_development`, `validate_native_approval`,
#: `traceability`) report the v1.0.0 definition-of-done state and do not follow this constant.
#:
#: Lifecycle: the 0.1.0 build stopped at `READY_FOR_USER_AUTHORIZATION`, and the published 0.1.0
#: wheel still reports that value because the flip to `RELEASED_0_1_0` was missed before the
#: tag. 0.1.0 was published to PyPI on 2026-10-12, so the tree now reports `RELEASED_0_1_0`.
#: Two tests pin it: `test_the_tree_reports_the_released_state` in
#: `tests/test_release_state.py` and the `release_state` assertion in `tests/test_core.py`.
RELEASE_STATE: Final = "RELEASED_0_1_0"
