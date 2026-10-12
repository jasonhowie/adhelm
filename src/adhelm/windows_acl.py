"""Windows ACL parsing and the privacy policy applied to credential and artifact paths.

Windows has no POSIX mode bits. The equivalent of "owned by the current user and
not accessible to group or other" is an explicit owner plus a DACL whose every
effective allow entry names a trusted principal. The binary SID/ACL parsing and
the policy here are plain functions over bytes, so they run and are tested on
every platform; ``windows_filesystem`` supplies the Win32 calls that read real
descriptors and feeds them to this policy.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Access mask bits (winnt.h).
_FILE_DELETE_CHILD = 0x40
_DELETE = 0x10000
_WRITE_DAC = 0x40000
_WRITE_OWNER = 0x80000
_GENERIC_WRITE = 0x40000000
_GENERIC_ALL = 0x10000000

# Rights that let a principal delete, rename or take over a directory or the
# children it holds: the Windows counterpart of "group/world-writable without
# the sticky bit". Rights that only create new children are tolerated, as they
# are for sticky directories such as /tmp and for the Windows system drive root.
_REPLACEMENT_RIGHTS = (
    _DELETE | _FILE_DELETE_CHILD | _WRITE_DAC | _WRITE_OWNER | _GENERIC_WRITE | _GENERIC_ALL
)

# ACE types that grant access (allowed, compound, object, callback) and ACE types
# known not to (denied, audit, alarm, mandatory label, resource attribute, scoped
# policy). An unknown type could grant access in a way this parser cannot judge,
# so it counts as granting.
_ALLOW_TYPES = frozenset({0, 4, 5, 9, 11})
_NON_GRANTING_TYPES = frozenset({1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17, 18, 19})
_OBJECT_TYPES = frozenset({5, 6, 7, 8, 11, 12, 15, 16})
_COMPOUND_TYPE = 4
_INHERIT_ONLY_ACE = 0x08

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
_TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
_CREATOR_OWNER_SID = "S-1-3-0"
_OWNER_RIGHTS_SID = "S-1-3-4"
_UNPARSED_SID = "S-UNPARSED"


@dataclass(frozen=True)
class AccessEntry:
    """One discretionary ACE reduced to the fields the privacy policy needs."""

    ace_type: int
    flags: int
    mask: int
    sid: str

    @property
    def effective(self) -> bool:
        """Whether this entry grants access to the object it is attached to."""
        if self.flags & _INHERIT_ONLY_ACE:
            return False
        if self.ace_type in _ALLOW_TYPES:
            return self.mask != 0
        return self.ace_type not in _NON_GRANTING_TYPES


@dataclass(frozen=True)
class SecurityState:
    """Owner and DACL of one object. ``entries`` is ``None`` for a NULL DACL."""

    owner: str
    entries: tuple[AccessEntry, ...] | None


@dataclass(frozen=True)
class Principals:
    """The identities a credential or artifact path may be shared with.

    ``user`` is the process token user and ``default_owner`` the owner assigned
    to objects the process creates (the user, or ``Administrators`` for an
    elevated administrator). ``SYSTEM`` and ``Administrators`` can already read
    any file, so listing them does not widen who can reach the data. ``CREATOR
    OWNER`` and ``OWNER RIGHTS`` name whoever owns the object, which the policy
    already requires to be one of the above; Python's own ``mkdir(mode=0o700)``
    on Windows produces exactly that ACL.
    """

    user: str
    default_owner: str

    @property
    def private_owners(self) -> frozenset[str]:
        return frozenset({self.user, self.default_owner})

    @property
    def trusted(self) -> frozenset[str]:
        return self.private_owners | {
            SYSTEM_SID,
            ADMINISTRATORS_SID,
            _TRUSTED_INSTALLER_SID,
            _CREATOR_OWNER_SID,
            _OWNER_RIGHTS_SID,
        }

    def is_private_object(self, state: SecurityState) -> bool:
        """Accept only an object owned by this user that grants nothing to others.

        The counterpart of ``st_uid == uid and mode & 0o077 == 0``. A NULL DACL
        grants everyone full access and is never private. Deny entries can only
        narrow access, so they are ignored.
        """
        if state.owner not in self.private_owners or state.entries is None:
            return False
        return all(entry.sid in self.trusted for entry in state.entries if entry.effective)

    def is_trusted_directory(self, state: SecurityState) -> bool:
        """Accept a directory that no untrusted principal can delete or take over.

        The counterpart of the POSIX ancestor rule: owned by the user or the
        system, and not writable by others without sticky-bit protection.
        """
        if state.owner not in self.trusted or state.entries is None:
            return False
        return all(
            entry.sid in self.trusted or not entry.mask & _REPLACEMENT_RIGHTS
            for entry in state.entries
            if entry.effective
        )


# Volume flag (fileapi.h) set by NTFS and ReFS, and by network servers that keep
# file IDs stable. FAT and exFAT synthesize a positional ID or none, and do not set it.
FILE_SUPPORTS_OPEN_BY_FILE_ID = 0x01000000


def has_stable_identity(volume_serial: int, file_id: bytes, volume_flags: int) -> bool:
    """Whether an object's volume serial and file ID can be trusted to tell objects apart.

    Identity comparison (``object_id``/``fingerprint``) catches a swapped object
    only when the ID names one object for as long as it exists. A volume that
    advertises ``FILE_SUPPORTS_OPEN_BY_FILE_ID`` promises that. An all-zero serial
    or ID is what a filesystem reports when it has none, so it never identifies
    anything and is refused whatever the volume claims.
    """
    return (
        bool(volume_flags & FILE_SUPPORTS_OPEN_BY_FILE_ID) and volume_serial != 0 and any(file_id)
    )


def parse_sid(data: bytes) -> str:
    """Return the string form of a binary SID, or raise ``ValueError``."""
    if len(data) < 8 or data[0] != 1:
        raise ValueError("Unsupported SID revision")
    count = data[1]
    if count > 15 or len(data) < 8 + 4 * count:
        raise ValueError("Truncated SID")
    authority = int.from_bytes(data[2:8], "big")
    subauthorities = struct.unpack_from(f"<{count}I", data, 8)
    return "S-1-" + "-".join(str(value) for value in (authority, *subauthorities))


def _entry_sid(data: bytes, ace_type: int, offset: int, end: int) -> str:
    """Locate the trustee SID inside one ACE, or return an unparsed marker."""
    if ace_type == _COMPOUND_TYPE:
        return _UNPARSED_SID
    sid_offset = offset + 8
    if ace_type in _OBJECT_TYPES:
        if end - offset < 12:
            return _UNPARSED_SID
        object_flags = struct.unpack_from("<I", data, offset + 8)[0]
        sid_offset = offset + 12 + (16 if object_flags & 1 else 0) + (16 if object_flags & 2 else 0)
    try:
        return parse_sid(data[sid_offset:end])
    except ValueError:
        return _UNPARSED_SID


def parse_acl(data: bytes) -> tuple[AccessEntry, ...]:
    """Parse a binary ACL, or raise ``ValueError`` when its structure is inconsistent."""
    if len(data) < 8:
        raise ValueError("Truncated ACL")
    _, _, size, count, _ = struct.unpack_from("<BBHHH", data, 0)
    if not 8 <= size <= len(data):
        raise ValueError("Inconsistent ACL size")
    entries: list[AccessEntry] = []
    offset = 8
    for _ in range(count):
        if offset + 8 > size:
            raise ValueError("Truncated ACE")
        ace_type, flags, ace_size = struct.unpack_from("<BBH", data, offset)
        end = offset + ace_size
        if ace_size < 8 or end > size:
            raise ValueError("Inconsistent ACE size")
        mask = struct.unpack_from("<I", data, offset + 4)[0]
        entries.append(AccessEntry(ace_type, flags, mask, _entry_sid(data, ace_type, offset, end)))
        offset = end
    return tuple(entries)
