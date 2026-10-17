"""High-level CRUD, Transfer, Auth, and Undo service for Wallet by BudgetBakers."""

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from sourdough.config import StateManager, WalletPaths, resolve_paths
from sourdough.domain.builders import (
    NumericAmount,
    build_record,
    build_transfer_pair,
    compute_ref_amount,
    decimal_to_minor_units,
    normalize_timestamp_input,
    utc_now_iso,
)
from sourdough.domain.enums import PaymentType, RecordState, RecordType
from sourdough.domain.models import (
    AccountDocument,
    CategoryDocument,
    CurrencyDocument,
    LabelDocument,
    RecordDocument,
)
from sourdough.exceptions import (
    BankConnectedAccountError,
    EntityNotFoundError,
    WalletError,
)
from sourdough.infra.cache import MetadataCache, MetadataSnapshot
from sourdough.infra.couchdb import (
    CouchDBClient,
    HttpCallable,
    refresh_replication_from_session,
)
from sourdough.infra.journal import JournalEntry, MutationJournal
from sourdough.services.resolver import EntityResolver


def normalize_record_id(record_id: str) -> str:
    rid = record_id.strip()
    if not rid.startswith("Record_"):
        return f"Record_{rid}"
    return rid


class WalletService:
    """Facade coordinating CouchDB CRUD, local cache, entity resolution, and rollback journaling."""

    def __init__(
        self,
        paths: WalletPaths | None = None,
        transport: HttpCallable | None = None,
    ) -> None:
        self.paths = paths or resolve_paths()
        self.state_manager = StateManager(self.paths.state_file)
        self._transport = transport
        self.replication = self.state_manager.load_replication()
        self.client = CouchDBClient(self.replication, transport=transport)
        self.cache = MetadataCache(self.paths.cache_file, self.client)
        self.resolver = EntityResolver(self.cache)
        self.journal = MutationJournal(self.paths.journal_file)

    def auth_status(self) -> dict[str, Any]:
        """Verify CouchDB connection and return database status + user metadata."""
        raw_state = self.state_manager.load_raw()
        db_info = self.client.db_info()
        return {
            "email": raw_state.get("email"),
            "userId": self.replication.owner_id,
            "dbName": self.replication.db_name,
            "couchUrl": self.replication.url,
            "docCount": db_info.get("doc_count"),
            "deletedDocCount": db_info.get("doc_del_count"),
            "updateSeq": db_info.get("update_seq"),
            "lastRecordId": raw_state.get("lastRecordId"),
            "stateFile": str(self.paths.state_file),
            "cacheFile": str(self.paths.cache_file),
            "journalFile": str(self.paths.journal_file),
        }

    def auth_refresh(self) -> dict[str, Any]:
        """Refresh CouchDB replication credentials from web.budgetbakers.com using sessionToken."""
        raw_state = self.state_manager.load_raw()
        session_token = str(raw_state.get("sessionToken", ""))
        new_rep, user_data = refresh_replication_from_session(
            session_token, transport=self._transport
        )
        self.state_manager.update_replication(new_rep)
        self.replication = new_rep
        self.client = CouchDBClient(new_rep, transport=self._transport)
        self.cache.client = self.client
        db_info = self.client.db_info()
        return {
            "refreshed": True,
            "email": user_data.get("email"),
            "fullName": user_data.get("fullName"),
            "dbName": new_rep.db_name,
            "couchUrl": new_rep.url,
            "docCount": db_info.get("doc_count"),
        }

    def sync_cache(self) -> MetadataSnapshot:
        """Force refresh of local Accounts, Categories, Currencies, and Labels cache."""
        return self.cache.refresh()

    def list_accounts(self, include_archived: bool = False) -> list[AccountDocument]:
        snap = self.cache.get_snapshot()
        accounts = snap.accounts
        if not include_archived:
            accounts = [a for a in accounts if not a.archived]
        return sorted(accounts, key=lambda a: a.name.lower())

    def list_categories(self, search: str | None = None) -> list[CategoryDocument]:
        snap = self.cache.get_snapshot()
        cats = snap.categories
        if search:
            q = search.strip().lower()
            cats = [
                c
                for c in cats
                if q in c.display_name.lower()
                or q in c.name.lower()
                or q == str(c.envelope_id)
            ]
        return sorted(cats, key=lambda c: (c.envelope_id, c.display_name.lower()))

    def list_currencies(self) -> list[CurrencyDocument]:
        snap = self.cache.get_snapshot()
        return sorted(snap.currencies, key=lambda c: (not c.referential, c.code))

    def list_labels(self, include_archived: bool = False) -> list[LabelDocument]:
        snap = self.cache.get_snapshot()
        labels = snap.labels
        if not include_archived:
            labels = [l for l in labels if not l.archived]
        return sorted(labels, key=lambda l: l.name.lower())

    def add_record(
        self,
        *,
        account_query: str,
        category_query: str,
        amount: NumericAmount,
        record_type: RecordType | str = RecordType.EXPENSE,
        payment_type: PaymentType | str = PaymentType.CASH,
        record_state: RecordState | str = RecordState.CLEARED,
        note: str = "",
        payee: str = "",
        record_date: str | None = None,
        labels: Iterable[str] | None = None,
        allow_bank_connected: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Validate, build, and persist a new Record_<uuid> document in CouchDB."""
        account = self.resolver.resolve_account(account_query)
        if account.bank_connected and not allow_bank_connected:
            raise BankConnectedAccountError(
                f"Account {account.name!r} is connected to a bank synchronization. "
                "Pass allow_bank_connected=True (--allow-bank-connected) to override."
            )
        category = self.resolver.resolve_category(category_query)
        currency = self.resolver.resolve_currency(account.currency_id)
        label_ids = self.resolver.resolve_labels(labels or ())

        r_type = (
            RecordType.from_str(record_type)
            if isinstance(record_type, str)
            else record_type
        )
        p_type = (
            PaymentType.from_str(payment_type)
            if isinstance(payment_type, str)
            else payment_type
        )
        r_state = (
            RecordState.from_str(record_state)
            if isinstance(record_state, str)
            else record_state
        )

        record_doc = build_record(
            owner_id=self.replication.owner_id,
            account=account,
            currency=currency,
            category=category,
            amount_major=amount,
            record_type=r_type,
            payment_type=p_type,
            record_state=r_state,
            note=note,
            payee=payee,
            record_date=record_date,
            labels=label_ids,
        )
        payload = record_doc.to_couch_dict()

        if dry_run:
            return {
                "dryRun": True,
                "operation": "CREATE",
                "accountName": account.name,
                "categoryName": category.display_name,
                "currencyCode": currency.code,
                "document": payload,
            }

        put_res = self.client.put_doc(payload)
        saved_doc = dict(payload)
        saved_doc["_rev"] = put_res.get("rev")

        journal_entry = self.journal.record(
            operation="CREATE",
            before_docs=[],
            after_docs=[saved_doc],
        )
        self.state_manager.update_last_record_id(record_doc.doc_id)

        return {
            "dryRun": False,
            "operation": "CREATE",
            "id": record_doc.doc_id,
            "rev": put_res.get("rev"),
            "journalEntryId": journal_entry.entry_id,
            "accountName": account.name,
            "categoryName": category.display_name,
            "currencyCode": currency.code,
            "document": saved_doc,
        }

    def add_transfer(
        self,
        *,
        from_account_query: str,
        to_account_query: str,
        amount: NumericAmount,
        to_amount: NumericAmount | None = None,
        payment_type: PaymentType | str = PaymentType.BANK_TRANSFER,
        record_state: RecordState | str = RecordState.CLEARED,
        note: str = "",
        record_date: str | None = None,
        allow_bank_connected: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create an atomic paired transfer across two accounts via _bulk_docs."""
        from_acc = self.resolver.resolve_account(from_account_query)
        to_acc = self.resolver.resolve_account(to_account_query)
        for acc in (from_acc, to_acc):
            if acc.bank_connected and not allow_bank_connected:
                raise BankConnectedAccountError(
                    f"Account {acc.name!r} is bank-connected; refusing to write transfer leg."
                )

        from_curr = self.resolver.resolve_currency(from_acc.currency_id)
        to_curr = self.resolver.resolve_currency(to_acc.currency_id)
        transfer_cat = self.resolver.resolve_transfer_category()

        p_type = (
            PaymentType.from_str(payment_type)
            if isinstance(payment_type, str)
            else payment_type
        )
        r_state = (
            RecordState.from_str(record_state)
            if isinstance(record_state, str)
            else record_state
        )

        outflow, inflow = build_transfer_pair(
            owner_id=self.replication.owner_id,
            from_account=from_acc,
            from_currency=from_curr,
            to_account=to_acc,
            to_currency=to_curr,
            transfer_category=transfer_cat,
            from_amount_major=amount,
            to_amount_major=to_amount,
            payment_type=p_type,
            record_state=r_state,
            note=note,
            record_date=record_date,
        )
        docs_payload = [outflow.to_couch_dict(), inflow.to_couch_dict()]

        if dry_run:
            return {
                "dryRun": True,
                "operation": "TRANSFER",
                "fromAccount": from_acc.name,
                "toAccount": to_acc.name,
                "transferId": outflow.transfer_id,
                "documents": docs_payload,
            }

        bulk_res = self.client.bulk_docs(docs_payload)
        saved_docs: list[dict[str, Any]] = []
        for doc_dict, res_item in zip(docs_payload, bulk_res):
            item_copy = dict(doc_dict)
            item_copy["_rev"] = res_item.get("rev")
            saved_docs.append(item_copy)

        journal_entry = self.journal.record(
            operation="TRANSFER",
            before_docs=[],
            after_docs=saved_docs,
        )
        self.state_manager.update_last_record_id(outflow.doc_id)

        return {
            "dryRun": False,
            "operation": "TRANSFER",
            "transferId": outflow.transfer_id,
            "outflowId": outflow.doc_id,
            "inflowId": inflow.doc_id,
            "journalEntryId": journal_entry.entry_id,
            "fromAccount": from_acc.name,
            "toAccount": to_acc.name,
            "documents": saved_docs,
        }

    def get_record(self, record_id: str) -> dict[str, Any]:
        """Fetch a single Record_<uuid> document and enrich it with resolved names."""
        norm_id = normalize_record_id(record_id)
        raw = self.client.get_doc(norm_id)
        rec = RecordDocument.from_couch_dict(raw)
        snap = self.cache.get_snapshot()
        acc = snap.accounts_by_id.get(rec.account_id)
        cat = snap.categories_by_id.get(rec.category_id)
        cur = snap.currencies_by_id.get(rec.currency_id)
        return {
            "id": rec.doc_id,
            "rev": rec.rev,
            "account": acc.name if acc else rec.account_id,
            "category": cat.display_name if cat else rec.category_id,
            "currency": cur.code if cur else rec.currency_id,
            "signedAmount": rec.signed_decimal_amount,
            "type": "EXPENSE" if rec.record_type == int(RecordType.EXPENSE) else "INCOME",
            "recordDate": rec.record_date,
            "note": rec.note,
            "payee": rec.payee,
            "document": raw,
        }

    def list_records(
        self,
        *,
        account_query: str | None = None,
        category_query: str | None = None,
        search: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch, filter, and sort records newest-first with resolved account/category names."""
        snap = self.cache.get_snapshot()
        account_id_filter: str | None = None
        if account_query:
            account_id_filter = self.resolver.resolve_account(account_query).doc_id
        category_id_filter: str | None = None
        if category_query:
            category_id_filter = self.resolver.resolve_category(category_query).doc_id

        raw_records = self.client.all_docs_by_prefix("Record_")
        filtered: list[dict[str, Any]] = []
        q_search = search.strip().lower() if search else None

        for raw in raw_records:
            if raw.get("reservedModelType") != "Record":
                continue
            if account_id_filter and raw.get("accountId") != account_id_filter:
                continue
            if category_id_filter and raw.get("categoryId") != category_id_filter:
                continue
            if q_search:
                haystack = f"{raw.get('note', '')} {raw.get('payee', '')} {raw.get('_id', '')}".lower()
                if q_search not in haystack:
                    continue
            filtered.append(raw)

        filtered.sort(
            key=lambda d: (str(d.get("recordDate", "")), str(d.get("_id", ""))),
            reverse=True,
        )

        results: list[dict[str, Any]] = []
        for raw in filtered[: max(1, limit)]:
            rec_type = int(raw.get("type", 1))
            amt = float(raw.get("amount", 0)) / 100.0
            signed = -amt if rec_type == int(RecordType.EXPENSE) else amt
            acc = snap.accounts_by_id.get(str(raw.get("accountId", "")))
            cat = snap.categories_by_id.get(str(raw.get("categoryId", "")))
            cur = snap.currencies_by_id.get(str(raw.get("currencyId", "")))
            results.append(
                {
                    "id": raw.get("_id"),
                    "rev": raw.get("_rev"),
                    "recordDate": raw.get("recordDate"),
                    "account": acc.name if acc else raw.get("accountId"),
                    "category": cat.display_name if cat else raw.get("categoryId"),
                    "currency": cur.code if cur else raw.get("currencyId"),
                    "signedAmount": signed,
                    "type": "EXPENSE" if rec_type == int(RecordType.EXPENSE) else "INCOME",
                    "payee": raw.get("payee", ""),
                    "note": raw.get("note", ""),
                }
            )
        return results

    def update_record(
        self,
        record_id: str,
        *,
        amount: NumericAmount | None = None,
        note: str | None = None,
        payee: str | None = None,
        category_query: str | None = None,
        account_query: str | None = None,
        record_type: RecordType | str | None = None,
        payment_type: PaymentType | str | None = None,
        record_date: str | None = None,
        labels: Iterable[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Safely update an existing Record_<uuid> document while preserving unknown fields."""
        norm_id = normalize_record_id(record_id)
        before_raw = self.client.get_doc(norm_id)
        existing = RecordDocument.from_couch_dict(before_raw)

        new_account_id = existing.account_id
        new_currency_id = existing.currency_id
        if account_query is not None:
            acc = self.resolver.resolve_account(account_query)
            new_account_id = acc.doc_id
            new_currency_id = acc.currency_id

        currency = self.resolver.resolve_currency(new_currency_id)

        new_amount = existing.amount
        if amount is not None:
            new_amount = decimal_to_minor_units(amount)
        new_ref_amount = compute_ref_amount(new_amount, currency)

        new_category_id = existing.category_id
        if category_query is not None:
            new_category_id = self.resolver.resolve_category(category_query).doc_id

        new_type = existing.record_type
        if record_type is not None:
            new_type = int(
                RecordType.from_str(record_type)
                if isinstance(record_type, str)
                else record_type
            )

        new_payment_type = existing.payment_type
        if payment_type is not None:
            new_payment_type = int(
                PaymentType.from_str(payment_type)
                if isinstance(payment_type, str)
                else payment_type
            )

        new_date = (
            normalize_timestamp_input(record_date)
            if record_date is not None
            else existing.record_date
        )
        new_labels = (
            self.resolver.resolve_labels(labels)
            if labels is not None
            else existing.labels
        )

        updated_model = replace(
            existing,
            account_id=new_account_id,
            currency_id=new_currency_id,
            category_id=new_category_id,
            amount=new_amount,
            ref_amount=new_ref_amount,
            record_type=new_type,
            payment_type=new_payment_type,
            record_date=new_date,
            note=note if note is not None else existing.note,
            payee=payee if payee is not None else existing.payee,
            labels=new_labels,
            reserved_updated_at=utc_now_iso(),
        )
        after_payload = updated_model.to_couch_dict()

        if dry_run:
            return {
                "dryRun": True,
                "operation": "UPDATE",
                "id": norm_id,
                "before": before_raw,
                "after": after_payload,
            }

        put_res = self.client.put_doc(after_payload, retry_on_conflict=True)
        saved_after = dict(after_payload)
        saved_after["_rev"] = put_res.get("rev")

        journal_entry = self.journal.record(
            operation="UPDATE",
            before_docs=[before_raw],
            after_docs=[saved_after],
        )
        return {
            "dryRun": False,
            "operation": "UPDATE",
            "id": norm_id,
            "rev": put_res.get("rev"),
            "journalEntryId": journal_entry.entry_id,
            "document": saved_after,
        }

    def delete_record(self, record_id: str, dry_run: bool = False) -> dict[str, Any]:
        """Soft-delete a Record_<uuid> document (and its paired transfer leg if applicable)."""
        norm_id = normalize_record_id(record_id)
        target_doc = self.client.get_doc(norm_id)
        docs_to_delete = [target_doc]

        transfer_id = target_doc.get("transferId")
        if transfer_id:
            all_records = self.client.all_docs_by_prefix("Record_")
            for candidate in all_records:
                if (
                    candidate.get("transferId") == transfer_id
                    and candidate.get("_id") != norm_id
                ):
                    docs_to_delete.append(candidate)

        if dry_run:
            return {
                "dryRun": True,
                "operation": "DELETE",
                "deletedIds": [d.get("_id") for d in docs_to_delete],
                "before": docs_to_delete,
            }

        after_tombstones: list[dict[str, Any]] = []
        for doc in docs_to_delete:
            del_res = self.client.delete_doc(str(doc["_id"]), rev=str(doc["_rev"]))
            after_tombstones.append(
                {
                    "_id": doc["_id"],
                    "_rev": del_res.get("rev"),
                    "_deleted": True,
                }
            )

        journal_entry = self.journal.record(
            operation="DELETE",
            before_docs=docs_to_delete,
            after_docs=after_tombstones,
        )
        return {
            "dryRun": False,
            "operation": "DELETE",
            "deletedIds": [d.get("_id") for d in docs_to_delete],
            "journalEntryId": journal_entry.entry_id,
            "results": after_tombstones,
        }

    def undo_last(self, dry_run: bool = False) -> dict[str, Any]:
        """Revert the most recent non-undone mutation in the local journal."""
        entry: JournalEntry | None = self.journal.get_last_undoable()
        if entry is None:
            raise WalletError("No undoable operations found in the mutation journal")

        if dry_run:
            return {
                "dryRun": True,
                "undoEntryId": entry.entry_id,
                "originalOperation": entry.operation,
                "originalTimestamp": entry.timestamp,
                "beforeDocs": entry.before_docs,
                "afterDocs": entry.after_docs,
            }

        reverted_ids: list[str] = []
        if entry.operation in ("CREATE", "TRANSFER"):
            for created in entry.after_docs:
                doc_id = str(created["_id"])
                self.client.delete_doc(doc_id)
                reverted_ids.append(doc_id)
        elif entry.operation in ("UPDATE", "DELETE"):
            for prev_doc in entry.before_docs:
                doc_id = str(prev_doc["_id"])
                restored = dict(prev_doc)
                restored.pop("_deleted", None)
                try:
                    current = self.client.get_doc(doc_id)
                    restored["_rev"] = current["_rev"]
                except EntityNotFoundError:
                    restored.pop("_rev", None)
                self.client.put_doc(restored, retry_on_conflict=True)
                reverted_ids.append(doc_id)
        else:
            raise WalletError(f"Unsupported journal operation for undo: {entry.operation}")

        self.journal.mark_undone(entry.entry_id)
        return {
            "dryRun": False,
            "undoneEntryId": entry.entry_id,
            "originalOperation": entry.operation,
            "revertedIds": reverted_ids,
        }
