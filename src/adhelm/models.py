"""Strict versioned public data contracts shared by local commands."""

import hashlib
import re
from typing import Annotated, Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    is_google_oauth_client_id,
    is_google_oauth_operational_principal_ref,
)
from adhelm.privacy import validate_data

# One subplan may issue several ordered child requests inside a single approved
# provider operation.  The bound is deliberately small: a plan that needs more
# steps than this is a hierarchy the compiler should split, not a longer script.
MAX_CHILD_EGRESS_COUNT: Final[int] = 32

Provider = Literal[
    "google_ads",
    "meta_ads",
    "linkedin_ads",
    "tiktok_ads",
    "reddit_ads",
    "openai_ads",
    "ga4",
    "ga4_admin",
    "gtm",
    "hubspot",
    "salesforce",
]
AD_PROVIDERS: tuple[str, ...] = (
    "google_ads",
    "meta_ads",
    "linkedin_ads",
    "tiktok_ads",
    "reddit_ads",
    "openai_ads",
)
PROVIDERS: tuple[str, ...] = (*AD_PROVIDERS, "ga4", "ga4_admin", "gtm", "hubspot", "salesforce")
SafeRef = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:/-]+$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
OpenAIAccountClientRef = Annotated[
    str, Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
]
EXPORT_PSEUDONYMIZATION_REFERENCE: Final = "export-pseudonymization-v1"
OPENAI_PAUSE_BINDING_KEY_REFERENCE: Final = "openai-pause-credential-binding-v1"
GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE: Final = "google-ads-pause-credential-binding-v1"
META_TOKEN_CREDENTIAL_REFERENCE_PREFIX: Final = "meta-token-bundle-v1."
# LinkedIn derives its protected client-secret, refresh and state selectors under this
# namespace. The "#" separator cannot occur in a SafeRef, so a configured credential
# reference can never reach it; this check is defense in depth, matching Meta.
LINKEDIN_TOKEN_CREDENTIAL_REFERENCE_PREFIX: Final = "linkedin-token-v1#"
# TikTok derives its protected app-secret, advertiser and state selectors the same way.
TIKTOK_TOKEN_CREDENTIAL_REFERENCE_PREFIX: Final = "tiktok-token-v1#"
# Reddit derives its protected refresh-token and state selectors the same way.
REDDIT_TOKEN_CREDENTIAL_REFERENCE_PREFIX: Final = "reddit-token-v1#"
RESERVED_CREDENTIAL_REFERENCES: Final = frozenset(
    {
        EXPORT_PSEUDONYMIZATION_REFERENCE,
        OPENAI_PAUSE_BINDING_KEY_REFERENCE,
        GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
    }
)
_META_TOKEN_CREDENTIAL_REFERENCE_DOMAIN: Final = b"adhelm/meta/token/bundle-reference/v1\x00"


def meta_token_credential_reference(profile_id: object) -> str:
    """Derive the sole protected Meta-token bundle slot for one profile.

    The profile ID is configuration data, while the resulting reference is an
    internal credential-store selector.  It is never accepted from a caller as
    an independently chosen slot.
    """
    if type(profile_id) is not str or re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,160}", profile_id) is None:
        raise ValueError("Meta token profile ID is invalid")
    suffix = hashlib.sha256(
        _META_TOKEN_CREDENTIAL_REFERENCE_DOMAIN + profile_id.encode("ascii")
    ).hexdigest()
    return META_TOKEN_CREDENTIAL_REFERENCE_PREFIX + suffix


def is_reserved_credential_reference(value: object) -> bool:
    """Return whether a reference selects local internal credential state."""
    if type(value) is not str:
        return False
    normalized = value.casefold()
    return normalized in RESERVED_CREDENTIAL_REFERENCES or normalized.startswith(
        (
            META_TOKEN_CREDENTIAL_REFERENCE_PREFIX,
            LINKEDIN_TOKEN_CREDENTIAL_REFERENCE_PREFIX,
            TIKTOK_TOKEN_CREDENTIAL_REFERENCE_PREFIX,
            REDDIT_TOKEN_CREDENTIAL_REFERENCE_PREFIX,
        )
    )


