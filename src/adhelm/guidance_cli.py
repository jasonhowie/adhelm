"""Bounded local guidance discovery, without invoking or installing an agent."""

from typing import Any

import typer

from adhelm import __version__
from adhelm.canonical import canonical_bytes
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError
from adhelm.guidance import guidance_catalog, guidance_document
from adhelm.models import ResultEnvelope

app = typer.Typer(no_args_is_help=True)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 2) -> None:
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        preferences = load_configuration(root(ctx)).output
        if cursor < 0 or not 1 <= limit <= 2:
            raise AdhelmError(
                "INVALID_GUIDANCE_CURSOR", "Invalid guidance catalog page.", exit_code=2
            )
        document = guidance_catalog()
        skills = document.pop("guidance")
        total = len(skills)
        if cursor > total:
            raise AdhelmError(
                "INVALID_GUIDANCE_CURSOR", "Invalid guidance catalog page.", exit_code=2
            )
        count = min(limit, preferences.max_rows, max(0, total - cursor))
        while True:
            next_cursor = str(cursor + count) if cursor + count < total else None
            data = {**document, "guidance": skills[cursor : cursor + count]}
            envelope = ResultEnvelope(
                tool_version=__version__,
                command="guidance catalog",
                outcome="OK",
                data=data,
                next_cursor=next_cursor,
                total_count=total,
            )
            if len(canonical_bytes(envelope)) + 1 <= preferences.max_output_bytes:
                break
            if count <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED", "Guidance metadata exceeds the output limit."
                )
            count -= 1
        emit(
            "guidance catalog",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=next_cursor,
            total_count=total,
        )

    guarded("guidance catalog", work)


@app.command("show")
def show(ctx: typer.Context, name: str, section: str = "main", cursor: int = 0) -> None:
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        document = guidance_document(name, section)
        text = str(document.pop("text"))
        if not 0 <= cursor <= len(text):
            raise AdhelmError(
                "INVALID_GUIDANCE_CURSOR", "Cursor is outside this guidance document.", exit_code=2
            )
        maximum = load_configuration(root(ctx)).output.max_output_bytes

        def page(end: int) -> tuple[dict[str, Any], str | None]:
            return {**document, "cursor": cursor, "text": text[cursor:end]}, str(end) if end < len(
                text
            ) else None

        def size(end: int) -> int:
            data, next_cursor = page(end)
            return (
                len(
                    canonical_bytes(
                        ResultEnvelope(
                            tool_version=__version__,
                            command="guidance show",
                            outcome="OK",
                            data=data,
                            next_cursor=next_cursor,
                            total_count=len(text),
                        )
                    )
                )
                + 1
            )

        lower, upper = cursor, len(text)
        # A byte-bounded JSON page keeps Unicode intact and accounts for escaped
        # characters, document metadata, cursor digits, and the output newline.
        while lower < upper:
            middle = (lower + upper + 1) // 2
            if size(middle) <= maximum:
                lower = middle
            else:
                upper = middle - 1
        if size(lower) > maximum or (lower == cursor and cursor < len(text)):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Guidance metadata exceeds the output limit.",
                next_action="Increase the saved output limit.",
            )
        data, next_cursor = page(lower)
        emit(
            "guidance show",
            data,
            maximum_bytes=maximum,
            next_cursor=next_cursor,
            total_count=len(text),
        )

    guarded("guidance show", work)
