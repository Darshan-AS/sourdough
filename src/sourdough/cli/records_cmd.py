"""Record CRUD and rollback commands (`dough record ...`)."""

from typing import Annotated

from rich import box
from rich.table import Table
import typer

from sourdough.cli.common import (
    HELP_CONTEXT_SETTINGS,
    complete_account,
    complete_category,
    complete_label,
    console,
    emit_json,
    get_service,
    handle_error,
)
from sourdough.exceptions import WalletError

record_app = typer.Typer(
    help="Create, list, inspect, update, delete, or undo records.",
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
)


@record_app.command("list")
def record_list(
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="Filter by account name or ID", autocompletion=complete_account),
    ] = None,
    category: Annotated[
        str | None,
        typer.Option("--category", "-c", help="Filter by category name or ID", autocompletion=complete_category),
    ] = None,
    search: Annotated[str | None, typer.Option("--search", "-s", help="Substring search in note/payee")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum records to return")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """List recent financial records."""
    try:
        service = get_service(state_file)
        rows = service.list_records(
            account_query=account,
            category_query=category,
            search=search,
            limit=limit,
        )
        if as_json:
            emit_json(rows)
            return

        table = Table(title=f"Recent Records (showing {len(rows)})", box=box.ROUNDED)
        table.add_column("Date (UTC)", style="dim")
        table.add_column("Account", style="bold")
        table.add_column("Category", style="cyan")
        table.add_column("Amount", justify="right")
        table.add_column("Payee")
        table.add_column("Note")
        table.add_column("ID", style="dim")

        for r in rows:
            amt = r["signedAmount"]
            amt_str = (
                f"[bold red]{amt:+.2f} {r['currency']}[/bold red]"
                if amt < 0
                else f"[bold green]{amt:+.2f} {r['currency']}[/bold green]"
            )
            table.add_row(
                str(r["recordDate"])[:19].replace("T", " "),
                str(r["account"]),
                str(r["category"]),
                amt_str,
                r["payee"] or "-",
                (r["note"] or "").replace("\n", " ")[:45],
                str(r["id"]),
            )
        console.print(table)
    except WalletError as exc:
        handle_error(exc, as_json)


@record_app.command("get")
def record_get(
    record_id: Annotated[str, typer.Argument(help="Record ID (Record_<uuid> or <uuid>)")],
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Fetch a single record by ID."""
    try:
        service = get_service(state_file)
        rec = service.get_record(record_id)
        emit_json(rec)
    except WalletError as exc:
        handle_error(exc, as_json)


@record_app.command("add")
def record_add(
    account: Annotated[
        str,
        typer.Option("--account", "-a", help="Account name or ID", autocompletion=complete_account),
    ],
    amount: Annotated[
        str,
        typer.Option("--amount", "-m", help="Positive amount in major units (e.g. 1000 or 250.50)"),
    ],
    category: Annotated[
        str,
        typer.Option("--category", "-c", help="Category name, ID, or envelopeId", autocompletion=complete_category),
    ] = "Groceries",
    record_type: Annotated[
        str,
        typer.Option("--type", "-t", help="Record type: expense or income"),
    ] = "expense",
    payment_type: Annotated[
        str,
        typer.Option(
            "--payment-type",
            "-p",
            help="Payment type: cash, debit_card, credit_card, bank_transfer, voucher, mobile_payment, web_payment",
        ),
    ] = "cash",
    state: Annotated[str, typer.Option("--state", help="Record state: reconciled, cleared, uncleared")] = "cleared",
    note: Annotated[str, typer.Option("--note", "-n", help="Transaction note")] = "",
    payee: Annotated[str, typer.Option("--payee", help="Payee / merchant name")] = "",
    date: Annotated[str | None, typer.Option("--date", "-d", help="Record date in ISO-8601 UTC or YYYY-MM-DD")] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Label name or ID (repeatable)", autocompletion=complete_label),
    ] = None,
    allow_bank_connected: Annotated[
        bool,
        typer.Option("--allow-bank-connected", help="Override guard on bank-connected accounts"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and preview payload without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Add a new expense or income record."""
    try:
        service = get_service(state_file)
        res = service.add_record(
            account_query=account,
            category_query=category,
            amount=amount,
            record_type=record_type,
            payment_type=payment_type,
            record_state=state,
            note=note,
            payee=payee,
            record_date=date,
            labels=label or [],
            allow_bank_connected=allow_bank_connected,
            dry_run=dry_run,
        )
        if as_json or dry_run:
            emit_json(res)
            return

        doc = res["document"]
        sign = "-" if doc["type"] == 1 else "+"
        color = "red" if doc["type"] == 1 else "green"
        amt = doc["amount"] / 100.0
        console.print(
            f"[bold green]✔ Created[/bold green] [bold]{res['id']}[/bold] ([dim]rev={res['rev']}[/dim]) | "
            f"[bold]{res['accountName']}[/bold] | [cyan]{res['categoryName']}[/cyan] | "
            f"[bold {color}]{sign}{amt:.2f} {res['currencyCode']}[/bold {color}] | "
            f"note={doc.get('note')!r}"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@record_app.command("update")
def record_update(
    record_id: Annotated[str, typer.Argument(help="Record ID (Record_<uuid> or <uuid>)")],
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="New account name or ID", autocompletion=complete_account),
    ] = None,
    amount: Annotated[str | None, typer.Option("--amount", "-m", help="New positive amount in major units")] = None,
    category: Annotated[
        str | None,
        typer.Option("--category", "-c", help="New category name, ID, or envelopeId", autocompletion=complete_category),
    ] = None,
    record_type: Annotated[str | None, typer.Option("--type", "-t", help="New record type: expense or income")] = None,
    payment_type: Annotated[str | None, typer.Option("--payment-type", "-p", help="New payment type")] = None,
    note: Annotated[str | None, typer.Option("--note", "-n", help="New transaction note")] = None,
    payee: Annotated[str | None, typer.Option("--payee", help="New payee / merchant name")] = None,
    date: Annotated[str | None, typer.Option("--date", "-d", help="New record date in ISO-8601")] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Replace labels with specified label(s)", autocompletion=complete_label),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and preview payload without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Update an existing record safely."""
    try:
        service = get_service(state_file)
        res = service.update_record(
            record_id,
            account_query=account,
            amount=amount,
            category_query=category,
            record_type=record_type,
            payment_type=payment_type,
            note=note,
            payee=payee,
            record_date=date,
            labels=label,
            dry_run=dry_run,
        )
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Updated[/bold green] [bold]{res['id']}[/bold] "
            f"([dim]new rev={res['rev']}, journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@record_app.command("delete")
def record_delete(
    record_id: Annotated[str, typer.Argument(help="Record ID (Record_<uuid> or <uuid>)")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview deletion without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Soft-delete a record (and its paired transfer leg if applicable)."""
    try:
        service = get_service(state_file)
        res = service.delete_record(record_id, dry_run=dry_run)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold yellow]✔ Deleted[/bold yellow] [bold]{', '.join(res['deletedIds'])}[/bold] "
            f"([dim]journal={res['journalEntryId']}[/dim])"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@record_app.command("undo")
def record_undo(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview rollback without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Revert the most recent mutation from the local rollback journal."""
    try:
        service = get_service(state_file)
        res = service.undo_last(dry_run=dry_run)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Reverted {res['originalOperation']}[/bold green] "
            f"([dim]journal={res['undoneEntryId']}[/dim]): [bold]{', '.join(res['revertedIds'])}[/bold]"
        )
    except WalletError as exc:
        handle_error(exc, as_json)

