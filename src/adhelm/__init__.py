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
#: Lifecycle: the build session sets `READY_FOR_USER_AUTHORIZATION` and stops there. That is the
#: value in the tree the owner exports. In the commit the owner tags as `v0.1.0` (owner action 6
#: in `docs/implementation/oss-0.1.0-plan.md`) they set this to `RELEASED_0_1_0`, so the wheel
#: published to PyPI reports the released state and not a pre-release one. Doing so changes two
#: pins to `RELEASED_0_1_0`: `test_the_build_reports_the_state_it_stops_at` in
#: `tests/test_release_state.py` and the `release_state` assertion in `tests/test_core.py`.
RELEASE_STATE: Final = "READY_FOR_USER_AUTHORIZATION"
