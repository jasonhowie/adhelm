"""Provider-neutral checks for the shared semantic-hierarchy projection."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel

from adhelm.errors import AdhelmError
from adhelm.providers import _semantic_state as semantic_state
from adhelm.providers._semantic_state import (
    SemanticHierarchySpec,
    SemanticStateBounds,
)

_KEY = b"k" * 32
_OTHER_KEY = b"q" * 32
_SPEC = SemanticHierarchySpec(
    context=b"adhelm-test-semantic-v1\x00",
    state_kind="TEST_SEMANTIC_COLLECTION_V1",
    semantic_projection="NAME_HMAC_CONFIGURATION_EXACT_V1",
    target_purpose="test-semantic-target",
    member_purpose="test-semantic-member",
    collection_purpose="test-semantic-collection",
    bounds=SemanticStateBounds(max_items=4, max_state_nodes=12, max_state_depth=3),
)
_OTHER_SPEC = SemanticHierarchySpec(
    context=b"adhelm-other-semantic-v1\x00",
    state_kind=_SPEC.state_kind,
    semantic_projection=_SPEC.semantic_projection,
    target_purpose=_SPEC.target_purpose,
    member_purpose=_SPEC.member_purpose,
    collection_purpose=_SPEC.collection_purpose,
)
_MARKER = "c" * 64


def _member(name: str, *, spec: SemanticHierarchySpec = _SPEC, key: bytes = _KEY) -> str:
    return semantic_state.member_fingerprint(
        spec,
        identity_field="name_fingerprint",
        identity_fingerprint=semantic_state.keyed_fingerprint(
            key, context=spec.context, purpose="test-name", value=name
        ),
        configuration={"mode": "ONCE", "value": None},
        fingerprint_key=key,
    )


def _state(members: tuple[str, ...], *, spec: SemanticHierarchySpec = _SPEC, key: bytes = _KEY):
    return semantic_state.collection_state(
        spec,
        binding_fingerprint=semantic_state.target_binding_fingerprint(
            spec, {"property": "p", "stream": "s"}, fingerprint_key=key
        ),
        fingerprint_key_marker=_MARKER,
        member_fingerprints=tuple(sorted(members)),
        fingerprint_key=key,
    )


def test_projection_is_order_independent_and_keeps_duplicate_members() -> None:
    one, two = _member("first"), _member("second")

    assert _state((one, two)) == _state((two, one))
    assert _state((one, one)) != _state((one,))
    assert _state((one,)).revision == _SPEC.state_kind


def test_provider_native_identifiers_are_excluded_from_a_member() -> None:
    """Two resources with different provider IDs but one configuration match."""

    assert _member("same") == _member("same")
    assert _member("same") != _member("other")


def test_a_different_key_or_context_never_produces_the_same_fingerprint() -> None:
    assert _member("first") != _member("first", key=_OTHER_KEY)
    assert _member("first") != _member("first", spec=_OTHER_SPEC)
    assert _state((_member("first"),)) != _state(
        (_member("first", spec=_OTHER_SPEC),), spec=_OTHER_SPEC
    )


def test_member_configuration_cannot_restate_reserved_projection_fields() -> None:
    with pytest.raises(ValueError):
        semantic_state.member_fingerprint(
            _SPEC,
            identity_field="name_fingerprint",
            identity_fingerprint="a" * 64,
            configuration={"semantic_projection": "forged"},
            fingerprint_key=_KEY,
        )


def test_collection_refuses_unsorted_oversized_or_malformed_members() -> None:
    one, two = sorted((_member("first"), _member("second")))
    with pytest.raises(ValueError):
        semantic_state.collection_state(
            _SPEC,
            binding_fingerprint="a" * 64,
            fingerprint_key_marker=_MARKER,
            member_fingerprints=(two, one),
            fingerprint_key=_KEY,
        )
    with pytest.raises(ValueError):
        semantic_state.collection_state(
            _SPEC,
            binding_fingerprint="a" * 64,
            fingerprint_key_marker=_MARKER,
            member_fingerprints=("not-a-digest",),
            fingerprint_key=_KEY,
        )
    with pytest.raises(ValueError):
        semantic_state.collection_state(
            _SPEC,
            binding_fingerprint="a" * 64,
            fingerprint_key_marker=_MARKER,
            member_fingerprints=tuple(sorted(_member(str(index)) for index in range(6))),
            fingerprint_key=_KEY,
        )


def test_a_short_or_missing_local_key_cannot_produce_a_fingerprint() -> None:
    for key in (b"", b"short", "not-bytes"):
        with pytest.raises(ValueError):
            semantic_state.keyed_fingerprint(
                key,  # type: ignore[arg-type]
                context=_SPEC.context,
                purpose="test-name",
                value="first",
            )


def test_bounded_json_shape_rejects_deep_wide_and_non_json_values() -> None:
    semantic_state.validate_json_shape({"a": {"b": [1, "x", None, True]}}, bounds=_SPEC.bounds)
    with pytest.raises(ValueError):
        semantic_state.validate_json_shape({"a": {"b": {"c": {"d": 1}}}}, bounds=_SPEC.bounds)
    with pytest.raises(ValueError):
        semantic_state.validate_json_shape({"a": list(range(6))}, bounds=_SPEC.bounds)
    with pytest.raises(ValueError):
        semantic_state.validate_json_shape({"a": Decimal("1.5")}, bounds=_SPEC.bounds)
    with pytest.raises(ValueError):
        semantic_state.validate_json_shape({"a": float("nan")}, bounds=_SPEC.bounds)
    with pytest.raises(ValueError):
        semantic_state.validate_json_shape({"a": 2**60}, bounds=_SPEC.bounds)


def test_state_json_mapping_accepts_only_the_expected_installed_model() -> None:
    class _Payload(BaseModel):
        value: int

    class _Other(BaseModel):
        value: int

    assert semantic_state.state_json_mapping(_Payload(value=1), expected=_Payload) == {"value": 1}
    with pytest.raises(ValueError):
        semantic_state.state_json_mapping(_Other(value=1), expected=_Payload)


def test_spec_and_bounds_reject_empty_or_non_positive_configuration() -> None:
    with pytest.raises(ValueError):
        SemanticStateBounds(max_items=0)
    with pytest.raises(ValueError):
        SemanticHierarchySpec(
            context=b"",
            state_kind="K",
            semantic_projection="P",
            target_purpose="t",
            member_purpose="m",
            collection_purpose="c",
        )
    with pytest.raises(ValueError):
        SemanticHierarchySpec(
            context=b"x\x00",
            state_kind="ü",
            semantic_projection="P",
            target_purpose="t",
            member_purpose="m",
            collection_purpose="c",
        )


@pytest.mark.parametrize(
    "configuration",
    [
        {"id": "9182"},
        {"resource_name": "keyEvent"},
        {"campaign_id": "88213"},
        {"nested": {"ad_group_ids": ["1", "2"]}},
        {"parent": "properties/123/keyEvents/456"},
        {"parent": "customers/1234567890/campaigns/77"},
        {"members": [{"name": "x"}]},
    ],
)
def test_native_identifier_shapes_are_refused_in_a_member_configuration(
    configuration: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        semantic_state.member_fingerprint(
            _SPEC,
            identity_field="name_fingerprint",
            identity_fingerprint="a" * 64,
            configuration=configuration,
            fingerprint_key=_KEY,
        )


def test_request_controllable_configuration_is_still_accepted() -> None:
    assert semantic_state.member_fingerprint(
        _SPEC,
        identity_field="name_fingerprint",
        identity_fingerprint="a" * 64,
        configuration={"mode": "ONCE_PER_EVENT", "value": {"amount": "1.25", "currency": "USD"}},
        fingerprint_key=_KEY,
    ) != semantic_state.member_fingerprint(
        _SPEC,
        identity_field="name_fingerprint",
        identity_fingerprint="a" * 64,
        configuration={"mode": "ONCE_PER_SESSION", "value": None},
        fingerprint_key=_KEY,
    )


def test_lone_surrogates_raise_the_modules_bounded_failure() -> None:
    with pytest.raises(ValueError) as error:
        semantic_state.validate_json_shape({"a": "lone \ud800 surrogate"}, bounds=_SPEC.bounds)
    assert not isinstance(error.value, UnicodeError)
    with pytest.raises(ValueError) as key_error:
        semantic_state.state_json_mapping(
            {"a": "lone \ud800 surrogate"}, expected=BaseModel, bounds=_SPEC.bounds
        )
    assert not isinstance(key_error.value, UnicodeError)


@pytest.mark.parametrize(
    "key", ["campaignId", "resourceName", "propertyId", "adGroupIds", "Resource-Ref", "CAMPAIGN_ID"]
)
def test_camel_case_identifier_keys_are_refused_like_snake_case(key: str) -> None:
    with pytest.raises(ValueError):
        semantic_state.member_fingerprint(
            _SPEC,
            identity_field="name_fingerprint",
            identity_fingerprint="a" * 64,
            configuration={key: "value"},
            fingerprint_key=_KEY,
        )


def test_uncanonicalizable_values_raise_the_modules_bounded_failure() -> None:
    for value in ({"big": 2**60}, {"text": "lone \ud800 surrogate"}, {"weird": object()}):
        with pytest.raises(ValueError) as error:
            semantic_state.keyed_fingerprint(
                _KEY, context=_SPEC.context, purpose="test-name", value=value
            )
        assert not isinstance(error.value, AdhelmError)
