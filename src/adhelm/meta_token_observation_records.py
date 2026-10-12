"""Retained nonsecret facts from one fixed Meta `debug_token` inspection.

`auth meta-token inspect` performs the one reviewed Graph inspection and then
discards everything it saw.  `auth meta-token status` makes no provider request
at all, so without a local record it can say nothing about when the configured
token expires or what it was actually granted.

This module retains exactly the nonsecret facts a later status read needs: the
two documented expiry timestamps, the observation time, how many scopes the
token carried, and the granted scopes **clamped to the profile's purpose
allowlist**.  It never retains a token, an inspector token, a binding key, a
principal, an application identifier, or a scope outside the allowlist, so a
READ profile's record can never contain `ads_management` even if the customer's
token carries it.

Nothing here revokes anything.  Removing a local record, or removing the
protected bundle it describes, stops this product from using a token; only Meta
can revoke one.  `META_TOKEN_REVOCATION_GUIDANCE` states that separation in one
place so no command has to restate it differently.
"""

from __future__ import annotations

import hmac
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import MetaAccountTokenProfile
from adhelm.private_directories import ensure_private_directory

#: The closed per-purpose scope allowlist.  A profile records only the granted
#: scopes inside its own purpose's set; every other granted scope is counted
#: and never named.  A READ profile therefore never records `ads_management`.
META_TOKEN_PURPOSE_SCOPE_ALLOWLIST: Final[dict[str, tuple[str, ...]]] = {
    "READ": ("ads_read",),
    "EDIT": ("ads_management", "ads_read"),
}
#: One sentence, in one place, separating what this product can do locally from
#: what only Meta can do.  Local removal is not revocation.
META_TOKEN_REVOCATION_GUIDANCE: Final = (
    "Removing the local Meta bundle or this retained inspection record stops this product from "
    "using the token; it does not revoke it. Only Meta can revoke a token, from the Business "
    "Integrations or app settings that issued it. This product performs no revocation request."
)
#: What a status read discloses about Meta's app review and Business
#: Verification: nothing, because no command in this build reads either one.
META_TOKEN_APP_REVIEW_DISCLOSURE: Final = "NOT_DOCUMENTED_LOCALLY"

#: Another AdHelm command holds the observations-file lock.  Its callers say
#: what was and was not done, because the right recovery differs by command.
OBSERVATIONS_BUSY_CODE: Final = "META_TOKEN_OBSERVATIONS_BUSY"

_FILE = "meta-token-observations.json"
_MAX_BYTES = 65_536
_MAX_RECORDS = 100
_SCHEMA_VERSION = 1
_MAX_GRANTED_SCOPES = 100
# The exact shape `adhelm.meta_token_inspection` produces.  It is an HMAC over
# a private local key; it names no token and cannot be reversed into one.
_BINDING_REF = re.compile(r"^credential-binding:sha256:[0-9a-f]{64}$", re.ASCII)


@dataclass(frozen=True, slots=True)
class MetaTokenObservationRecord:
    """Nonsecret retained facts about one inspected Meta token.

    `credential_binding_ref` is the inspection's own binding reference: an HMAC
    over the installed bundle's private key and the exact subject token it
    inspected.  It names no token and cannot be reversed into one, and it is
    the reason a rotation cannot leave the replaced token's expiry in a status
    read: a new bundle carries a new key and a new token, so the reference
    changes and this record stops matching.
    """

    purpose: Literal["READ", "EDIT"]
    profile_configuration_digest: str
    credential_binding_ref: str
    observed_at: datetime
    expires_at: datetime
    data_access_expires_at: datetime
    purpose_allowed_scopes: tuple[str, ...]
    purpose_clamped_scopes: tuple[str, ...]
    granted_scope_count: int
    schema_version: Literal[1] = 1

    def is_expired_at(self, now: datetime) -> bool:
        """Whether either documented Meta expiry has already passed."""
        return self.expires_at <= now or self.data_access_expires_at <= now


def meta_token_purpose_allowed_scopes(purpose: str) -> tuple[str, ...]:
    """The closed scope allowlist for one declared profile purpose."""
    allowed = META_TOKEN_PURPOSE_SCOPE_ALLOWLIST.get(purpose)
    if allowed is None:
        raise _record_invalid()
    return allowed


def clamp_meta_token_scopes(purpose: str, granted: tuple[str, ...]) -> tuple[str, ...]:
    """Clamp an observed grant to the profile purpose's allowlist.

    Meta grants scopes to a *token*, not to a purpose, so a customer's token
    may legitimately carry more than this profile is for.  The extra scopes are
    counted, never recorded: recording them would let a READ profile's local
    state assert a write permission the product must never exercise from it.
    """
    allowed = set(meta_token_purpose_allowed_scopes(purpose))
    return tuple(sorted(scope for scope in set(granted) if scope in allowed))


