"""LinkedIn and OpenAI report references join the inventory this product retained."""

from __future__ import annotations

import pytest

from adhelm.errors import AdhelmError
from adhelm.saved_audit_evidence import retained_account_key


@pytest.mark.parametrize(
    ("provider", "canonical", "native"),
    [
        ("linkedin_ads", "linkedin_ads.account.456", "456"),
        ("openai_ads", "openai_ads.account.account_fixture", "account_fixture"),
    ],
)
def test_the_canonical_and_native_spellings_of_one_account_share_one_key(
    provider: str, canonical: str, native: str
) -> None:
    assert retained_account_key(provider, canonical) == native
    assert retained_account_key(provider, native) == native
    assert retained_account_key(provider, native) != retained_account_key(provider, native + "9")
    # Another provider's prefix is never reduced, and this prefix never leaks across providers.
    assert retained_account_key("ga4", canonical) == canonical
    assert retained_account_key("google_ads", canonical) == canonical


@pytest.mark.parametrize("reference", ["linkedin_ads.account.", ""])
def test_a_linkedin_reference_that_reduces_to_nothing_is_refused(reference: str) -> None:
    with pytest.raises(AdhelmError) as error:
        retained_account_key("linkedin_ads", reference)

    assert (error.value.code, error.value.exit_code) == ("INVALID_AUDIT_EVIDENCE_SELECTION", 2)


@pytest.mark.parametrize("reference", ["openai_ads.account.", ""])
def test_an_openai_reference_that_reduces_to_nothing_is_refused(reference: str) -> None:
    with pytest.raises(AdhelmError) as error:
        retained_account_key("openai_ads", reference)

    assert (error.value.code, error.value.exit_code) == ("INVALID_AUDIT_EVIDENCE_SELECTION", 2)
