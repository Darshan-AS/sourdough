"""Metadata listing, Category/Label management, Journal inspection, and cache sync commands."""

from typing import Annotated

from rich import box
from rich.table import Table
import typer

from sourdough.cli.common import (
    HELP_CONTEXT_SETTINGS,
    complete_category,
    complete_label,
    console,
    emit_json,
    get_service,
    handle_error,
)
from sourdough.domain.enums import ENVELOPE_NAMES
from sourdough.exceptions import WalletError

categories_app = typer.Typer(
    help="List, create, rename, move, or delete categories.",
    context_settings=HELP_CONTEXT_SETTINGS,
)

labels_app = typer.Typer(
    help="List, create, rename, or delete labels/hashtags.",
    context_settings=HELP_CONTEXT_SETTINGS,
)


def _render_categories_list(
    search: str | None,
    as_json: bool,
    state_file: str | None,
) -> None:
    try:
        service = get_service(state_file)
        cats = service.list_categories(search=search)
        data = [
            {
                "id": c.doc_id,
                "name": c.display_name,
                "envelopeId": c.envelope_id,
                "parentEnvelope": ENVELOPE_NAMES.get(c.envelope_id, ""),
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
        table.add_column("Parent Envelope", style="dim")
        table.add_column("Custom")
        table.add_column("ID", style="dim")
        for d in data:
            table.add_row(
                d["name"],
                str(d["envelopeId"]),
                d["parentEnvelope"] if d["custom"] else "-",
                "[magenta]yes[/magenta]" if d["custom"] else "[dim]no[/dim]",
                d["id"],
            )
        console.print(table)
    except WalletError as exc:
        handle_error(exc, as_json)


@categories_app.callback(invoke_without_command=True)
def categories_default(
    ctx: typer.Context,
    search: Annotated[str | None, typer.Option("--search", "-s", help="Filter by name or envelopeId")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """List categories (or run subcommands: add, rename, move, delete)."""
    if ctx.invoked_subcommand is None:
        _render_categories_list(search=search, as_json=as_json, state_file=state_file)


@categories_app.command("list")
def categories_list(
    search: Annotated[str | None, typer.Option("--search", "-s", help="Filter by name or envelopeId")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """List categories."""
    _render_categories_list(search=search, as_json=as_json, state_file=state_file)


@categories_app.command("add")
def categories_add(
    name: Annotated[str, typer.Argument(help="New custom subcategory name")],
    parent: Annotated[
        str,
        typer.Option("--parent", "-p", help="Parent category name or envelopeId (e.g. '1001' or 'Taxi')", autocompletion=complete_category),
    ],
    color: Annotated[str, typer.Option("--color", help="Hex color code")] = "#ff1744",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Create a custom subcategory under a parent envelope."""
    try:
        service = get_service(state_file)
        res = service.add_category(
            name,
            parent_query=parent,
            color=color,
            dry_run=dry_run,
        )
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Created category[/bold green] [bold]{res['name']}[/bold] "
            f"(envelopeId={res['envelopeId']}, id={res['id']}, [dim]journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@categories_app.command("rename")
def categories_rename(
    category: Annotated[
        str,
        typer.Argument(help="Existing category name, Parent > Child, ID, or envelopeId", autocompletion=complete_category),
    ],
    new_name: Annotated[str, typer.Argument(help="New category display name")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Rename a built-in or custom category."""
    try:
        service = get_service(state_file)
        res = service.rename_category(category, new_name, dry_run=dry_run)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Renamed category[/bold green] [bold]{res['oldName']}[/bold] -> "
            f"[bold cyan]{res['newName']}[/bold cyan] ([dim]id={res['id']}, journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@categories_app.command("move")
def categories_move(
    category: Annotated[
        str,
        typer.Argument(help="Category to move/re-parent", autocompletion=complete_category),
    ],
    parent: Annotated[
        str,
        typer.Option("--parent", "-p", help="New parent category name or envelopeId", autocompletion=complete_category),
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Re-parent a custom subcategory under a different parent envelope."""
    try:
        service = get_service(state_file)
        res = service.move_category(category, parent_query=parent, dry_run=dry_run)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Moved category[/bold green] [bold]{res['name']}[/bold]: "
            f"envelope {res['oldEnvelopeId']} -> {res['newEnvelopeId']} ([dim]journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@categories_app.command("delete")
def categories_delete(
    category: Annotated[
        str,
        typer.Argument(help="Category to soft-delete", autocompletion=complete_category),
    ],
    move_to: Annotated[
        str | None,
        typer.Option("--move-to", help="Target category to migrate existing records into before deleting", autocompletion=complete_category),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Soft-delete a category (optionally migrating referencing records to --move-to)."""
    try:
        service = get_service(state_file)
        res = service.delete_category(category, move_records_to=move_to, dry_run=dry_run)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold yellow]✔ Deleted category[/bold yellow] [bold]{res['name']}[/bold] "
            f"(migrated {res['migratedRecordCount']} record(s), [dim]journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


def _render_labels_list(
    show_all: bool,
    as_json: bool,
    state_file: str | None,
) -> None:
    try:
        service = get_service(state_file)
        lbls = service.list_labels(include_archived=show_all)
        data = [{"id": l.doc_id, "name": l.name, "color": l.color, "archived": l.archived} for l in lbls]
        if as_json:
            emit_json(data)
            return

        table = Table(title="Wallet Labels", box=box.ROUNDED)
        table.add_column("Name", style="bold")
        table.add_column("Color", style="cyan")
        table.add_column("Archived")
        table.add_column("ID", style="dim")
        for d in data:
            table.add_row(
                d["name"],
                d["color"] or "-",
                "[yellow]yes[/yellow]" if d["archived"] else "[dim]no[/dim]",
                d["id"],
            )
        console.print(table)
    except WalletError as exc:
        handle_error(exc, as_json)


@labels_app.callback(invoke_without_command=True)
def labels_default(
    ctx: typer.Context,
    show_all: Annotated[bool, typer.Option("--all", help="Include archived labels")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """List labels/hashtags (or run subcommands: add, rename, delete)."""
    if ctx.invoked_subcommand is None:
        _render_labels_list(show_all=show_all, as_json=as_json, state_file=state_file)


@labels_app.command("list")
def labels_list(
    show_all: Annotated[bool, typer.Option("--all", help="Include archived labels")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """List labels/hashtags."""
    _render_labels_list(show_all=show_all, as_json=as_json, state_file=state_file)


@labels_app.command("add")
def labels_add(
    name: Annotated[str, typer.Argument(help="New label name (without leading #)")],
    color: Annotated[str, typer.Option("--color", help="Hex color code")] = "#26c6da",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Create a new label/hashtag."""
    try:
        service = get_service(state_file)
        res = service.add_label(name, color=color, dry_run=dry_run)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Created label[/bold green] [bold]#{res['name']}[/bold] "
            f"([dim]id={res['id']}, journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@labels_app.command("rename")
def labels_rename(
    label: Annotated[str, typer.Argument(help="Existing label name or ID", autocompletion=complete_label)],
    new_name: Annotated[str, typer.Argument(help="New label name")],
    color: Annotated[str | None, typer.Option("--color", help="Optional new hex color code")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Rename an existing label and/or update its color."""
    try:
        service = get_service(state_file)
        res = service.rename_label(label, new_name, color=color, dry_run=dry_run)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Renamed label[/bold green] [bold]#{res['oldName']}[/bold] -> "
            f"[bold cyan]#{res['newName']}[/bold cyan] ([dim]id={res['id']}, journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@labels_app.command("delete")
def labels_delete(
    label: Annotated[str, typer.Argument(help="Label name or ID to soft-delete", autocompletion=complete_label)],
    force: Annotated[bool, typer.Option("--force", help="Detach label from any referencing records and delete")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Soft-delete a label (requires 0 referencing records unless --force is passed)."""
    try:
        service = get_service(state_file)
        res = service.delete_label(label, force=force, dry_run=dry_run)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold yellow]✔ Deleted label[/bold yellow] [bold]#{res['name']}[/bold] "
            f"(detached from {res['detachedRecordCount']} record(s), [dim]journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


def register_meta_commands(app: typer.Typer) -> None:
    """Register top-level metadata inspection, management, and cache commands on the root Typer app."""
    app.add_typer(categories_app, name="categories")
    app.add_typer(labels_app, name="labels")

    @app.command("sync-cache")
    def sync_cache(
        records: Annotated[
            bool,
            typer.Option("--records", help="Also cache all Record_ documents locally for fast offline/cached listing"),
        ] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
        state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
    ) -> None:
        """Refresh local metadata cache (Accounts, Categories, Currencies, Labels, and optionally Records)."""
        try:
            service = get_service(state_file)
            snap = service.sync_cache(include_records=records)
            cached_recs = service.cache.get_cached_records() if records else None
            summary: dict[str, object] = {
                "synced": True,
                "accounts": len(snap.accounts),
                "categories": len(snap.categories),
                "currencies": len(snap.currencies),
                "labels": len(snap.labels),
                "cacheFile": str(service.paths.cache_file),
            }
            if records and cached_recs is not None:
                summary["records"] = len(cached_recs)
            if as_json:
                emit_json(summary)
                return
            rec_suffix = f", {len(cached_recs)} records" if (records and cached_recs is not None) else ""
            console.print(
                f"[bold green]✔ Synced metadata cache[/bold green] "
                f"({summary['accounts']} accounts, {summary['categories']} categories, "
                f"{summary['currencies']} currencies, {summary['labels']} labels{rec_suffix}) -> [dim]{summary['cacheFile']}[/dim]"
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

    @app.command("journal")
    def list_journal(
        limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum journal entries to show (0 for all)")] = 20,
        as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
        state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
    ) -> None:
        """List recent mutation journal entries for audit and rollback."""
        try:
            service = get_service(state_file)
            entries = service.list_journal(limit=limit)
            if as_json:
                emit_json(entries)
                return

            table = Table(title=f"Mutation Journal (showing {len(entries)})", box=box.ROUNDED)
            table.add_column("Timestamp (UTC)", style="dim")
            table.add_column("Operation", style="bold cyan")
            table.add_column("Before Docs", justify="right")
            table.add_column("After Docs", justify="right")
            table.add_column("Status")
            table.add_column("Entry ID", style="dim")
            for e in entries:
                status_str = "[yellow]undone[/yellow]" if e["undone"] else "[green]active[/green]"
                table.add_row(
                    str(e["timestamp"])[:19].replace("T", " "),
                    str(e["operation"]),
                    str(e["beforeCount"]),
                    str(e["afterCount"]),
                    status_str,
                    str(e["entryId"]),
                )
            console.print(table)
        except WalletError as exc:
            handle_error(exc, as_json)
