"""Paired transfer commands (`dough transfer ...`)."""

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
    help="Create paired transfers between accounts.",
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