def retain_meta_token_observation(
    directory: Path,
    profile: MetaAccountTokenProfile,
    *,
    granted_scopes: tuple[str, ...],
    credential_binding_ref: str,
    observed_at: datetime,
    expires_at: datetime,
    data_access_expires_at: datetime,
) -> MetaTokenObservationRecord:
    """Replace this profile's retained inspection facts with one bounded record."""
    if (
        type(granted_scopes) is not tuple
        or not 1 <= len(granted_scopes) <= _MAX_GRANTED_SCOPES
        or any(type(scope) is not str for scope in granted_scopes)
        or _BINDING_REF.fullmatch(credential_binding_ref) is None
    ):
        raise _record_invalid()
    record = MetaTokenObservationRecord(
        purpose=profile.purpose,
        profile_configuration_digest=digest(profile),
        credential_binding_ref=credential_binding_ref,
        observed_at=_aware(observed_at),
        expires_at=_aware(expires_at),
        data_access_expires_at=_aware(data_access_expires_at),
        purpose_allowed_scopes=meta_token_purpose_allowed_scopes(profile.purpose),
        purpose_clamped_scopes=clamp_meta_token_scopes(profile.purpose, granted_scopes),
        granted_scope_count=len(set(granted_scopes)),
    )
    with _observation_file_lock(directory):
        stored = _read_all(directory)
        stored[_key(profile)] = _encode(record)
        if len(stored) > _MAX_RECORDS:
            raise _record_invalid()
        _write_all(directory, stored)
    return record


class MetaTokenObservationOutcome(NamedTuple):
    """What a status read could learn from the retained inspection file.

    `NOT_RETAINED` is the honest answer for four different situations, and all
    four mean the same thing to a reader: the **installed** token has not been
    inspected locally.  `UNREADABLE` is deliberately not one of them -- a file
    this product cannot parse leaves a declared fact unread, and the caller
    reports that rather than a clean answer.
    """

    state: Literal["RETAINED", "NOT_RETAINED", "UNREADABLE"]
    record: MetaTokenObservationRecord | None


def load_meta_token_observation(
    directory: Path,
    profile: MetaAccountTokenProfile,
    *,
    credential_binding_ref: str | None,
) -> MetaTokenObservationOutcome:
    """Return this profile's retained inspection facts for the installed bundle.

    A record is answered only when it was produced by inspecting **the bundle
    that is installed now**.  Three things make it not apply:

    * the profile declaration changed, so the inspection describes a different
      customer declaration;
    * the installed bundle changed -- `auth meta-token rotate` replaces both
      tokens and the private binding key -- so the retained expiry and grant
      belong to a token this product no longer holds;
    * there is no installed bundle to bind to at all, so nothing can be
      confirmed.

    Each yields `NOT_RETAINED`, because reporting a replaced token's expiry as
    the installed token's expiry would state a fact about one credential as a
    fact about another.
    """
    payload = _read_all(directory).get(_key(profile))
    if payload is None:
        return MetaTokenObservationOutcome("NOT_RETAINED", None)
    try:
        record = _decode(payload)
    except AdhelmError:
        return MetaTokenObservationOutcome("UNREADABLE", None)
    if (
        record.profile_configuration_digest != digest(profile)
        or record.purpose != profile.purpose
        or credential_binding_ref is None
        or not hmac.compare_digest(record.credential_binding_ref, credential_binding_ref)
    ):
        return MetaTokenObservationOutcome("NOT_RETAINED", None)
    return MetaTokenObservationOutcome("RETAINED", record)


def remove_meta_token_observation(directory: Path, profile: MetaAccountTokenProfile) -> bool:
    """Delete only this profile's retained inspection record, then prove it is gone.

    The read, the rewrite and the readback happen under the same observation-file
    lock that `retain_meta_token_observation` takes, so every other profile's
    record in the shared file is written back unchanged against any other
    AdHelm writer.  The lock coordinates this product's writers only, not a
    manual edit of the file.  Returns whether a record existed; a profile that
    was never inspected, or whose record was already removed, is `False` and the
    file is not rewritten.  A file this product cannot parse is refused rather
    than rewritten, because rewriting it would silently drop the other
    profiles' records.
    """
    key = _key(profile)
    with _observation_file_lock(directory):
        stored = _read_all(directory)
        if key not in stored:
            return False
        del stored[key]
        _write_all(directory, stored)
        if key in _read_all(directory):
            raise _record_invalid()
    return True


