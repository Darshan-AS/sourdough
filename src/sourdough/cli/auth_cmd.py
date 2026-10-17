"""Authentication and CouchDB replication status commands (`dough auth ...`)."""

from typing import Annotated

from rich.panel import Panel
from rich.table import Table
import typer

from sourdough.cli.common import (
    HELP_CONTEXT_SETTINGS,
    console,
    emit_json,
    get_service,
    handle_error,
)
from sourdough.exceptions import WalletError

auth_app = typer.Typer(
    help="Inspect or refresh CouchDB replication credentials.",
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
)


@auth_app.command("status")
def auth_status(
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Verify CouchDB connection and display database status and file paths."""
    try:
        service = get_service(state_file)
        status = service.auth_status()
        if as_json:
            emit_json(status)
            return

        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan")
        grid.add_column()
        grid.add_row("Email:", str(status["email"]))
        grid.add_row("User ID:", str(status["userId"]))
        grid.add_row("Database:", f"{status['dbName']} ([dim]{status['couchUrl']}[/dim])")
        grid.add_row(
            "Active Docs:",
            f"[bold green]{status['docCount']}[/bold green] (deleted: {status['deletedDocCount']})",
        )
        grid.add_row("Last Record ID:", str(status["lastRecordId"]))
        grid.add_row("State File:", f"[dim]{status['stateFile']}[/dim]")
        grid.add_row("Cache File:", f"[dim]{status['cacheFile']}[/dim]")
        grid.add_row("Journal File:", f"[dim]{status['journalFile']}[/dim]")
        console.print(
            Panel(
                grid,
                title="[bold]Wallet CouchDB Connection[/bold]",
                border_style="green",
                expand=False,
            )
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@auth_app.command("refresh")
def auth_refresh(
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Refresh CouchDB replication credentials via web.budgetbakers.com sessionToken."""
    try:
        service = get_service(state_file)
        res = service.auth_refresh()
        if as_json:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Refreshed replication credentials[/bold green] for "
            f"[cyan]{res['email']}[/cyan] (db=[bold]{res['dbName']}[/bold], docs={res['docCount']})"
        )
    except WalletError as exc:
        handle_error(exc, as_json)

