"""Progressive views of installed schemas, bound to one complete schema digest."""

from typing import Any

from pydantic import BaseModel

from adhelm.canonical import digest
from adhelm.errors import AdhelmError


def installed_schema_view(
    model: type[BaseModel],
    name: str,
    *,
    root_only: bool = False,
    definitions: bool = False,
    definition: str | None = None,
) -> dict[str, Any]:
    if sum((root_only, definitions, definition is not None)) > 1:
        raise AdhelmError(
            "CONFLICTING_SCHEMA_VIEWS",
            "Select only one schema view.",
            exit_code=2,
            next_action="Use --root-only, --definitions, or --definition NAME separately.",
        )
    schema = model.model_json_schema()
    fragments = schema.get("$defs", {})
    result: dict[str, Any] = {
        "schema_name": name,
        "schema_digest": digest(schema),
        "view": "FULL",
    }
    if root_only:
        result.update(
            view="ROOT",
            schema={key: value for key, value in schema.items() if key != "$defs"},
            definitions_required=bool(fragments),
        )
    elif definitions:
        result.update(view="DEFINITION_CATALOG", definitions=sorted(fragments))
    elif definition is not None:
        if definition not in fragments:
            raise AdhelmError(
                "UNKNOWN_SCHEMA_DEFINITION",
                "Definition is not part of the selected installed schema.",
                exit_code=2,
                next_action="Use schema NAME --definitions to discover exact definition names.",
            )
        result.update(view="DEFINITION", definition=definition, schema=fragments[definition])
    else:
        result["schema"] = schema
    return result
