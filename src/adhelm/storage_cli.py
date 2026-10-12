"""Local storage observations; no retention execution or file cleanup."""

import typer

from adhelm.config import load_configuration
from adhelm.errors import AdhelmError
from adhelm.storage_usage import observe_local_storage

app = typer.Typer(no_args_is_help=True)


@app.command("usage")
def usage(ctx: typer.Context) -> None:
    """Count local file lengths by category without reading file contents."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        configuration = load_configuration(root(ctx))
        result = observe_local_storage(
            root(ctx), output_directory=configuration.output.output_directory
        )
        try:
            emit(
                "storage usage",
                result,
                outcome="LOCAL_OBSERVATION",
                maximum_bytes=configuration.output.max_output_bytes,
            )
        except AdhelmError as error:
            if error.code != "OUTPUT_LIMIT_EXCEEDED":
                raise
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "The complete storage observation exceeds the saved output bound.",
                next_action="Increase saved max_output_bytes; category totals cannot be omitted.",
            ) from None

    guarded("storage usage", work)
