# `sourdough` (`dough` CLI)

A strictly validated Python SDK (`sourdough`) and CLI (`dough`) for programmatic and agentic CRUD against your **Wallet by BudgetBakers** CouchDB database.

## Key Engineering & Safety Guarantees

1. **Client SQLite & PouchDB Invariant Enforcement (`sourdough.domain.models`)**:
   - Validates every field against the exact `NOT NULL` and type constraints in Wallet Web's `sql.js` indexer (`module 15168`) and `upsertRecordQuery` (`module 88437`).
   - Enforces positive integer minor units (`paise`/`cents`) for `amount` and `refAmount`, ISO-8601 UTC timestamps, prefixed UUID formats (`Record_`, `-Account_`, `-Category_`, `-Currency_`, `-HashTag_`), and account currency pinning.
2. **Human-Friendly Name Resolution & Local Metadata Cache (`sourdough.services.resolver`)**:
   - Automatically maps account names (`"Cash"`, `"HDFC"`), category names or `envelopeId`s (`"Groceries"`, `1000`), currency codes (`"INR"`), and label names to internal CouchDB document IDs.
3. **Atomic Paired Transfers (`sourdough.domain.builders.build_transfer_pair`)**:
   - Creates both outflow (`EXPENSE`) and inflow (`INCOME`) transfer legs with a shared `transferId` in a single `_bulk_docs` request, and deletes both legs together when removing a transfer.
4. **Local Mutation Journal & One-Command Undo (`sourdough.infra.journal`)**:
   - Every `CREATE`, `UPDATE`, `DELETE`, and `TRANSFER` operation records a before/after snapshot (`0600` permissions) so `dough record undo` can revert the last change immediately.
5. **Auth Session Refresh (`dough auth refresh`)**:
   - Uses `sessionToken` to call `user.getUser` on `web.budgetbakers.com` and refresh CouchDB `replication` credentials.

---

## Quick Start

```bash
# Display CLI usage help
./dough --help
./dough help record add

# Check database status and paths
./dough auth status

# Refresh local metadata cache (Accounts, Categories, Currencies, Labels)
./dough sync-cache

# List accounts and categories
./dough accounts
./dough categories --search "food"

# Preview creating a record (--dry-run)
./dough record add --account "Cash" --amount 1000 --category "Groceries" --note "Test" --dry-run

# Create an expense or income record
./dough record add --account "Cash" --amount 1000 --category "Groceries" --note "Gemini Argon test"

# List recent records (or pass --json for agent/script usage)
./dough record list --account "Cash" --limit 10
./dough record list --limit 5 --json

# Update, delete, or undo the most recent mutation
./dough record update Record_<uuid> --amount 1200 --note "Updated note"
./dough record delete Record_<uuid>
./dough record undo

# Create a paired transfer between two accounts
./dough transfer add --from "HDFC" --to "Cash" --amount 2000 --note "ATM Withdrawal"
```

## Running Tests

```bash
uv run python -m unittest discover -s tests -v
```