def _exact_schema_version_one(value: object) -> int:
    """Reject bool and float equality shortcuts before ``Literal[1]`` sees them."""
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be the integer 1")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def schema_version_requires_integer(cls, value: object) -> int:
        # Pydantic's Literal equality can accept 1.0 or True even with strict
        # mode. Preserve the raw version's type before per-model version
        # constraints run, including inside persisted output/retention rules.
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def safe_data(self) -> "StrictModel":
        validate_data(self.model_dump(mode="json"))
        return self


class Money(StrictModel):
    minor_units: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    currency: Currency


class Profile(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "provider": {"const": "google_ads"},
                            "purpose": {"const": "EDIT"},
                            "credential_source": {"const": "OS_OAUTH_GOOGLE"},
                        },
                        "required": ["provider", "purpose", "credential_source"],
                    },
                    "then": {
                        "required": ["currency", "timezone"],
                        "properties": {"currency": {"type": "string"}},
                    },
                }
            ]
        }
    )
    schema_version: Literal[1] = 1
    profile_id: SafeRef
    provider: Provider
    account_ref: SafeRef
    principal_ref: SafeRef
    client_ref: SafeRef
    scopes: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1)
    purpose: Literal["READ", "EDIT", "ACTIVATE", "PUBLISH"] = "READ"
    currency: Currency | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=80)] = "UTC"
    credential_source: Literal["OS_STORE", "EXPLICIT_CI", "OS_OAUTH_GOOGLE"] = "OS_STORE"
    credential_ref: SafeRef
    login_customer_ref: str | None = Field(default=None, pattern=r"^[0-9]{10}$")

    @model_validator(mode="before")
    @classmethod
    def explicit_google_ads_edit_expectations(cls, value: Any) -> Any:
        if isinstance(value, dict) and "secondary_credential_ref" in value:
            # AdHelm 0.1.0 wrote this field on every profile (null, or the name of a Google
            # Ads developer-token entry). Google retired developer tokens on 2026-09-09: the
            # value is dropped on load, never read and never written back.
            value = {k: v for k, v in value.items() if k != "secondary_credential_ref"}
        if (
            isinstance(value, dict)
            and value.get("provider") == "google_ads"
            and value.get("purpose") == "EDIT"
            and value.get("credential_source") == "OS_OAUTH_GOOGLE"
            and not {"currency", "timezone"} <= value.keys()
        ):
            raise ValueError("Google Ads EDIT requires explicit currency and timezone")
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version_one(value)

    @model_validator(mode="after")
    def provider_specific_auth_metadata(self) -> "Profile":
        if is_reserved_credential_reference(self.credential_ref):
            raise ValueError("Internal key references cannot be provider credentials")
        if self.provider != "google_ads" and self.login_customer_ref is not None:
            raise ValueError("Manager-login references apply only to Google Ads")
        if self.credential_source == "OS_OAUTH_GOOGLE":
            expected_scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE.get(
                (self.provider, self.purpose)
            )
            if expected_scope is None or self.scopes != [expected_scope]:
                raise ValueError("Google OAuth profiles require their exact supported API scope")
            if not is_google_oauth_client_id(self.client_ref):
                raise ValueError("Google OAuth profiles require a supported installed client ID")
            if not is_google_oauth_operational_principal_ref(self.principal_ref):
                raise ValueError("Google OAuth profiles require a derived OIDC principal reference")
            if self.provider == "google_ads" and self.purpose == "EDIT" and self.currency is None:
                raise ValueError("Google Ads EDIT requires the declared account currency")
            if self.provider in {"ga4", "ga4_admin"} and self.currency is None:
                raise ValueError("Google OAuth GA4 profiles require a declared property currency")
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Google OAuth profiles require an IANA timezone") from None
            account_pattern = r"[0-9]{10}" if self.provider == "google_ads" else r"[0-9]{1,20}"
            if re.fullmatch(account_pattern, self.account_ref) is None:
                raise ValueError("Google OAuth profiles require the provider's native resource ID")
        return self


class OpenAIAccountKeyProfile(StrictModel):
    """OpenAI Ads account-key profile without invented OAuth metadata."""

    schema_version: Literal[1] = 1
    profile_kind: Literal["OPENAI_ADS_ACCOUNT_KEY"]
    provider: Literal["openai_ads"] = "openai_ads"
    provider_scope_status: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    profile_id: SafeRef
    account_ref: OpenAIAccountClientRef
    purpose: Literal["READ", "EDIT", "ACTIVATE", "PUBLISH"] = "READ"
    currency: Currency | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=80)] = "UTC"
    credential_source: Literal["OS_STORE", "EXPLICIT_CI"] = "OS_STORE"
    credential_ref: SafeRef

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version_one(value)

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def account_key_reference_is_safe(self) -> "OpenAIAccountKeyProfile":
        if is_reserved_credential_reference(self.credential_ref):
            raise ValueError("Internal key references cannot be provider credentials")
        return self


