"""Regression checks for bounded sensitive-text screening."""

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from adhelm.errors import AdhelmError
from adhelm.privacy import _EMAIL, validate_data

_LEGACY_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_SCREEN_DEADLINE_SECONDS = 10


@pytest.mark.parametrize(
    ("value", "matches_email"),
    (
        ("plain conversion declaration", False),
        ("person@example.test", True),
        ("prefix+person@example.test, suffix", True),
        # The legacy matcher can start after an earlier @ and can use a later
        # dot in a domain run.  Keep those deliberately broad matches intact.
        ("a@b@c.test", True),
        ("person@.x.test", True),
        ("person@localhost", False),
        ("person@.test", False),
        ("person@host.", False),
        ("person @ host.test", False),
        # validate_data normalizes NFKC before screening.
        ("Ｐｅｒｓｏｎ＠ｅｘａｍｐｌｅ．ｔｅｓｔ", True),
        ("\u2003person@example.test", True),
    ),
)
def test_boundary_email_matcher_preserves_short_legacy_corpus(
    value: str, matches_email: bool
) -> None:
    normalized = unicodedata.normalize("NFKC", value)

    assert bool(_LEGACY_EMAIL.search(normalized)) is matches_email
    assert bool(_EMAIL.search(normalized)) is matches_email
    if matches_email:
        with pytest.raises(AdhelmError) as error:
            validate_data(value)
        assert error.value.code == "SENSITIVE_DATA_RISK"
    else:
        validate_data(value)


@pytest.mark.parametrize(
    "value",
    (
        "contact person@example.test",
        "Bearer synthetic-fixture-token",
        "(312) 555-1234",
    ),
)
def test_existing_sensitive_text_fixtures_remain_rejected(value: str) -> None:
    with pytest.raises(AdhelmError) as error:
        validate_data(value)
    assert error.value.code == "SENSITIVE_DATA_RISK"


@pytest.mark.parametrize(
    "expression",
    (
        '"x" * 600_000',
        '"x" * 599_990 + "@nodomain"',
    ),
)
def test_long_nonmatching_email_shapes_finish_within_bounded_deadline(expression: str) -> None:
    """Run hostile shapes out of process so timing failures cannot hang pytest."""
    program = (
        f"from adhelm.privacy import validate_data\nvalue = {expression}\nvalidate_data(value)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SCREEN_DEADLINE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Sensitive-text screening exceeded the bounded subprocess deadline.")

    assert result.returncode == 0, result.stderr
