"""Record CRUD, batch update, filtering, and rollback commands (`dough record ...`)."""

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
    help="Create, list, inspect, update (single or batch), delete, or undo records.",
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
        typer.Option("--category", "-c", help="Filter by category name, Parent > Child, or ID", autocompletion=complete_category),
    ] = None,
    label: Annotated[
        str | None,
        typer.Option("--label", help="Filter by label name or ID", autocompletion=complete_label),
    ] = None,
    unlabeled: Annotated[
        bool,
        typer.Option("--unlabeled", help="Only show records with no labels"),
    ] = False,
    date_from: Annotated[
        str | None,
        typer.Option("--from", help="Start date inclusive (YYYY-MM-DD or ISO-8601)"),
    ] = None,
    date_to: Annotated[
        str | None,
        typer.Option("--to", help="End date inclusive (YYYY-MM-DD or ISO-8601)"),
    ] = None,
    min_amount: Annotated[
        str | None,
        typer.Option("--min-amount", help="Minimum absolute amount in major units"),
    ] = None,
    max_amount: Annotated[
        str | None,
        typer.Option("--max-amount", help="Maximum absolute amount in major units"),
    ] = None,
    amount: Annotated[
        str | None,
        typer.Option("--amount", "-m", help="Exact absolute amount in major units"),
    ] = None,
    record_type: Annotated[
        str | None,
        typer.Option("--type", "-t", help="Filter by type: expense, income, transfer, or unlinked-transfer"),
    ] = None,
    search: Annotated[str | None, typer.Option("--search", "-s", help="Substring search in note/payee/ID")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum records to return (0 for all)")] = 20,
    cached: Annotated[
        bool,
        typer.Option("--cached", help="Read from local record cache if available (sync via 'dough sync-cache --records')"),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """List and filter financial records."""
    try:
        service = get_service(state_file)
        rows = service.list_records(
            account_query=account,
            category_query=category,
            label_query=label,
            unlabeled=unlabeled,
            date_from=date_from,
            date_to=date_to,
            min_amount=min_amount,
            max_amount=max_amount,
            exact_amount=amount,
            record_type_filter=record_type,
            search=search,
            limit=limit,
            use_cache=cached,
        )
        if as_json:
            emit_json(rows)
            return

        table = Table(title=f"Records (showing {len(rows)})", box=box.ROUNDED)
        table.add_column("Date (UTC)", style="dim")
        table.add_column("Account", style="bold")
        table.add_column("Category", style="cyan")
        table.add_column("Amount", justify="right")
        table.add_column("Payee")
        table.add_column("Note")
        table.add_column("Labels", style="magenta")
        table.add_column("ID", style="dim")

        for r in rows:
            amt = r["signedAmount"]
            amt_str = (
                f"[bold red]{amt:+.2f} {r['currency']}[/bold red]"
                if amt < 0
                else f"[bold green]{amt:+.2f} {r['currency']}[/bold green]"
            )
            lbl_str = ", ".join(f"#{x}" for x in (r.get("labels") or [])) or "-"
            table.add_row(
                str(r["recordDate"])[:19].replace("T", " "),
                str(r["account"]),
                str(r["category"]),
                amt_str,
                r["payee"] or "-",
                (r["note"] or "").replace("\n", " ")[:45],
                lbl_str,
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
        typer.Option("--category", "-c", help="Category name, Parent > Child, ID, or envelopeId", autocompletion=complete_category),
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
    record_ids: Annotated[
        list[str],
        typer.Argument(help="One or more Record IDs (Record_<uuid> or <uuid>)"),
    ],
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="New account name or ID", autocompletion=complete_account),
    ] = None,
    amount: Annotated[str | None, typer.Option("--amount", "-m", help="New positive amount in major units")] = None,
    category: Annotated[
        str | None,
        typer.Option("--category", "-c", help="New category name, Parent > Child, ID, or envelopeId", autocompletion=complete_category),
    ] = None,
    record_type: Annotated[str | None, typer.Option("--type", "-t", help="New record type: expense or income")] = None,
    payment_type: Annotated[str | None, typer.Option("--payment-type", "-p", help="New payment type")] = None,
    note: Annotated[str | None, typer.Option("--note", "-n", help="Replace transaction note")] = None,
    prepend_note: Annotated[
        str | None,
        typer.Option("--prepend-note", help="Prepend prefix to existing note (if not already present)"),
    ] = None,
    append_note: Annotated[
        str | None,
        typer.Option("--append-note", help="Append suffix to existing note"),
    ] = None,
    payee: Annotated[str | None, typer.Option("--payee", help="New payee / merchant name")] = None,
    date: Annotated[str | None, typer.Option("--date", "-d", help="New record date in ISO-8601")] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Replace all labels with specified label(s)", autocompletion=complete_label),
    ] = None,
    add_label: Annotated[
        list[str] | None,
        typer.Option("--add-label", help="Append label(s) without overwriting existing labels", autocompletion=complete_label),
    ] = None,
    remove_label: Annotated[
        list[str] | None,
        typer.Option("--remove-label", help="Remove specified label(s) from record", autocompletion=complete_label),
    ] = None,
    single_leg: Annotated[
        bool,
        typer.Option("--single-leg", help="Do not auto-synchronize paired transfer leg when updating a transfer"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and preview payload without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Update one or more existing records safely (auto-syncing paired transfer legs unless --single-leg)."""
    try:
        service = get_service(state_file)
        if len(record_ids) == 1:
            res = service.update_record(
                record_ids[0],
                account_query=account,
                amount=amount,
                category_query=category,
                record_type=record_type,
                payment_type=payment_type,
                note=note,
                prepend_note=prepend_note,
                append_note=append_note,
                payee=payee,
                record_date=date,
                labels=label,
                add_labels=add_label,
                remove_labels=remove_label,
                sync_transfer_leg=not single_leg,
                dry_run=dry_run,
            )
            if as_json or dry_run:
                emit_json(res)
                return
            console.print(
                f"[bold green]✔ Updated[/bold green] [bold]{', '.join(res['updatedIds'])}[/bold] "
                f"([dim]rev={res['rev']}, journal={res['journalEntryId']}[/dim])"
            )
        else:
            batch_res = service.update_records(
                record_ids,
                account_query=account,
                amount=amount,
                category_query=category,
                record_type=record_type,
                payment_type=payment_type,
                note=note,
                prepend_note=prepend_note,
                append_note=append_note,
                payee=payee,
                record_date=date,
                labels=label,
                add_labels=add_label,
                remove_labels=remove_label,
                sync_transfer_leg=not single_leg,
                dry_run=dry_run,
            )
            if as_json or dry_run:
                emit_json(batch_res)
                return
            console.print(
                f"[bold green]✔ Batch updated {len(batch_res['updatedIds'])} document(s)[/bold green]: "
                f"[bold]{', '.join(batch_res['updatedIds'])}[/bold] "
                f"([dim]journal={batch_res['journalEntryId']}[/dim])"
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
    entry_id: Annotated[
        str | None,
        typer.Option("--id", help="Specific journal entry ID (or unambiguous prefix) to undo"),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview rollback without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Revert the most recent mutation (or a specific journal entry via --id) from the rollback journal."""
    try:
        service = get_service(state_file)
        res = service.undo_last(dry_run=dry_run, entry_id=entry_id)
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Reverted {res['originalOperation']}[/bold green] "
            f"([dim]journal={res['undoneEntryId']}[/dim]): [bold]{', '.join(res['revertedIds'])}[/bold]"
        )
    except WalletError as exc:
        handle_error(exc, as_json)
