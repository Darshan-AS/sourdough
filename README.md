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

# Refresh local metadata cache (and optionally cache all Record_ docs locally via --records)
./dough sync-cache
./dough sync-cache --records

# List & manage accounts, categories, and labels
./dough accounts
./dough categories --search "food"
./dough categories add "Meals & Fast Food" --parent "1001"
./dough categories rename "6009" "Holiday, trips, hotels"
./dough categories move "Auto" --parent "4001"
./dough categories delete "EmptySubcat" --move-to "1001 > Meals & Fast Food"

./dough labels
./dough labels add "Family" --color "#26c6da"
./dough labels rename "Family" "Family Support"
./dough labels delete "UnusedLabel"

# Preview creating a record (--dry-run)
./dough record add --account "Cash" --amount 1000 --category "Groceries" --note "Test" --dry-run

# Create an expense or income record (supports Parent > Child category resolution)
./dough record add --account "Cash" --amount 1000 --category "1001 > Meals & Fast Food" --note "Dinner"

# Rich filtering on records (date range, labels, amount, type, and instant local --cached mode)
./dough record list --account "HDFC" --from 2023-12-20 --to 2024-01-15 --label "Family" --cached
./dough record list --type unlinked-transfer --limit 50
./dough record list --amount 395 --json

# Single or batch update with incremental label & note flags (auto-syncs paired transfer legs)
./dough record update Record_<uuid1> Record_<uuid2> \
  --category "6009" \
  --add-label "Family" \
  --add-label "Shared with BrotherMine" \
  --prepend-note "[Thailand Jan 2024] "

# Delete a record (and its paired transfer leg if applicable)
./dough record delete Record_<uuid>

# Inspect mutation journal and undo the last mutation (or a specific batch via --id)
./dough journal --limit 10
./dough record undo
./dough record undo --id <journal_entry_prefix>

# Create, link, or convert paired transfers between accounts
./dough transfer add --from "HDFC" --to "Cash" --amount 2000 --note "ATM Withdrawal"
./dough transfer link Record_<outflow_uuid> Record_<inflow_uuid> --note "Credit Card Bill"
./dough transfer convert Record_<single_leg_uuid> --to "Investments" --payee "IndMoney"
```

## Running Tests

```bash
uv run python -m unittest discover -s tests -v
```
