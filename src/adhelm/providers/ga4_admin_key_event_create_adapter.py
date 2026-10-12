"""Guarded, fixed GA4 Admin key-event CREATE adapter.

This internal library adapter is deliberately narrower than a provider client:
it accepts only one already compiled GA4 CREATE action through the private
configured factory below.  The factory resolves one protected OAuth snapshot
inside this module, retains the bearer only in a nonserializable runtime object,
and creates a fixed-host transport.  No CLI or public caller can provide a
bearer, arbitrary endpoint, request body, or generic transport.

The adapter remains development-fixture-only.  Its local OAuth association and
resource GETs do not prove a current Google CREATE grant, reservation, source
semantics, browser firing, processed reporting, or user authorization.  The
SafetyKernel supplies the latter's exact one-use egress permit.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Never

import httpx

from adhelm.canonical import digest
from adhelm.credentials import export_key
from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_admin_execution_observation import (
    validate_ga4_admin_edit_execution_observation,
)
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput
from adhelm.google_oauth_execution_binding import (
    Ga4AdminEditOAuthExecutionBinding,
    bearer_for_ga4_admin_edit,
    resolve_ga4_admin_edit_execution_binding,
)
from adhelm.models import Profile
from adhelm.providers.ga4_admin import Ga4AdminKeyEvent, _json, _parse_ga4_admin_key_event
from adhelm.providers.ga4_admin_create_state import (
    project_ga4_admin_edit_key_event_semantic_collection,
)
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
    Ga4AdminEditStandardKeyEventCapacity,
    revalidate_ga4_admin_edit_key_event_inventory,
)
from adhelm.providers.ga4_create_contract import (
    GA4_KEY_EVENT_CREATE_HOST,
    Ga4KeyEventCreatePlannedAction,
    revalidate_ga4_key_event_create_planned_action,
)
from adhelm.safety import (
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    EffectStatus,
    OperationKind,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    SubPlan,
    Verification,
)
from adhelm.transport import SafeHttpClient

if TYPE_CHECKING:
    from adhelm.ledger import Ledger

_HOST: Literal["analyticsadmin.googleapis.com"] = GA4_KEY_EVENT_CREATE_HOST
_API_VERSION: Literal["v1alpha"] = "v1alpha"
_EDIT_SCOPE: Literal["https://www.googleapis.com/auth/analytics.edit"] = (
    "https://www.googleapis.com/auth/analytics.edit"
)
_KEY_EVENT_FIELDS = "name,eventName,createTime,deletable,custom,countingMethod,defaultValue"
_RUNTIME_ISSUER = object()


def _now() -> datetime:
    return datetime.now(UTC)


def _clock_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception:
        raise AdhelmError("CLOCK_INVALID", "GA4 CREATE clock is unavailable.") from None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise AdhelmError("CLOCK_INVALID", "GA4 CREATE clock is unavailable.")
    return value.astimezone(UTC)


def _require_fingerprint_key(value: object) -> bytes:
    if type(value) is not bytes or not 32 <= len(value) <= 4_096:
        raise AdhelmError(
            "FINGERPRINT_KEY_REQUIRED",
            "GA4 CREATE requires the current private local fingerprint key.",
            exit_code=ExitCode.PRECONDITION,
        )
    return value


def _safe_bearer(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 16_384
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GA4 Admin EDIT authorization is unavailable.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Authorize the configured local GA4 Admin EDIT profile.",
        )
    return value


def _validate_current_sources(
    ledger: Ledger,
    action: Ga4KeyEventCreatePlannedAction,
    *,
    fresh: Ga4KeyEventCandidateInput,
    fingerprint_key: bytes,
    now: datetime,
) -> None:
    """Late import keeps the pure action contract independent of ledger code."""

    from adhelm.ga4_create_execution_sources import validate_current_ga4_create_sources

    validate_current_ga4_create_sources(
        ledger,
        action,
        fresh=fresh,
        fingerprint_key=fingerprint_key,
        now=now,
    )


class _ConfiguredFacts:
    """Private current configuration/profile facts; no bearer is stored here."""

    __slots__ = (
        "profile",
        "configuration_digest",
        "profile_digest",
        "target",
        "profile_ref",
        "account_ref",
        "principal_ref",
        "client_ref",
    )

    def __init__(
        self,
        *,
        profile: Profile,
        configuration_digest: str,
        profile_digest: str,
        target: Ga4AdminEditInventoryTarget,
        profile_ref: str,
        account_ref: str,
        principal_ref: str,
        client_ref: str,
    ) -> None:
        self.profile = profile
        self.configuration_digest = configuration_digest
        self.profile_digest = profile_digest
        self.target = target
        self.profile_ref = profile_ref
        self.account_ref = account_ref
        self.principal_ref = principal_ref
        self.client_ref = client_ref

    def __repr__(self) -> str:
        return "<_ConfiguredFacts redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("GA4 CREATE configuration facts cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("GA4 CREATE configuration facts cannot be serialized")


def _current_configured_facts(
    directory: Path, action: Ga4KeyEventCreatePlannedAction
) -> _ConfiguredFacts:
    """Read and compare only the configured nonsecret GA4 EDIT binding."""

    try:
        configuration, profile, target, configuration_digest = (
            validate_ga4_admin_edit_execution_observation(
                directory,
                str(action.execution_binding.auth_profile_ref),
                stream_id=action.execution_binding.target.web_stream_client_ref,
                expected_configuration_digest=action.execution_binding.configuration_digest,
            )
        )
        # The public observation validator already rejects every non-OAuth
        # profile; retain explicit exact comparisons at this new mutation edge.
        facts = _ConfiguredFacts(
            profile=profile,
            configuration_digest=configuration_digest,
            profile_digest=digest(profile),
            target=target,
            profile_ref=profile.profile_id,
            account_ref=profile.account_ref,
            principal_ref=profile.principal_ref,
            client_ref=profile.client_ref,
        )
        del configuration
        _assert_configured_facts(action, facts)
        return facts
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_RUNTIME",
            "GA4 CREATE configuration facts are invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _assert_configured_facts(
    action: Ga4KeyEventCreatePlannedAction, facts: _ConfiguredFacts
) -> None:
    binding = action.execution_binding
    account = action.plan.subplans[0].account
    expected_values = (
        (facts.configuration_digest, binding.configuration_digest),
        (facts.profile_digest, binding.profile_configuration_digest),
        (facts.profile_ref, str(binding.auth_profile_ref)),
        (facts.account_ref, account.stable_account_id),
        (facts.principal_ref, str(binding.principal_ref)),
        (facts.client_ref, str(binding.client_application_ref)),
    )
    if facts.target != binding.target or not all(
        type(actual) is str and hmac.compare_digest(actual, expected)
        for actual, expected in expected_values
    ):
        raise AdhelmError(
            "ACCOUNT_IDENTITY_DRIFT",
            "The configured GA4 EDIT identity differs from the approved plan.",
            exit_code=ExitCode.PRECONDITION,
        )


def _resolve_runtime_bearer(facts: _ConfiguredFacts, action: Ga4KeyEventCreatePlannedAction) -> str:
    """Consume exactly one lifecycle-issued private OAuth handoff."""

    try:
        binding = resolve_ga4_admin_edit_execution_binding(facts.profile)
        if type(binding) is not Ga4AdminEditOAuthExecutionBinding:
            raise ValueError("binding type")
        expected = (
            (binding.profile_ref, facts.profile_ref),
            (binding.profile_digest, facts.profile_digest),
            (binding.provider, "ga4_admin"),
            (binding.purpose, "EDIT"),
            (binding.credential_source, "OS_OAUTH_GOOGLE"),
            (binding.account_ref, facts.account_ref),
            (binding.principal_ref, facts.principal_ref),
            (binding.client_ref, facts.client_ref),
            (binding.api_scope, _EDIT_SCOPE),
        )
        if not all(
            type(actual) is str and hmac.compare_digest(actual, expected_value)
            for actual, expected_value in expected
        ):
            raise ValueError("binding facts")
        _assert_configured_facts(action, facts)
        return _safe_bearer(bearer_for_ga4_admin_edit(binding, facts.profile))
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GA4 Admin EDIT authorization is unavailable.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Authorize the configured local GA4 Admin EDIT profile.",
        ) from None


class _ConfiguredGa4CreateRuntime:
    """Nonserializable private bearer/configuration context for one adapter."""

    __slots__ = (
        "_issuer",
        "_directory",
        "_action",
        "_ledger",
        "_fresh",
        "_fingerprint_key",
        "_bearer",
        "_facts",
        "_clock",
        "_closed",
    )

    def __init__(
        self,
        *,
        _issuer: object,
        directory: Path,
        action: Ga4KeyEventCreatePlannedAction,
        ledger: Ledger,
        fresh: Ga4KeyEventCandidateInput,
        fingerprint_key: bytes,
        bearer: str,
        facts: _ConfiguredFacts,
        clock: Callable[[], datetime],
    ) -> None:
        if _issuer is not _RUNTIME_ISSUER:
            raise TypeError("GA4 CREATE runtime is factory-issued only")
        self._issuer = _issuer
        self._directory = directory
        self._action = action
        self._ledger = ledger
        self._fresh = fresh
        self._fingerprint_key = fingerprint_key
        self._bearer = bearer
        self._facts = facts
        self._clock = clock
        self._closed = False

    def __repr__(self) -> str:
        return "<_ConfiguredGa4CreateRuntime redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("GA4 CREATE runtime cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("GA4 CREATE runtime cannot be serialized")

    def __copy__(self) -> Never:
        raise TypeError("GA4 CREATE runtime cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Never:
        del memo
        raise TypeError("GA4 CREATE runtime cannot be copied")

    @property
    def action(self) -> Ga4KeyEventCreatePlannedAction:
        self._require_open()
        return revalidate_ga4_key_event_create_planned_action(self._action)

    def _bearer_for_fixed_request(self) -> str:
        """Return the private bearer only to this module's fixed request paths."""

        self._require_open()
        return _safe_bearer(self._bearer)

    def _fingerprint_key_for_local_hmac(self) -> bytes:
        """Return the transient local key only to fixed local revalidation."""

        self._require_open()
        return self._fingerprint_key

    def current(self) -> _ConfiguredFacts:
        """Recheck all local retained/config/key facts outside every HTTP call."""

        self._require_open()
        action = self.action
        now = _clock_now(self._clock)
        _validate_current_sources(
            self._ledger,
            action,
            fresh=self._fresh,
            fingerprint_key=self._fingerprint_key,
            now=now,
        )
        facts = _current_configured_facts(self._directory, action)
        _assert_configured_facts(action, facts)
        try:
            current_key = export_key()
        except AdhelmError:
            raise
        except Exception:
            raise AdhelmError(
                "FINGERPRINT_KEY_UNAVAILABLE",
                "The current local fingerprint key is unavailable.",
                exit_code=ExitCode.PRECONDITION,
            ) from None
        if type(current_key) is not bytes or not hmac.compare_digest(
            current_key, self._fingerprint_key
        ):
            raise AdhelmError(
                "FINGERPRINT_KEY_CHANGED",
                "The local fingerprint key changed during GA4 CREATE execution.",
                exit_code=ExitCode.PRECONDITION,
            )
        self._facts = facts
        return facts

    def close(self) -> None:
        self._bearer = ""
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise AdhelmError("GA4_CREATE_RUNTIME_CLOSED", "GA4 CREATE runtime is closed.")