MetaNativeIdentifier = Annotated[
    str, Field(min_length=1, max_length=40, pattern=r"^[1-9][0-9]{0,39}$")
]


class MetaAccountTokenProfile(StrictModel):
    """Customer-declared Meta account/auth target for the fixed token inspector.

    This profile deliberately carries no raw token, user principal, scope
    observation, expiry, or account-access assertion. Its currency and
    timezone are customer-declared expectations, not current-account facts.
    The protected bundle is the only credential-bearing state and must be
    checked by the dedicated Meta-token lifecycle before the reviewed
    inspection GET.
    """

    schema_version: Literal[1] = 1
    profile_kind: Literal["META_ADS_ACCOUNT_TOKEN"]
    provider: Literal["meta_ads"] = "meta_ads"
    profile_id: SafeRef
    account_ref: MetaNativeIdentifier
    client_application_ref: MetaNativeIdentifier
    currency: Currency
    timezone: Annotated[str, Field(min_length=1, max_length=80)]
    purpose: Literal["READ", "EDIT"] = "READ"
    inspector_credential_kind: Literal[
        "CUSTOMER_APP_ACCESS_TOKEN", "CUSTOMER_APP_DEVELOPER_USER_TOKEN"
    ]
    credential_source: Literal["OS_META_TOKEN"] = "OS_META_TOKEN"
    credential_ref: SafeRef

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version_one(value)

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @property
    def required_scope(self) -> Literal["ads_read", "ads_management"]:
        return "ads_read" if self.purpose == "READ" else "ads_management"

    @model_validator(mode="after")
    def fixed_bundle_reference(self) -> "MetaAccountTokenProfile":
        if self.credential_ref != meta_token_credential_reference(self.profile_id):
            raise ValueError("Meta token credential reference must be derived from the profile ID")
        return self


ConfiguredProfile = Profile | OpenAIAccountKeyProfile | MetaAccountTokenProfile
_PROFILE_ADAPTER: TypeAdapter[ConfiguredProfile] = TypeAdapter(ConfiguredProfile)


class _ConfiguredProfileInput(RootModel[ConfiguredProfile]):
    root: ConfiguredProfile


def validate_configured_profile(value: object) -> ConfiguredProfile:
    """Revalidate all three closed profile shapes without altering legacy bytes."""
    if isinstance(value, OpenAIAccountKeyProfile):
        return OpenAIAccountKeyProfile.model_validate(
            value.model_dump(mode="python", warnings="error")
        )
    if isinstance(value, MetaAccountTokenProfile):
        return MetaAccountTokenProfile.model_validate(
            value.model_dump(mode="python", warnings="error")
        )
    if isinstance(value, Profile):
        return Profile.model_validate(value.model_dump(mode="python", warnings="error"))
    return _PROFILE_ADAPTER.validate_python(value)


def validate_configured_profile_json(raw: str | bytes) -> ConfiguredProfile:
    from adhelm.strict_json import validate_json_input

    return validate_json_input(_ConfiguredProfileInput, raw).root


class ArtifactRule(StrictModel):
    workflow: Literal["audit", "report", "plan", "verify", "diagnostics"]
    stage: Literal["completion", "before_approval", "meaningful_change", "failure"]
    providers: list[Provider] = Field(default_factory=list)
    profiles: list[SafeRef] = Field(default_factory=list)
    formats: list[Literal["json", "csv", "html"]] = Field(min_length=1, max_length=3)


class OutputPreferences(StrictModel):
    schema_version: Literal[1] = 1
    artifact_mode: Literal["OFF", "ON_REQUEST", "RULES"] = "ON_REQUEST"
    purposes: list[
        Literal["audit", "comparison", "plan_review", "integration_map", "result", "diagnostics"]
    ] = Field(default_factory=list)
    workflow_rules: list[ArtifactRule] = Field(default_factory=list, max_length=50)
    formats: list[Literal["json", "csv", "html"]] = Field(default_factory=list, max_length=3)
    output_directory: str | None = None
    detail: Literal["compact", "standard", "detailed"] = "compact"
    max_rows: Annotated[int, Field(ge=1, le=500)] = 50
    max_output_bytes: Annotated[int, Field(ge=1024, le=1_048_576)] = 65_536
    regeneration: Literal["REUSE", "REGENERATE"] = "REUSE"
    overwrite_policy: Literal["NEW_VERSION", "REPLACE_AUTHORIZED"] = "NEW_VERSION"


