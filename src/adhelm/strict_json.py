"""Closed JSON model validation before precision-sensitive canonicalization."""

import json
import math
from typing import Any

from pydantic import BaseModel

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError


def _reject_nonfinite_numbers(value: Any) -> None:
    """Reject numbers that valid JSON syntax overflowed into non-finite floats."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, float):
            if not math.isfinite(current):
                raise AdhelmError("INVALID_JSON", "JSON numbers must be finite.", exit_code=2)
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def validate_json_input[T: BaseModel](model: type[T], raw: str | bytes) -> T:
    """Screen bounded, representable JSON while preserving input number types."""
    screened = parse_json(raw)
    _reject_nonfinite_numbers(screened)
    # Preserve the canonical domain's key and safe-integer limits without
    # feeding canonicalized bytes to Pydantic: RFC 8785 would turn ``1.0``
    # into ``1`` before strict schema validation can reject the float.
    canonical_bytes(screened)
    return model.model_validate_json(raw)


def validate_json_value[T: BaseModel](model: type[T], value: dict[str, Any]) -> T:
    """Validate a JSON-shaped mapping without rounding integer-looking floats.

    The representability check preserves the existing canonical-domain limit,
    including string keys and safe integer range. Its encoded bytes are not
    used for model validation: RFC 8785 intentionally serializes 1.0 as 1.
    Normalization for persistence still happens only after model validation.
    """
    canonical_bytes(value)
    return model.model_validate_json(json.dumps(value, allow_nan=False))
