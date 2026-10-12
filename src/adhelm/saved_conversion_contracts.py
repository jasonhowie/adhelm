"""Versioned customer declarations in verified local history, without execution authority."""

import re
from typing import Any

from adhelm.canonical import canonical_bytes
from adhelm.conversion_contracts import (
    ConversionContract,
    ConversionContractInput,
    compile_conversion_contract,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger


def retain_conversion_contract(
    ledger: Ledger, definition: ConversionContractInput
) -> dict[str, Any]:
    contract = compile_conversion_contract(definition)
    # The ledger validates the version/supersedes/effective-time chain while
    # holding its stream lock, so a competing registration cannot fork it.
    return ledger.append("changes", "CONVERSION_CONTRACT", contract.model_dump(mode="json"))


def load_conversion_contract(
    ledger: Ledger, record_digest: str
) -> tuple[ConversionContract, dict[str, Any]]:
    if not isinstance(record_digest, str) or re.fullmatch(r"[0-9a-f]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_CONVERSION_SELECTION", "A retained record digest is required.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in checkpoint["records_by_stream"]["changes"]
        if record["record_kind"] == "CONVERSION_CONTRACT"
        and record["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "CONVERSION_NOT_RETAINED", "The selected definition is absent from verified history."
        )
    record = records[0]
    contract = ConversionContract.model_validate_json(canonical_bytes(record["payload"]))
    return contract, {
        "record_digest": record["record_digest"],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": record["recorded_at"],
    }


def conversion_catalog(ledger: Ledger, *, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_CONVERSION_SELECTION", "Invalid conversion catalog pagination.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in reversed(checkpoint["records_by_stream"]["changes"])
        if record["record_kind"] == "CONVERSION_CONTRACT"
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_CONVERSION_SELECTION",
            "Cursor exceeds the retained version count.",
            exit_code=2,
        )
    return {
        "contracts": [
            {
                "record_digest": record["record_digest"],
                "content_digest": record["payload"]["content_digest"],
                "contract_ref": record["payload"]["definition"]["contract_id"],
                "version": record["payload"]["definition"]["version"],
                "effective_from": record["payload"]["definition"]["effective_from"],
                "provenance": "CUSTOMER_DECLARED",
                "execution_authority": False,
            }
            for record in records[cursor : cursor + limit]
        ],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
    }