class RetentionPreferences(StrictModel):
    schema_version: Literal[1] = 1
    execution: Literal["MANUAL_PREVIEW"] = "MANUAL_PREVIEW"
    changes_keep_days: Annotated[int, Field(ge=1)] | None = None
    performance_keep_days: Annotated[int, Field(ge=1)] | None = None
    creative_keep_days: Annotated[int, Field(ge=1)] | None = None
    assets_keep_days: Annotated[int, Field(ge=1)] | None = None
    exports_keep_days: Annotated[int, Field(ge=1)] | None = None
    caches_keep_days: Annotated[int, Field(ge=1)] | None = None


class Configuration(StrictModel):
    schema_version: Literal[1] = 1
    profiles: list[ConfiguredProfile] = Field(default_factory=list, max_length=100)
    output: OutputPreferences = Field(default_factory=OutputPreferences)
    retention: RetentionPreferences = Field(default_factory=RetentionPreferences)
    read_index: Literal["sqlite"] = "sqlite"
    analytics_engine: Literal["disabled"] = "disabled"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_schema_version_one(value)

    @model_validator(mode="after")
    def unique_profiles(self) -> "Configuration":
        profiles = [validate_configured_profile(p) for p in self.profiles]
        ids = [p.profile_id for p in profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("Profile IDs must be unique")
        return self


def validate_configuration(value: object) -> Configuration:
    """Detach a potentially bypassed configuration before local side effects."""
    if isinstance(value, Configuration):
        return Configuration.model_validate(value.model_dump(mode="python", warnings="error"))
    return Configuration.model_validate(value)


class Diagnostic(StrictModel):
    code: str
    message: str
    next_action: str
    capability: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_code: str | None = Field(default=None, max_length=160)
    provider_request_id: str | None = Field(default=None, max_length=128)

    @model_serializer(mode="wrap")
    def omit_absent_provider_details(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        result: dict[str, Any] = dict(handler(self))
        for key in ("http_status", "provider_code", "provider_request_id"):
            if result.get(key) is None:
                result.pop(key, None)
        return result


class ProviderReleaseLabel(StrictModel):
    """The release label and meaning of one provider an output names."""

    provider: Annotated[str, Field(min_length=1, max_length=20)]
    label: Literal["LIVE_PILOTED", "EXPERIMENTAL"]
    meaning: Annotated[str, Field(min_length=1, max_length=200)]
    pilot_record_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_digest(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # An `EXPERIMENTAL` provider has no record; keep every output that names
        # one as small as the minimum output bound requires.
        serialized: dict[str, Any] = handler(self)
        if serialized.get("pilot_record_digest") is None:
            serialized.pop("pilot_record_digest", None)
        return serialized


class ResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    tool_version: str
    command: str
    outcome: str
    data: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    next_cursor: str | None = None
    total_count: int | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    #: Derived from the command and data, never supplied: every output that names
    #: a released provider states its release label (docs/release-0.1.0.md).
    provider_labels: list[ProviderReleaseLabel] = Field(default_factory=list)

    @model_validator(mode="after")
    def screen_output(self) -> "ResultEnvelope":
        validate_data(self.model_dump(mode="json"))
        return self

    @model_serializer(mode="wrap")
    def omit_empty_provider_labels(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Most outputs name no released provider; they stay byte-identical to the
        # envelope before release labels, which the minimum output bound relies on.
        serialized: dict[str, Any] = handler(self)
        if not serialized.get("provider_labels"):
            serialized.pop("provider_labels", None)
        return serialized

    @model_validator(mode="after")
    def label_named_providers(self) -> "ResultEnvelope":
        # Imported here because the label loader validates pilot records, which
        # are strict models defined above, so this module cannot import it first.
        from adhelm.release_labels import labels_for_output

        self.provider_labels = labels_for_output(self.command, self.data, self.outcome)
        return self
