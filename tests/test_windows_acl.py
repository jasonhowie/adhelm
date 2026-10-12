"""Windows ACL parsing and privacy policy, exercised on every platform with real binary ACLs."""

from __future__ import annotations

import struct

import pytest

from adhelm.windows_acl import (
    ADMINISTRATORS_SID,
    FILE_SUPPORTS_OPEN_BY_FILE_ID,
    SYSTEM_SID,
    AccessEntry,
    Principals,
    SecurityState,
    has_stable_identity,
    parse_acl,
    parse_sid,
)

USER = "S-1-5-21-1000-2000-3000-1001"
OTHER_USER = "S-1-5-21-1000-2000-3000-1002"
EVERYONE = "S-1-1-0"
USERS_GROUP = "S-1-5-32-545"

ALLOWED, DENIED, AUDIT, ALLOWED_OBJECT, ALLOWED_CALLBACK, LABEL = 0, 1, 2, 5, 9, 17
INHERIT_ONLY = 0x08
OBJECT_INHERIT = 0x01
FULL_CONTROL = 0x1F01FF
READ_AND_EXECUTE = 0x1200A9
CREATE_CHILD = 0x4
MODIFY = 0x1301BF
DELETE_CHILD = 0x40


def binary_sid(text: str) -> bytes:
    parts = [int(part) for part in text.split("-")[2:]]
    authority, subauthorities = parts[0], parts[1:]
    return (
        bytes([1, len(subauthorities)])
        + authority.to_bytes(6, "big")
        + b"".join(struct.pack("<I", value) for value in subauthorities)
    )


def binary_ace(ace_type: int, flags: int, mask: int, sid: str, *, prefix: bytes = b"") -> bytes:
    body = struct.pack("<I", mask) + prefix + binary_sid(sid)
    return struct.pack("<BBH", ace_type, flags, 4 + len(body)) + body


def binary_acl(*entries: bytes) -> bytes:
    body = b"".join(entries)
    return struct.pack("<BBHHH", 2, 0, 8 + len(body), len(entries), 0) + body


def state(owner: str, *entries: tuple[int, int, int, str]) -> SecurityState:
    return SecurityState(owner, tuple(AccessEntry(*entry) for entry in entries))


PRINCIPALS = Principals(user=USER, default_owner=USER)
ELEVATED = Principals(user=USER, default_owner=ADMINISTRATORS_SID)

PRIVATE_ENTRIES = (
    (ALLOWED, 0, FULL_CONTROL, USER),
    (ALLOWED, 0, FULL_CONTROL, SYSTEM_SID),
    (ALLOWED, 0, FULL_CONTROL, ADMINISTRATORS_SID),
)


def test_sid_round_trips_well_known_and_domain_identities() -> None:
    for text in (EVERYONE, SYSTEM_SID, ADMINISTRATORS_SID, USER):
        assert parse_sid(binary_sid(text)) == text


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x02\x01" + bytes(6) + bytes(4),
        bytes([1, 3]) + bytes(6) + bytes(4),
        bytes([1, 16]) + bytes(70),
    ],
)
def test_malformed_sids_are_rejected(data: bytes) -> None:
    with pytest.raises(ValueError):
        parse_sid(data)


def test_acl_parses_allow_deny_and_object_entries() -> None:
    object_flags = struct.pack("<I", 1) + bytes(16)
    data = binary_acl(
        binary_ace(ALLOWED, OBJECT_INHERIT, FULL_CONTROL, USER),
        binary_ace(DENIED, 0, FULL_CONTROL, OTHER_USER),
        binary_ace(ALLOWED_OBJECT, 0, READ_AND_EXECUTE, EVERYONE, prefix=object_flags),
    )
    assert parse_acl(data) == (
        AccessEntry(ALLOWED, OBJECT_INHERIT, FULL_CONTROL, USER),
        AccessEntry(DENIED, 0, FULL_CONTROL, OTHER_USER),
        AccessEntry(ALLOWED_OBJECT, 0, READ_AND_EXECUTE, EVERYONE),
    )


def test_compound_acl_entries_are_kept_but_never_trusted() -> None:
    entries = parse_acl(binary_acl(binary_ace(4, 0, FULL_CONTROL, USER)))
    assert entries[0].sid != USER
    assert not PRINCIPALS.is_private_object(SecurityState(USER, entries))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data[:7],
        lambda data: data[:2] + struct.pack("<H", len(data) + 8) + data[4:],
        lambda data: data[:4] + struct.pack("<H", 99) + data[6:],
        lambda data: data[:-3],
    ],
)
def test_inconsistent_acl_structures_are_rejected(mutate) -> None:
    valid = binary_acl(binary_ace(ALLOWED, 0, FULL_CONTROL, USER))
    with pytest.raises(ValueError):
        parse_acl(mutate(valid))


def test_a_user_system_and_administrators_acl_is_private() -> None:
    assert PRINCIPALS.is_private_object(state(USER, *PRIVATE_ENTRIES))


