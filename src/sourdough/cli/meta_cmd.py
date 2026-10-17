"""Metadata listing and cache synchronization commands (`accounts`, `categories`, `currencies`, `labels`, `sync-cache`)."""

from typing import Annotated

from rich import box
from rich.table import Table
import typer

from sourdough.cli.common import console, emit_json, get_service, handle_error
from sourdough.exceptions import WalletError


def register_meta_commands(app: typer.Typer) -> None:
    """Register top-level metadata inspection and cache commands on the root Typer app."""

    @app.command("sync-cache")
    def sync_cache(
        as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
        state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
    ) -> None:
        """Refresh local metadata cache (Accounts, Categories, Currencies, Labels)."""
        try:
            service = get_service(state_file)
            snap = service.sync_cache()
            summary = {
                "synced": True,
                "accounts": len(snap.accounts),
                "categories": len(snap.categories),
                "currencies": len(snap.currencies),
                "labels": len(snap.labels),
                "cacheFile": str(service.paths.cache_file),
            }
            if as_json:
                emit_json(summary)
                return
            console.print(
                f"[bold green]✔ Synced metadata cache[/bold green] "
                f"({summary['accounts']} accounts, {summary['categories']} categories, "
                f"{summary['currencies']} currencies, {summary['labels']} labels) -> [dim]{summary['cacheFile']}[/dim]"
            )
        except WalletError as exc:
            handle_error(exc, as_json)

    @app.command("accounts")
    def list_accounts(
        show_all: Annotated[bool, typer.Option("--all", help="Include archived accounts")] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
        state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
    ) -> None:
        """List financial accounts."""
        try:
            service = get_service(state_file)
            accounts = service.list_accounts(include_archived=show_all)
            currencies = service.cache.get_snapshot().currencies_by_id
            data = [
                {
                    "id": a.doc_id,
                    "name": a.name,
                    "currency": currencies[a.currency_id].code if a.currency_id in currencies else a.currency_id,
                    "locked": a.locked,
                    "archived": a.archived,
                    "bankConnected": a.bank_connected,
                }
                for a in accounts
            ]
            if as_json:
                emit_json(data)
                return

            table = Table(title="Wallet Accounts", box=box.ROUNDED)
            table.add_column("Name", style="bold")
            table.add_column("Currency", style="cyan")
            table.add_column("Locked")
            table.add_column("Bank Sync")
            table.add_column("ID", style="dim")
            for d in data:
                table.add_row(
                    d["name"],
                    d["currency"],
                    "[yellow]yes[/yellow]" if d["locked"] else "[green]no[/green]",
                    "[cyan]yes[/cyan]" if d["bankConnected"] else "[dim]no[/dim]",
                    d["id"],
                )
            console.print(table)
        except WalletError as exc:
            handle_error(exc, as_json)

    @app.command("categories")
    def list_categories(
        search: Annotated[str | None, typer.Option("--search", "-s", help="Filter by name or envelopeId")] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
        state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
    ) -> None:
        """List categories."""
        try:
            service = get_service(state_file)
            cats = service.list_categories(search=search)
            data = [
                {
                    "id": c.doc_id,
                    "name": c.display_name,
                    "envelopeId": c.envelope_id,
                    "custom": c.custom_category,
                }
                for c in cats
            ]
            if as_json:
                emit_json(data)
                return

            table = Table(title="Wallet Categories", box=box.ROUNDED)
            table.add_column("Name", style="bold")
            table.add_column("Envelope ID", justify="right", style="cyan")
            table.add_column("Custom")
            table.add_column("ID", style="dim")
            for d in data:
                table.add_row(
                    d["name"],
                    str(d["envelopeId"]),
                    "[magenta]yes[/magenta]" if d["custom"] else "[dim]no[/dim]",
                    d["id"],
                )
            console.print(table)
        except WalletError as exc:
            handle_error(exc, as_json)

    @app.command("currencies")
    def list_currencies(
        as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
        state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
    ) -> None:
        """List currencies."""
        try:
            service = get_service(state_file)
            curs = service.list_currencies()
            data = [
                {
                    "id": c.doc_id,
                    "code": c.code,
                    "referential": c.referential,
                    "ratioToReferential": c.ratio_to_referential,
                }
                for c in curs
            ]
            if as_json:
                emit_json(data)
                return

            table = Table(title="Wallet Currencies", box=box.ROUNDED)
            table.add_column("Code", style="bold cyan")
            table.add_column("Base Currency")
            table.add_column("Ratio to Base", justify="right")
            table.add_column("ID", style="dim")
            for d in data:
                table.add_row(
                    d["code"],
                    "[bold green]yes[/bold green]" if d["referential"] else "[dim]no[/dim]",
                    f"{d['ratioToReferential']:.6f}",
                    d["id"],
                )
            console.print(table)
        except WalletError as exc:
            handle_error(exc, as_json)

    @app.command("labels")
    def list_labels(
        show_all: Annotated[bool, typer.Option("--all", help="Include archived labels")] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
        state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
    ) -> None:
        """List labels/hashtags."""
        try:
            service = get_service(state_file)
            lbls = service.list_labels(include_archived=show_all)
            data = [{"id": l.doc_id, "name": l.name, "archived": l.archived} for l in lbls]
            if as_json:
                emit_json(data)
                return

            table = Table(title="Wallet Labels", box=box.ROUNDED)
            table.add_column("Name", style="bold")
            table.add_column("Archived")
            table.add_column("ID", style="dim")
            for d in data:
                table.add_row(
                    d["name"],
                    "[yellow]yes[/yellow]" if d["archived"] else "[dim]no[/dim]",
                    d["id"],
                )
            console.print(table)
        except WalletError as exc:
            handle_error(exc, as_json)

