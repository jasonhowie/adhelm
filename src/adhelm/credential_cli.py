"""Explicit local terminal credential handoff and bounded non-secret status."""

from dataclasses import asdict
from enum import StrEnum
from typing import Annotated

import typer

from adhelm import credential_setup
from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError
from adhelm.models import ConfiguredProfile

app = typer.Typer(no_args_is_help=True)


class Slot(StrEnum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


def _selected(profiles: list[ConfiguredProfile], profile_id: str) -> ConfiguredProfile:
    selected = [profile for profile in profiles if profile.profile_id == profile_id]
    if len(selected) != 1:
        raise AdhelmError("PROFILE_REQUIRED", "Select exactly one configured profile.", exit_code=2)
    return selected[0]


def _result_data(
    profile: ConfiguredProfile, results: tuple[credential_setup.CredentialSetupResult, ...]
) -> dict[str, object]:
    # The invoking agent already has its selected profile argument. Return the
    # exact configuration digest without exposing customer-selected aliases or
    # requiring creation/access of the unrelated private export key.
    slots = []
    for result in results:
        value = asdict(result)
        value.pop("credential_ref")
        value.pop("profile_id")
        slots.append(value)
    return {
        "profile_configuration_digest": digest(profile),
        "local_only": True,
        "provider_authentication_verified": False,
        "slots": slots,
    }


@app.command("status")
def status(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
) -> None:
    """Inspect selected local credential slots without retrieving secret output."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        configuration = load_configuration(root(ctx))
        profile = _selected(configuration.profiles, profile_id)
        results = credential_setup.credential_status(configuration, profile_id)
        emit(
            "auth status",
            _result_data(profile, results),
            maximum_bytes=configuration.output.max_output_bytes,
        )

    guarded("auth status", work)


def _mutate(
    ctx: typer.Context,
    profile_id: str,
    slot: Slot,
    operation: str,
    interactive: bool,
) -> None:
    from adhelm.cli import emit, guarded, root

    command = "auth " + operation

    def work() -> None:
        if operation != "remove-local" and not interactive:
            raise AdhelmError(
                "EXPLICIT_TERMINAL_HANDOFF_REQUIRED",
                "Credential entry requires an explicitly requested local hidden terminal prompt.",
                exit_code=4,
                next_action=(
                    "Run this command locally with --interactive; do not send secrets in chat."
                ),
            )
        configuration = load_configuration(root(ctx))
        profile = _selected(configuration.profiles, profile_id)
        selected_slot: credential_setup.CredentialSlot = (
            "PRIMARY" if slot is Slot.PRIMARY else "SECONDARY"
        )
        try:
            if operation == "remove-local":
                result = credential_setup.local_remove(configuration, profile_id, selected_slot)
            else:
                action = (
                    credential_setup.rotate_interactive
                    if operation == "rotate"
                    else credential_setup.store_interactive
                )
                result = action(configuration, profile_id, selected_slot)
        except AdhelmError as error:
            if error.code != "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED":
                raise
            emit(
                command,
                {
                    "profile_configuration_digest": digest(profile),
                    "provider": profile.provider,
                    "slot": selected_slot,
                    "local_only": True,
                    "local_state": "UNVERIFIED",
                    "failure_code": error.code,
                    "remote_revocation_verified": False,
                    "next_action": "Inspect local auth status before a deliberate new action.",
                },
                outcome="LOCAL_CHANGE_UNVERIFIED",
                maximum_bytes=configuration.output.max_output_bytes,
            )
            raise typer.Exit(6) from None
        emit(
            command,
            _result_data(profile, (result,)),
            maximum_bytes=configuration.output.max_output_bytes,
        )

    guarded(command, work)


@app.command("store")
def store(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
    slot: Annotated[Slot, typer.Option("--slot")] = Slot.PRIMARY,
    interactive: Annotated[bool, typer.Option("--interactive")] = False,
) -> None:
    """Store a missing local secret through an explicitly selected hidden terminal prompt."""
    _mutate(ctx, profile_id, slot, "store", interactive)


@app.command("rotate")
def rotate(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
    slot: Annotated[Slot, typer.Option("--slot")] = Slot.PRIMARY,
    interactive: Annotated[bool, typer.Option("--interactive")] = False,
) -> None:
    """Explicitly replace a local secret; this does not rotate or revoke it at the provider."""
    _mutate(ctx, profile_id, slot, "rotate", interactive)


@app.command("remove-local")
def remove_local(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--profile")],
    slot: Annotated[Slot, typer.Option("--slot")] = Slot.PRIMARY,
) -> None:
    """Remove the selected local entry; provider revocation remains unverified."""
    _mutate(ctx, profile_id, slot, "remove-local", False)