def test_the_acl_python_itself_applies_for_mkdir_mode_0o700_is_private() -> None:
    owner_rights = "S-1-3-4"
    python_private = (
        (ALLOWED, 0x03, FULL_CONTROL, SYSTEM_SID),
        (ALLOWED, 0x03, FULL_CONTROL, ADMINISTRATORS_SID),
        (ALLOWED, 0x03, FULL_CONTROL, owner_rights),
    )
    assert ELEVATED.is_private_object(state(ADMINISTRATORS_SID, *python_private))
    assert PRINCIPALS.is_private_object(state(USER, *python_private))
    assert not PRINCIPALS.is_private_object(state(OTHER_USER, *python_private))


def test_an_elevated_administrator_default_owner_is_private() -> None:
    owned = state(ADMINISTRATORS_SID, *PRIVATE_ENTRIES)
    assert ELEVATED.is_private_object(owned)
    assert not PRINCIPALS.is_private_object(owned)


@pytest.mark.parametrize("sid", [EVERYONE, USERS_GROUP, OTHER_USER])
def test_any_allow_entry_for_another_principal_is_not_private(sid: str) -> None:
    assert not PRINCIPALS.is_private_object(
        state(USER, *PRIVATE_ENTRIES, (ALLOWED, 0, READ_AND_EXECUTE, sid))
    )


def test_a_foreign_owner_is_not_private_even_with_a_clean_acl() -> None:
    assert not PRINCIPALS.is_private_object(state(OTHER_USER, *PRIVATE_ENTRIES))


def test_a_null_dacl_grants_everyone_and_is_not_private() -> None:
    assert not PRINCIPALS.is_private_object(SecurityState(USER, None))
    assert not PRINCIPALS.is_trusted_directory(SecurityState(USER, None))


def test_deny_audit_label_and_inherit_only_entries_do_not_grant_access() -> None:
    entries = (
        *PRIVATE_ENTRIES,
        (DENIED, 0, FULL_CONTROL, EVERYONE),
        (AUDIT, 0, FULL_CONTROL, EVERYONE),
        (LABEL, 0, 0x1, EVERYONE),
        (ALLOWED, INHERIT_ONLY, FULL_CONTROL, EVERYONE),
        (ALLOWED, 0, 0, EVERYONE),
    )
    assert PRINCIPALS.is_private_object(state(USER, *entries))


def test_an_unknown_entry_type_is_treated_as_granting() -> None:
    assert not PRINCIPALS.is_private_object(state(USER, *PRIVATE_ENTRIES, (0x7F, 0, 1, EVERYONE)))


def test_a_callback_allow_entry_for_another_principal_is_not_private() -> None:
    entries = (*PRIVATE_ENTRIES, (ALLOWED_CALLBACK, 0, READ_AND_EXECUTE, EVERYONE))
    assert not PRINCIPALS.is_private_object(state(USER, *entries))


def test_a_system_drive_root_is_a_trusted_ancestor() -> None:
    root = state(
        SYSTEM_SID,
        (ALLOWED, 0, FULL_CONTROL, SYSTEM_SID),
        (ALLOWED, 0, FULL_CONTROL, ADMINISTRATORS_SID),
        (ALLOWED, 0, READ_AND_EXECUTE, USERS_GROUP),
        (ALLOWED, 0, CREATE_CHILD, "S-1-5-11"),
        (ALLOWED, INHERIT_ONLY, MODIFY, "S-1-5-11"),
    )
    assert PRINCIPALS.is_trusted_directory(root)
    assert not PRINCIPALS.is_private_object(root)


@pytest.mark.parametrize("mask", [MODIFY, DELETE_CHILD, 0x10000, 0x40000, 0x80000, 0x40000000])
def test_an_ancestor_others_can_delete_or_take_over_is_not_trusted(mask: int) -> None:
    ancestor = state(USER, *PRIVATE_ENTRIES, (ALLOWED, 0, mask, "S-1-5-11"))
    assert not PRINCIPALS.is_trusted_directory(ancestor)


def test_an_ancestor_owned_by_another_user_is_not_trusted() -> None:
    assert not PRINCIPALS.is_trusted_directory(state(OTHER_USER, *PRIVATE_ENTRIES))


# --- Stable file identity ------------------------------------------------------------

REAL_ID = bytes(range(1, 17))


def test_a_stable_volume_with_a_nonzero_id_is_trusted() -> None:
    assert has_stable_identity(0xABCD, REAL_ID, FILE_SUPPORTS_OPEN_BY_FILE_ID)
    # Other volume flags are irrelevant, only the open-by-ID promise matters.
    assert has_stable_identity(0xABCD, REAL_ID, FILE_SUPPORTS_OPEN_BY_FILE_ID | 0x7)


@pytest.mark.parametrize("flags", [0, 0x7, 0x00FFFFFF])
def test_a_volume_without_the_open_by_id_promise_is_never_trusted(flags: int) -> None:
    # What FAT and exFAT report: IDs, if any, are positional and may be reused.
    assert not has_stable_identity(0xABCD, REAL_ID, flags)


def test_a_zero_file_id_or_volume_serial_never_identifies_an_object() -> None:
    assert not has_stable_identity(0xABCD, bytes(16), FILE_SUPPORTS_OPEN_BY_FILE_ID)
    assert not has_stable_identity(0xABCD, b"", FILE_SUPPORTS_OPEN_BY_FILE_ID)
    assert not has_stable_identity(0, REAL_ID, FILE_SUPPORTS_OPEN_BY_FILE_ID)
