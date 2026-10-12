"""Generate the closed synthetic Meta campaign PAUSE approval fixture.

This generator uses only the pure M40 action contract and Python receipt wire
protocol.  It opens no configuration, credential store, provider, native host,
or signing key.  Run it from the repository root with:

``UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run python
native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause/
generate_meta_pause_approval_vectors.py``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from adhelm.approval_protocol import (
    ApprovalHostConfiguration,
    approval_review_facts,
    build_meta_campaign_pause_challenge,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPauseAccountBinding,
    MetaCampaignPauseCampaignBaseline,
    MetaCampaignPauseExecutionBinding,
    MetaCampaignPauseObservationBinding,
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
)

FIXTURE_PATH = Path(__file__).with_name("meta-campaign-pause-approval-v1.json")
FRACTIONAL_FIXTURE_PATH = Path(__file__).with_name(
    "meta-campaign-pause-approval-fractional-v1.json"
)
NOW = datetime(2030, 1, 1, tzinfo=UTC)
FRACTIONAL_NOW = datetime(2030, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
CONFIGURATION = ApprovalHostConfiguration(
    host_ref="host:adhelm-macos-approval-v1",
    signing_key_ref="approval-key:fixture-v1",
)


def _action(now: datetime) -> MetaCampaignPausePlannedAction:
    return build_meta_campaign_pause_plan(
        workspace_profile_ref="workspace.meta.fixture",
        account_binding=MetaCampaignPauseAccountBinding(
            account_ref="meta_ads.account.act_123456",
            account_client_ref="act_123456",
            timezone="UTC",
            currency="USD",
            account_status="1",
        ),
        execution_binding=MetaCampaignPauseExecutionBinding(
            profile_ref="meta-edit-fixture",
            client_application_ref="555555",
            principal_ref="meta_token_principal_v1." + "a" * 64,
            credential_binding_ref="credential-binding:sha256:" + "b" * 64,
            configuration_digest="c" * 64,
            profile_configuration_digest="d" * 64,
            inspected_at=now - timedelta(seconds=2),
            expires_at=now + timedelta(hours=1),
            data_access_expires_at=now + timedelta(hours=2),
        ),
        observation_binding=MetaCampaignPauseObservationBinding(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref="meta_ads.campaign.987654",
                campaign_client_ref="987654",
                account_client_ref="123456",
                effective_status="WITH_ISSUES",
            ),
            read_started_at=now - timedelta(seconds=1),
            read_finished_at=now,
        ),
        expires_at=now + timedelta(minutes=10),
    )


def _mapping(data: bytes) -> dict[str, Any]:
    value = parse_json(data)
    if type(value) is not dict:  # pragma: no cover - closed protocol invariant
        raise ValueError("fixture wire must be an object")
    return value


def _build_fixture(
    *, now: datetime, receipt_issued_at: datetime, fixture_kind: str
) -> dict[str, Any]:
    """Return one canonical, unsigned cross-language fixture data object."""

    challenge = build_meta_campaign_pause_challenge(
        _action(now),
        grantor_ref="customer-grantor-1",
        expires_at=now + timedelta(minutes=4),
        configuration=CONFIGURATION,
        nonce=bytes(range(96, 128)),
        issued_at=now,
        now=now,
    )
    review = approval_review_facts(challenge, configuration=CONFIGURATION)
    unsigned_receipt = receipt_signing_payload(
        challenge,
        configuration=CONFIGURATION,
        receipt_id="approval-receipt:" + "M" * 43,
        issued_at=receipt_issued_at,
    )
    return {
        "schema_version": 1,
        "fixture_kind": fixture_kind,
        "scope": "SYNTHETIC_PROTOCOL_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION_OR_EXECUTION",
        "configuration": CONFIGURATION.to_data(),
        "challenge": _mapping(challenge.canonical_bytes),
        "challenge_digest": challenge.digest,
        "review_facts": _mapping(review.canonical_bytes),
        "review_digest": review.digest,
        "unsigned_receipt": _mapping(unsigned_receipt.canonical_unsigned_receipt),
        "unsigned_receipt_digest": hashlib.sha256(
            unsigned_receipt.canonical_unsigned_receipt
        ).hexdigest(),
        "domain_separated_payload_sha256": hashlib.sha256(
            unsigned_receipt.domain_separated_bytes
        ).hexdigest(),
    }


def build_fixture() -> dict[str, Any]:
    """Return the whole-second canonical, unsigned fixture."""

    return _build_fixture(
        now=NOW,
        receipt_issued_at=NOW,
        fixture_kind="META_CAMPAIGN_PAUSE_APPROVAL_V1",
    )


def build_fractional_fixture() -> dict[str, Any]:
    """Return the valid nonzero-six-fraction timestamp parity fixture.

    Every M40 action timestamp uses ``.123456Z``. Receipt issuance uses a
    separately valid millisecond timestamp because the receipt-v1 grammar is
    deliberately three fractional digits.
    """

    return _build_fixture(
        now=FRACTIONAL_NOW,
        receipt_issued_at=FRACTIONAL_NOW + timedelta(microseconds=544),
        fixture_kind="META_CAMPAIGN_PAUSE_APPROVAL_FRACTIONAL_V1",
    )


def main() -> None:
    FIXTURE_PATH.write_bytes(canonical_bytes(build_fixture()))
    FRACTIONAL_FIXTURE_PATH.write_bytes(canonical_bytes(build_fractional_fixture()))


if __name__ == "__main__":
    main()
