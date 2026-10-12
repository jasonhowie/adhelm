"""Closed nonsecret drafts for the fixed Meta token-inspection profile."""

from __future__ import annotations

from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from adhelm.models import (
    Currency,
    MetaAccountTokenProfile,
    MetaNativeIdentifier,
    SafeRef,
    StrictModel,
    meta_token_credential_reference,
)


class MetaAccountTokenProfileDraft(StrictModel):
    """Customer declaration used before a protected Meta bundle is installed.

    The draft has no credential reference because that selector is derived from
    ``profile_id`` by the closed profile factory.  It also has no principal,
    scope observation, expiry, app secret, or provider access assertion.
    Currency and timezone are customer-declared read expectations; the future
    fixed account identity read must verify them before it uses any results.
    """

    schema_version: Literal[1] = 1
    profile_id: SafeRef
    account_ref: MetaNativeIdentifier
    client_application_ref: MetaNativeIdentifier
    currency: Currency
    timezone: Annotated[str, Field(min_length=1, max_length=80)]
    purpose: Literal["READ", "EDIT"] = "READ"
    inspector_credential_kind: Literal[
        "CUSTOMER_APP_ACCESS_TOKEN", "CUSTOMER_APP_DEVELOPER_USER_TOKEN"
    ]

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


def validate_meta_account_token_profile_draft(value: object) -> MetaAccountTokenProfileDraft:
    """Detach a potentially copied draft before a profile/config mutation."""
    if isinstance(value, MetaAccountTokenProfileDraft):
        return MetaAccountTokenProfileDraft.model_validate(
            value.model_dump(mode="python", warnings="error")
        )
    return MetaAccountTokenProfileDraft.model_validate(value)


def finalize_meta_account_token_profile(
    value: MetaAccountTokenProfileDraft,
) -> MetaAccountTokenProfile:
    """Create the only persisted Meta inspection profile form from a draft."""
    draft = validate_meta_account_token_profile_draft(value)
    return MetaAccountTokenProfile(
        profile_kind="META_ADS_ACCOUNT_TOKEN",
        profile_id=draft.profile_id,
        account_ref=draft.account_ref,
        client_application_ref=draft.client_application_ref,
        currency=draft.currency,
        timezone=draft.timezone,
        purpose=draft.purpose,
        inspector_credential_kind=draft.inspector_credential_kind,
        credential_ref=meta_token_credential_reference(draft.profile_id),
    )
