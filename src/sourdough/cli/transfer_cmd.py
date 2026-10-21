"""Paired transfer creation, linking, and conversion commands (`dough transfer ...`)."""

from typing import Annotated

import typer

from sourdough.cli.common import (
    HELP_CONTEXT_SETTINGS,
    complete_account,
    console,
    emit_json,
    get_service,
    handle_error,
)
from sourdough.exceptions import WalletError

transfer_app = typer.Typer(
    help="Create, link, or convert paired transfers between accounts.",
    no_args_is_help=True,
    context_settings=HELP_CONTEXT_SETTINGS,
)


@transfer_app.command("add")
def transfer_add(
    from_account: Annotated[
        str,
        typer.Option("--from", help="Source account name or ID", autocompletion=complete_account),
    ],
    to_account: Annotated[
        str,
        typer.Option("--to", help="Destination account name or ID", autocompletion=complete_account),
    ],
    amount: Annotated[str, typer.Option("--amount", "-m", help="Positive amount leaving source account")],
    to_amount: Annotated[
        str | None,
        typer.Option("--to-amount", help="Optional positive amount entering destination account (cross-currency)"),
    ] = None,
    payment_type: Annotated[str, typer.Option("--payment-type", "-p", help="Payment type")] = "bank_transfer",
    note: Annotated[str, typer.Option("--note", "-n", help="Transfer note")] = "",
    date: Annotated[str | None, typer.Option("--date", "-d", help="Transfer date in ISO-8601 UTC or YYYY-MM-DD")] = None,
    allow_bank_connected: Annotated[
        bool,
        typer.Option("--allow-bank-connected", help="Override guard on bank-connected accounts"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and preview transfer legs without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Create an atomic paired transfer between two accounts."""
    try:
        service = get_service(state_file)
        res = service.add_transfer(
            from_account_query=from_account,
            to_account_query=to_account,
            amount=amount,
            to_amount=to_amount,
            payment_type=payment_type,
            note=note,
            record_date=date,
            allow_bank_connected=allow_bank_connected,
            dry_run=dry_run,
        )
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Created paired transfer[/bold green] ([dim]transferId={res['transferId']}[/dim]): "
            f"[bold]{res['fromAccount']}[/bold] ({res['outflowId']}) -> "
            f"[bold]{res['toAccount']}[/bold] ({res['inflowId']})"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@transfer_app.command("link")
def transfer_link(
    first_record_id: Annotated[str, typer.Argument(help="First Record ID (outflow or inflow leg)")],
    second_record_id: Annotated[str, typer.Argument(help="Second Record ID on the other account")],
    note: Annotated[str | None, typer.Option("--note", "-n", help="Optional unified transfer note")] = None,
    payee: Annotated[str | None, typer.Option("--payee", help="Optional unified payee")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview linked transfer legs without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Link two existing records on different accounts into an atomic paired transfer."""
    try:
        service = get_service(state_file)
        res = service.link_transfer(
            first_record_id,
            second_record_id,
            note=note,
            payee=payee,
            dry_run=dry_run,
        )
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Linked transfer pair[/bold green] ([dim]transferId={res['transferId']}, journal={res['journalEntryId']}[/dim]): "
            f"[bold]{res['fromAccount']}[/bold] ({res['outflowId']}) -> "
            f"[bold]{res['toAccount']}[/bold] ({res['inflowId']})"
        )
    except WalletError as exc:
        handle_error(exc, as_json)


@transfer_app.command("convert")
def transfer_convert(
    record_id: Annotated[str, typer.Argument(help="Existing single-leg Record ID to convert into a transfer")],
    to_account: Annotated[
        str,
        typer.Option("--to", help="Target account name or ID for the newly created mirror leg", autocompletion=complete_account),
    ],
    to_amount: Annotated[
        str | None,
        typer.Option("--to-amount", help="Optional positive amount on the target account (for cross-currency transfers)"),
    ] = None,
    note: Annotated[str | None, typer.Option("--note", "-n", help="Optional updated transfer note")] = None,
    payee: Annotated[str | None, typer.Option("--payee", help="Optional updated payee")] = None,
    allow_bank_connected: Annotated[
        bool,
        typer.Option("--allow-bank-connected", help="Override guard on bank-connected target account"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview converted transfer pair without writing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    state_file: Annotated[str | None, typer.Option("--state-file", help="Path to .wallet_state.json")] = None,
) -> None:
    """Convert an existing expense/income record into a paired transfer by creating its mirror leg on --to."""
    try:
        service = get_service(state_file)
        res = service.convert_to_transfer(
            record_id,
            target_account_query=to_account,
            target_amount=to_amount,
            note=note,
            payee=payee,
            allow_bank_connected=allow_bank_connected,
            dry_run=dry_run,
        )
        if as_json or dry_run:
            emit_json(res)
            return
        console.print(
            f"[bold green]✔ Converted to paired transfer[/bold green] ([dim]transferId={res['transferId']}, journal={res['journalEntryId']}[/dim]): "
            f"[bold]{res['sourceAccount']}[/bold] ({res['existingId']}) <-> "
            f"[bold]{res['targetAccount']}[/bold] ({res['mirrorId']})"
        )
    except WalletError as exc:
        handle_error(exc, as_json)
