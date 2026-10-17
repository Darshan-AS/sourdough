"""Append-only local mutation journal for audit logging and 1-command undo/rollback."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Self
import uuid

from sourdough.exceptions import WalletError


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """Single reversible mutation entry in the local journal."""

    entry_id: str
    timestamp: str
    operation: str  # CREATE, UPDATE, DELETE, TRANSFER
    before_docs: list[dict[str, Any]]
    after_docs: list[dict[str, Any]]
    undone: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "operation": self.operation,
            "before_docs": self.before_docs,
            "after_docs": self.after_docs,
            "undone": self.undone,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            entry_id=str(data["entry_id"]),
            timestamp=str(data["timestamp"]),
            operation=str(data["operation"]),
            before_docs=list(data.get("before_docs", [])),
            after_docs=list(data.get("after_docs", [])),
            undone=bool(data.get("undone", False)),
        )


class MutationJournal:
    """Manages an append-only JSONL journal with 0600 file permissions."""

    def __init__(self, journal_file: Path) -> None:
        self.journal_file = journal_file

    def record(
        self,
        operation: str,
        before_docs: list[dict[str, Any]],
        after_docs: list[dict[str, Any]],
    ) -> JournalEntry:
        """Append a mutation entry to the journal with 0600 permissions."""
        entry = JournalEntry(
            entry_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            operation=operation,
            before_docs=before_docs,
            after_docs=after_docs,
            undone=False,
        )
        self.journal_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(self.journal_file),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")
        return entry

    def list_entries(self) -> list[JournalEntry]:
        if not self.journal_file.exists():
            return []
        entries: list[JournalEntry] = []
        with open(self.journal_file, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    entries.append(JournalEntry.from_dict(json.loads(stripped)))
        return entries

    def get_last_undoable(self) -> JournalEntry | None:
        entries = self.list_entries()
        for entry in reversed(entries):
            if not entry.undone:
                return entry
        return None

    def mark_undone(self, entry_id: str) -> None:
        entries = self.list_entries()
        updated: list[JournalEntry] = []
        found = False
        for entry in entries:
            if entry.entry_id == entry_id:
                found = True
                updated.append(
                    JournalEntry(
                        entry_id=entry.entry_id,
                        timestamp=entry.timestamp,
                        operation=entry.operation,
                        before_docs=entry.before_docs,
                        after_docs=entry.after_docs,
                        undone=True,
                    )
                )
            else:
                updated.append(entry)
        if not found:
            raise WalletError(f"Journal entry {entry_id} not found")

        tmp_path = self.journal_file.with_suffix(".jsonl.tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for item in updated:
                    f.write(json.dumps(item.to_dict()) + "\n")
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        os.replace(str(tmp_path), str(self.journal_file))
