"""Typer + Rich CLI entrypoint for Sourdough (`dough` CLI)."""

from typing import Annotated

import typer

from sourdough.cli.auth_cmd import auth_app
from sourdough.cli.common import (
    HELP_CONTEXT_SETTINGS,
    complete_account,
    complete_category,
    complete_label,
)
from sourdough.cli.meta_cmd import register_meta_commands
from sourdough.cli.records_cmd import record_app
from sourdough.cli.transfer_cmd import transfer_app

__all__ = [
    "app",
    "complete_account",
    "complete_category",
    "complete_label",
    "main",
    "run_cli",
]

app = typer.Typer(
    name="dough",
    help="Sourdough (`dough`): Safe, strictly-validated CLI & SDK for Wallet by BudgetBakers CouchDB sync.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings=HELP_CONTEXT_SETTINGS,
)

app.add_typer(auth_app, name="auth")
app.add_typer(record_app, name="record")
app.add_typer(transfer_app, name="transfer")
register_meta_commands(app)


@app.command("help")
def show_help(
    command_path: Annotated[
        list[str] | None,
        typer.Argument(help="Optional command or subcommand path (e.g. 'record add')"),
    ] = None,
) -> None:
    """Display usage help for the CLI or a specific subcommand (e.g. `dough help record add`)."""
    target = list(command_path) if command_path else []
    app(args=[*target, "--help"], standalone_mode=False)


def run_cli(argv: list[str] | None = None) -> int:
    """Programmatic CLI runner returning an exit code (used by tests and scripts)."""
    try:
        app(args=argv, standalone_mode=False)
        return 0
    except typer.Exit as exc:
        return int(exc.exit_code)


def main() -> None:
    """Console script entrypoint registered in pyproject.toml."""
    app()


if __name__ == "__main__":
    main()