class Ga4AdminKeyEventCreateGuardedAdapter:
    """One fixed guarded GA4 CREATE capability for a private runtime context."""

    # ``Adapter.provider`` is mutable-protocol-compatible ``str`` even though
    # this concrete value is fixed and checked at every plan boundary.
    provider: str = "ga4_admin"

    def __init__(
        self, *, _issuer: object, runtime: _ConfiguredGa4CreateRuntime, transport: SafeHttpClient
    ) -> None:
        if _issuer is not _RUNTIME_ISSUER or type(runtime) is not _ConfiguredGa4CreateRuntime:
            raise TypeError("GA4 CREATE adapter is configured-factory-issued only")
        if transport.allowed_hosts != frozenset({_HOST}) or transport.client.params:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID",
                "GA4 CREATE requires its fixed Admin host and no default query parameters.",
                exit_code=ExitCode.INVALID_INPUT,
            )
        self._runtime = runtime
        self._transport = transport
        self._proof: tuple[str, ...] | None = None
        self._after_inventory: Ga4AdminEditKeyEventInventory | None = None

    @property
    def action(self) -> Ga4KeyEventCreatePlannedAction:
        """Exact typed action used by the configured workflow's root binding."""

        return self._runtime.action

    def _check_plan(self, plan: SubPlan) -> Ga4KeyEventCreatePlannedAction:
        action = self.action
        if plan != action.plan.subplans[0]:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH", "Adapter refuses a different GA4 CREATE plan."
            )
        return action

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        action = self._check_plan(plan)
        candidate = action.candidate.compilation.compiler_candidate
        return AdapterCapabilities(
            provider_api_version=candidate.target.api_version,
            capability_version=candidate.capability_version,
            supported_operations=(OperationKind.CREATE,),
            # This is an internal guarded capability exercised with fake HTTP.
            # It does not claim an installed user-facing live conformance path.
            writes_enabled=True,
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        action = self.action
        facts = self._runtime.current()
        account = action.plan.subplans[0].account
        if type(account_ref) is not str or not hmac.compare_digest(
            account_ref, account.account_ref
        ):
            raise AdhelmError("IDENTITY_MISMATCH", "GA4 account differs from the approved plan.")
        _assert_configured_facts(action, facts)
        return RemoteIdentity(
            stable_account_id=facts.account_ref,
            auth_profile_ref=facts.profile_ref,
            principal_ref=facts.principal_ref,
            client_application_ref=facts.client_ref,
            scopes=(_EDIT_SCOPE,),
        )

    def _inventory_reader(self) -> Ga4AdminEditKeyEventInventoryClient:
        return Ga4AdminEditKeyEventInventoryClient(
            self._transport,
            lambda: self._runtime._bearer_for_fixed_request(),
            fingerprint_key=self._runtime._fingerprint_key_for_local_hmac(),
        )

    def _read_inventory(
        self, action: Ga4KeyEventCreatePlannedAction
    ) -> Ga4AdminEditKeyEventInventory:
        self._runtime.current()
        inventory = self._inventory_reader().read_inventory(action.execution_binding.target)
        return revalidate_ga4_admin_edit_key_event_inventory(
            inventory, fingerprint_key=self._runtime._fingerprint_key_for_local_hmac()
        )

    @staticmethod
    def _projection_state(
        inventory: Ga4AdminEditKeyEventInventory, *, fingerprint_key: bytes
    ) -> tuple[RemoteState, int]:
        projection = project_ga4_admin_edit_key_event_semantic_collection(
            inventory, fingerprint_key=fingerprint_key
        )
        return (
            RemoteState(
                fingerprint=projection.state.fingerprint,
                revision=projection.state.revision,
            ),
            projection.member_count,
        )

    def _require_baseline(
        self, action: Ga4KeyEventCreatePlannedAction, inventory: Ga4AdminEditKeyEventInventory
    ) -> RemoteState:
        if digest(inventory) != action.observation_binding.inventory_digest:
            raise AdhelmError(
                "REMOTE_DRIFT", "GA4 key-event inventory differs from the approved baseline."
            )
        state, count = self._projection_state(
            inventory, fingerprint_key=self._runtime._fingerprint_key_for_local_hmac()
        )
        if (
            state.fingerprint != action.plan.subplans[0].before.fingerprint
            or state.revision != action.plan.subplans[0].before.revision
            or count != action.semantic_state.before_member_count
        ):
            raise AdhelmError(
                "REMOTE_DRIFT", "GA4 key-event semantic baseline differs from the approved plan."
            )
        return state

    def _require_expected_after(
        self,
        action: Ga4KeyEventCreatePlannedAction,
        inventory: Ga4AdminEditKeyEventInventory,
        expected: Ga4AdminEditKeyEventInventory,
    ) -> RemoteState:
        if inventory.target != expected.target or inventory.key_events != expected.key_events:
            raise AdhelmError(
                "REMOTE_DRIFT", "GA4 key-event read-back differs from the CREATE result."
            )
        if inventory.capacity != expected.capacity:
            raise AdhelmError(
                "REMOTE_DRIFT", "GA4 key-event read-back capacity differs from the CREATE result."
            )
        state, count = self._projection_state(
            inventory, fingerprint_key=self._runtime._fingerprint_key_for_local_hmac()
        )
        if (
            state.fingerprint != action.plan.subplans[0].after.fingerprint
            or state.revision != action.plan.subplans[0].after.revision
            or count != action.semantic_state.after_member_count
        ):
            raise AdhelmError(
                "REMOTE_DRIFT", "GA4 key-event semantic read-back differs from the approved plan."
            )
        return state

    def read_state(self, plan: SubPlan) -> RemoteState:
        action = self._check_plan(plan)
        inventory = self._read_inventory(action)
        if self._after_inventory is not None:
            return self._require_expected_after(action, inventory, self._after_inventory)
        return self._require_baseline(action, inventory)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError("PERMIT_REQUIRED", "GA4 CREATE egress requires a kernel-issued permit.")

    def _kernel_started(self, plan: SubPlan) -> bool:
        return any(
            record.get("payload", {}).get("event_type") == "APPLY_STARTED"
            and record.get("payload", {}).get("plan_digest") == self.action.plan.plan_digest
            and record.get("payload", {}).get("subplan_digest") == plan.subplan_digest
            for record in self._runtime._ledger.records("changes")
        )

    def _parse_key_event_response(
        self, response: httpx.Response, action: Ga4KeyEventCreatePlannedAction
    ) -> Ga4AdminKeyEvent:
        try:
            payload = _json(response, "GA4 key-event CREATE response")
            compiler = action.candidate.compilation.compiler_candidate
            parsed = _parse_ga4_admin_key_event(
                payload,
                property_id=compiler.identity.property_client_ref,
                property_ref=compiler.identity.property_ref,
                fingerprint_key=self._runtime._fingerprint_key_for_local_hmac(),
                reference_for=lambda kind, value: (
                    f"ga4_admin.key_event.{value}"
                    if kind == "key_event"
                    else (_ for _ in ()).throw(ValueError("unexpected reference kind"))
                ),
            )
            request = compiler.request
            # The v1 Boolean/no-monetary candidate contract cannot carry a
            # default value. Do not float-round a branch that is unsupported
            # by the retained action semantics.
            if request.defaultValue is not None or parsed.default_value is not None:
                raise ValueError("CREATE response default value is unsupported")
            if (
                parsed.event_name_fingerprint
                != _parse_ga4_admin_key_event(
                    {
                        "name": (
                            f"properties/{compiler.identity.property_client_ref}"
                            f"/keyEvents/{parsed.resource_client_ref}"
                        ),
                        "eventName": request.eventName,
                        "deletable": parsed.deletable,
                        "custom": parsed.custom,
                        "countingMethod": request.countingMethod,
                        **(
                            {"defaultValue": request.defaultValue.model_dump(mode="json")}
                            if request.defaultValue is not None
                            else {}
                        ),
                    },
                    property_id=compiler.identity.property_client_ref,
                    property_ref=compiler.identity.property_ref,
                    fingerprint_key=self._runtime._fingerprint_key_for_local_hmac(),
                    reference_for=lambda kind, value: f"ga4_admin.key_event.{value}",
                ).event_name_fingerprint
                or parsed.counting_method != request.countingMethod
            ):
                raise ValueError("CREATE response semantic mismatch")
            return parsed
        except AdhelmError:
            raise
        except Exception:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "GA4 CREATE response is invalid.",
            ) from None

    def _get_returned_key_event(
        self, action: Ga4KeyEventCreatePlannedAction, returned_id: str
    ) -> Ga4AdminKeyEvent:
        compiler = action.candidate.compilation.compiler_candidate
        if compiler.request.defaultValue is not None:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "GA4 CREATE action contains an unsupported default value.",
            )
        response = self._transport.request(
            "GET",
            f"https://{_HOST}/{_API_VERSION}/properties/{compiler.identity.property_client_ref}/keyEvents/{returned_id}",
            headers={"authorization": f"Bearer {self._runtime._bearer_for_fixed_request()}"},
            params={"fields": _KEY_EVENT_FIELDS},
        )
        return self._parse_key_event_response(response, action)

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        action = self._check_plan(plan)
        if not self._kernel_started(plan):
            raise AdhelmError(
                "KERNEL_START_REQUIRED", "GA4 CREATE egress requires the kernel's durable start."
            )
        before = self._read_inventory(action)
        self._require_baseline(action, before)
        if action.plan.expires_at <= _clock_now(self._runtime._clock):
            raise AdhelmError(
                "PLAN_EXPIRED",
                "GA4 CREATE plan expired before provider egress.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        # This final local recheck happens immediately before permit consumption
        # and therefore before the fixed POST. It intentionally holds no ledger
        # or configuration lock through provider I/O.
        self._runtime.current()
        compiler = action.candidate.compilation.compiler_candidate
        if compiler.request.defaultValue is not None:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "GA4 CREATE action contains an unsupported default value.",
            )
        permit.consume(
            plan_digest=action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        response = self._transport.request(
            "POST",
            f"https://{_HOST}{compiler.target.path}",
            headers={"authorization": f"Bearer {self._runtime._bearer_for_fixed_request()}"},
            json=compiler.request.model_dump(mode="json", warnings="error"),
            mutation=True,
        )
        returned = self._parse_key_event_response(response, action)
        fetched = self._get_returned_key_event(action, returned.resource_client_ref)
        if fetched != returned:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "GA4 CREATE returned-resource read-back differs from the POST result.",
            )
        after = self._read_inventory(action)
        expected_members = tuple(
            sorted((*before.key_events, returned), key=lambda item: item.resource_ref)
        )
        if (
            after.target != before.target
            or after.key_events != expected_members
            or not isinstance(after.capacity, Ga4AdminEditStandardKeyEventCapacity)
            or not isinstance(before.capacity, Ga4AdminEditStandardKeyEventCapacity)
            or after.capacity.complete_observed_key_event_total
            != before.capacity.complete_observed_key_event_total + 1
        ):
            raise AdhelmError(
                "REMOTE_DRIFT",
                "GA4 CREATE full collection read-back differs from the expected result.",
            )
        state, count = self._projection_state(
            after, fingerprint_key=self._runtime._fingerprint_key_for_local_hmac()
        )
        if (
            state.fingerprint != plan.after.fingerprint
            or state.revision != plan.after.revision
            or count != action.semantic_state.after_member_count
        ):
            raise AdhelmError(
                "REMOTE_DRIFT", "GA4 CREATE semantic read-back differs from the approved plan."
            )
        self._after_inventory = after
        response_ref = "evidence:sha256:" + digest(
            {"kind": "ga4_create_response", "after": state.fingerprint}
        )
        returned_ref = "evidence:sha256:" + digest(
            {"kind": "ga4_create_returned_get", "after": state.fingerprint}
        )
        list_ref = "evidence:sha256:" + digest(
            {"kind": "ga4_create_full_list", "after": state.fingerprint, "count": count}
        )
        self._proof = (response_ref, returned_ref, list_ref)
        return ApplyResponse(
            outcome=ApplyOutcome.CONFIRMED,
            opaque_resource_refs=(
                "opaque:sha256:"
                + digest({"kind": "ga4_created_key_event", "after": state.fingerprint}),
            ),
            evidence_refs=self._proof,
        )

    def verify(self, plan: SubPlan) -> Verification:
        self._check_plan(plan)
        return Verification(verified=self._proof is not None, evidence_refs=self._proof or ())

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self._check_plan(plan)
        try:
            state = self.read_state(plan)
        except Exception:
            state = RemoteState(fingerprint="unavailable")
        # GA4 CREATE has no documented idempotency or conditional-create proof;
        # a later matching collection never attributes a lost response to this run.
        return Reconciliation(
            state=state,
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )


@contextmanager
def _configured_ga4_create_adapter(
    directory: Path,
    action: Ga4KeyEventCreatePlannedAction,
    ledger: Ledger,
    *,
    fresh: Ga4KeyEventCandidateInput,
    fingerprint_key: bytes,
    clock: Callable[[], datetime] = _now,
) -> Iterator[Ga4AdminKeyEventCreateGuardedAdapter]:
    """Create one private configured guarded adapter.

    This is intentionally underscore-private: callers cannot inject bearer
    material, a generic HTTP client, or a mutable runtime context.  It does no
    remote mutation until the kernel invokes ``apply_guarded`` with its permit.
    """

    try:
        if not isinstance(directory, Path):
            raise ValueError("directory")
        checked_action = revalidate_ga4_key_event_create_planned_action(action)
        checked_key = _require_fingerprint_key(fingerprint_key)
        _clock_now(clock)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_RUNTIME",
            "GA4 CREATE runtime inputs are invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None
    # Validate source/target/configuration before touching protected OAuth state.
    first_now = _clock_now(clock)
    _validate_current_sources(
        ledger,
        checked_action,
        fresh=fresh,
        fingerprint_key=checked_key,
        now=first_now,
    )
    facts = _current_configured_facts(directory, checked_action)
    try:
        current_key = export_key()
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "FINGERPRINT_KEY_UNAVAILABLE",
            "The current local fingerprint key is unavailable.",
            exit_code=ExitCode.PRECONDITION,
        ) from None
    if type(current_key) is not bytes or not hmac.compare_digest(current_key, checked_key):
        raise AdhelmError(
            "FINGERPRINT_KEY_CHANGED",
            "The local fingerprint key changed before GA4 CREATE execution.",
            exit_code=ExitCode.PRECONDITION,
        )
    bearer = _resolve_runtime_bearer(facts, checked_action)
    # OAuth consumption is itself a current local boundary; recheck every
    # source/config/key fact before the first provider read.
    second_now = _clock_now(clock)
    _validate_current_sources(
        ledger,
        checked_action,
        fresh=fresh,
        fingerprint_key=checked_key,
        now=second_now,
    )
    facts = _current_configured_facts(directory, checked_action)
    try:
        latest_key = export_key()
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "FINGERPRINT_KEY_UNAVAILABLE",
            "The current local fingerprint key is unavailable.",
            exit_code=ExitCode.PRECONDITION,
        ) from None
    if type(latest_key) is not bytes or not hmac.compare_digest(latest_key, checked_key):
        raise AdhelmError(
            "FINGERPRINT_KEY_CHANGED",
            "The local fingerprint key changed before GA4 CREATE provider reads.",
            exit_code=ExitCode.PRECONDITION,
        )
    runtime = _ConfiguredGa4CreateRuntime(
        _issuer=_RUNTIME_ISSUER,
        directory=directory,
        action=checked_action,
        ledger=ledger,
        fresh=fresh,
        fingerprint_key=checked_key,
        bearer=bearer,
        facts=facts,
        clock=clock,
    )
    try:
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            transport = SafeHttpClient(client, {_HOST})
            yield Ga4AdminKeyEventCreateGuardedAdapter(
                _issuer=_RUNTIME_ISSUER,
                runtime=runtime,
                transport=transport,
            )
    finally:
        runtime.close()
