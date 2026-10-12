"""RFC 8785 canonical JSON; precision-sensitive values must be integers/strings."""

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import rfc8785
from pydantic import BaseModel

from adhelm.errors import AdhelmError


def canonical_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return rfc8785.dumps(value)
    except (ValueError, TypeError) as exc:
        raise AdhelmError(
            "INVALID_CANONICAL_VALUE", "Value cannot be canonically serialized.", exit_code=2
        ) from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def parse_json(
    raw: str | bytes, *, maximum_bytes: int = 1_048_576, decimal_numbers: bool = False
) -> Any:
    if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > maximum_bytes:
        raise AdhelmError("INPUT_TOO_LARGE", "JSON exceeds the allowed byte limit.", exit_code=2)
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=Decimal if decimal_numbers else float,
        )
    except (ValueError, RecursionError, UnicodeError, InvalidOperation) as exc:
        raise AdhelmError(
            "INVALID_JSON", "Expected bounded JSON with unique keys.", exit_code=2
        ) from exc
