"""Shared CLI consoles, error handlers, and fast (<5ms) autocompletion callbacks."""

import json
import sys
from typing import Any, NoReturn

from rich.console import Console
import typer

from sourdough.config import resolve_paths
from sourdough.domain.enums import ENVELOPE_NAMES
from sourdough.exceptions import WalletError
from sourdough.services.records import WalletService

HELP_CONTEXT_SETTINGS: dict[str, Any] = {"help_option_names": ["-h", "--help"]}

console = Console()
err_console = Console(stderr=True)


def _load_cached_json() -> dict[str, Any]:
    try:
        paths = resolve_paths()
        if paths.cache_file.exists():
            return json.loads(paths.cache_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def complete_account(incomplete: str) -> list[str]:
    """Autocomplete active account names from local metadata cache."""
    data = _load_cached_json()
    inc = incomplete.lower()
    names = [
        str(a.get("name", ""))
        for a in data.get("accounts", [])
        if isinstance(a, dict) and not a.get("archived") and a.get("name")
    ]
    return sorted(n for n in names if n.lower().startswith(inc))


def complete_category(incomplete: str) -> list[str]:
    """Autocomplete category names from local metadata cache."""
    data = _load_cached_json()
    inc = incomplete.lower()
    names: set[str] = set()
    for c in data.get("categories", []):
        if isinstance(c, dict):
            env_id = int(c.get("envelopeId", 0) or 0)
            name = str(c.get("name", "") or ENVELOPE_NAMES.get(env_id, "")).strip()
            if name:
                names.add(name)
    return sorted(n for n in names if n.lower().startswith(inc))


def complete_label(incomplete: str) -> list[str]:
    """Autocomplete active label names from local metadata cache."""
    data = _load_cached_json()
    inc = incomplete.lower()
    names = [
        str(l.get("name", ""))
        for l in data.get("labels", [])
        if isinstance(l, dict) and not l.get("archived") and l.get("name")
    ]
    return sorted(n for n in names if n.lower().startswith(inc))


def emit_json(data: Any) -> None:
    """Write formatted JSON to stdout."""
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


def get_service(state_file: str | None) -> WalletService:
    """Instantiate WalletService for the resolved paths."""
    paths = resolve_paths(explicit_state_file=state_file)
    return WalletService(paths=paths)


def handle_error(exc: WalletError, as_json: bool) -> NoReturn:
    """Format a domain/infrastructure error for human or JSON output and exit with code 1."""
    if as_json:
        sys.stderr.write(
            json.dumps({"error": exc.__class__.__name__, "message": str(exc)}, indent=2) + "\n"
        )
    else:
        err_console.print(f"[bold red]Error [{exc.__class__.__name__}]:[/bold red] {exc}")
    raise typer.Exit(code=1)