@contextmanager
def _observation_file_lock(directory: Path) -> Iterator[None]:
    """Serialize every read-modify-write of the shared observations file.

    The lock is per resolved file path and OS user, in the same NUL-prefixed
    internal namespace `configuration_transaction` uses, so it cannot collide
    with a customer-selected credential slot.  Lock ordering: configuration,
    then a credential slot, then this lock; it is never held while taking
    another.  A busy lock surfaces as `META_TOKEN_OBSERVATIONS_BUSY`, never as a
    credential-setup diagnostic, because no credential setup is involved.
    """
    reference = "meta-token-observations\x00" + str(_path(directory).resolve())
    with ExitStack() as stack:
        try:
            stack.enter_context(credential_mutation_lock(reference))
        except AdhelmError as error:
            if error.code == "CREDENTIAL_SETUP_BUSY":
                # Not a credential setup: say what is actually held.  Callers that
                # know their own recovery step re-raise this with it.
                raise AdhelmError(
                    OBSERVATIONS_BUSY_CODE,
                    "Another AdHelm command is updating the retained Meta token inspection file.",
                    next_action="Wait for that command to finish, then run this command again.",
                ) from None
            raise
        yield


def _key(profile: MetaAccountTokenProfile) -> str:
    """A fixed-width local key derived from the profile identifier."""
    return digest({"meta_token_observation_key": profile.profile_id})


def _aware(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _record_invalid()
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _encode(record: MetaTokenObservationRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "purpose": record.purpose,
        "profile_configuration_digest": record.profile_configuration_digest,
        "credential_binding_ref": record.credential_binding_ref,
        "observed_at": _iso(record.observed_at),
        "expires_at": _iso(record.expires_at),
        "data_access_expires_at": _iso(record.data_access_expires_at),
        "purpose_allowed_scopes": list(record.purpose_allowed_scopes),
        "purpose_clamped_scopes": list(record.purpose_clamped_scopes),
        "granted_scope_count": record.granted_scope_count,
    }


def _decode(payload: object) -> MetaTokenObservationRecord:
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "purpose",
        "profile_configuration_digest",
        "credential_binding_ref",
        "observed_at",
        "expires_at",
        "data_access_expires_at",
        "purpose_allowed_scopes",
        "purpose_clamped_scopes",
        "granted_scope_count",
    }:
        raise _record_invalid()
    purpose = payload["purpose"]
    if purpose not in META_TOKEN_PURPOSE_SCOPE_ALLOWLIST:
        raise _record_invalid()
    allowed = meta_token_purpose_allowed_scopes(purpose)
    clamped = payload["purpose_clamped_scopes"]
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != _SCHEMA_VERSION
        or type(payload["profile_configuration_digest"]) is not str
        or type(payload["credential_binding_ref"]) is not str
        or _BINDING_REF.fullmatch(payload["credential_binding_ref"]) is None
        or type(payload["granted_scope_count"]) is not int
        or type(payload["granted_scope_count"]) is bool
        or not 0 <= payload["granted_scope_count"] <= _MAX_GRANTED_SCOPES
        or payload["purpose_allowed_scopes"] != list(allowed)
        or type(clamped) is not list
        # A record that named a scope outside its purpose's allowlist would be
        # exactly the state this module exists to make impossible.  Refuse it.
        or any(scope not in allowed for scope in clamped)
        or len(set(clamped)) != len(clamped)
    ):
        raise _record_invalid()
    return MetaTokenObservationRecord(
        purpose=purpose,
        profile_configuration_digest=payload["profile_configuration_digest"],
        credential_binding_ref=payload["credential_binding_ref"],
        observed_at=_timestamp(payload["observed_at"]),
        expires_at=_timestamp(payload["expires_at"]),
        data_access_expires_at=_timestamp(payload["data_access_expires_at"]),
        purpose_allowed_scopes=allowed,
        purpose_clamped_scopes=tuple(clamped),
        granted_scope_count=payload["granted_scope_count"],
    )


def _timestamp(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise _record_invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _record_invalid() from None
    return _aware(parsed)


def _path(directory: Path) -> Path:
    return directory / _FILE


def _read_all(directory: Path) -> dict[str, Any]:
    path = _path(directory)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        raise _store_unavailable() from None
    if len(raw) > _MAX_BYTES:
        raise _record_invalid()
    try:
        value = parse_json(raw, maximum_bytes=_MAX_BYTES)
    except (AdhelmError, ValueError, UnicodeDecodeError):
        raise _record_invalid() from None
    if type(value) is not dict or len(value) > _MAX_RECORDS:
        raise _record_invalid()
    return value


def _write_all(directory: Path, stored: dict[str, Any]) -> None:
    ensure_private_directory(directory)
    encoded = canonical_bytes(stored)
    if len(encoded) > _MAX_BYTES:
        raise _record_invalid()
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".meta-token-observations-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, _path(directory))
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise _store_unavailable() from None


def _record_invalid() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_OBSERVATION_RECORD_INVALID",
        "The retained Meta token inspection record could not be verified.",
        exit_code=int(ExitCode.AMBIGUOUS),
        next_action="Inspect the configured Meta token again before relying on local status.",
    )


def _store_unavailable() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_OBSERVATION_STORE_UNAVAILABLE",
        "The local Meta token inspection record could not be read or written.",
        exit_code=int(ExitCode.PRECONDITION),
        next_action="Check the local data directory before a deliberate follow-up.",
    )
