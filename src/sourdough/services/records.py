"""High-level CRUD, Transfer, Category/Label Management, Auth, and Undo service for Wallet by BudgetBakers."""

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
import uuid

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
from sourdough.domain.enums import ENVELOPE_NAMES, PaymentType, RecordState, RecordType
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
    SchemaValidationError,
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

    def sync_cache(self, include_records: bool = False) -> MetadataSnapshot:
        """Force refresh of local Accounts, Categories, Currencies, and Labels (and optionally Records) cache."""
        return self.cache.refresh(include_records=include_records)

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
                or q in ENVELOPE_NAMES.get(c.envelope_id, "").lower()
            ]
        return sorted(cats, key=lambda c: (c.envelope_id, c.custom_category, c.display_name.lower()))

    def list_currencies(self) -> list[CurrencyDocument]:
        snap = self.cache.get_snapshot()
        return sorted(snap.currencies, key=lambda c: (not c.referential, c.code))

    def list_labels(self, include_archived: bool = False) -> list[LabelDocument]:
        snap = self.cache.get_snapshot()
        labels = snap.labels
        if not include_archived:
            labels = [l for l in labels if not l.archived]
        return sorted(labels, key=lambda l: l.name.lower())

    def list_debts(
        self,
        *,
        person_query: str | None = None,
        only_open: bool = False,
    ) -> list[dict[str, Any]]:
        """List -Debt_<uuid> documents in CouchDB, optionally filtered by person name/note or open state."""
        raw_debts = self.client.all_docs_by_prefix("-Debt_")
        q = person_query.strip().lower() if person_query else None
        results: list[dict[str, Any]] = []
        for raw in raw_debts:
            if raw.get("_deleted") or raw.get("reservedModelType") != "Debt":
                continue
            paid_back = bool(raw.get("paidBack", False)) or bool(raw.get("forgiven", False))
            if only_open and paid_back:
                continue
            name = str(raw.get("name", ""))
            note = str(raw.get("note", ""))
            if q and q not in name.lower() and q not in note.lower():
                continue
            results.append(
                {
                    "id": str(raw.get("_id", "")),
                    "rev": str(raw.get("_rev", "")),
                    "name": name,
                    "note": note,
                    "amount": int(raw.get("amount", 0)),
                    "remainingAmount": int(raw.get("remainingAmount", 0)),
                    "paidBack": paid_back,
                    "date": str(raw.get("date", "")),
                    "document": raw,
                }
            )
        results.sort(key=lambda d: (d["date"], d["id"]), reverse=True)
        return results

    def find_open_debt(self, person_name: str) -> dict[str, Any] | None:
        """Return the raw CouchDB document for the most recent open -Debt_<uuid> matching person_name."""
        clean = person_name.strip().lower()
        if not clean:
            return None
        open_debts = self.list_debts(person_query=person_name, only_open=True)
        for item in open_debts:
            if item["name"].strip().lower() == clean:
                return dict(item["document"])
        if open_debts:
            return dict(open_debts[0]["document"])
        return None

    def find_matching_transfer(
        self,
        *,
        from_account_id: str,
        to_account_id: str,
        amount_minor: int,
        date_iso: str,
        tolerance_days: int = 2,
        raw_records: Sequence[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Find an existing active paired transfer between from_account_id and to_account_id within tolerance_days."""
        try:
            target_dt = date.fromisoformat(date_iso[:10])
        except ValueError:
            return None

        records = (
            list(raw_records)
            if raw_records is not None
            else self.client.all_docs_by_prefix("Record_")
        )
        by_transfer_id: dict[str, list[dict[str, Any]]] = {}
        outflow_candidates: list[tuple[int, dict[str, Any]]] = []

        for raw in records:
            if raw.get("_deleted") or raw.get("reservedModelType") != "Record":
                continue
            if not raw.get("transfer"):
                continue
            tid = str(raw.get("transferId") or "")
            if tid:
                by_transfer_id.setdefault(tid, []).append(raw)

            if (
                raw.get("accountId") == from_account_id
                and raw.get("transferAccountId") == to_account_id
                and int(raw.get("type", 1)) == int(RecordType.EXPENSE)
                and int(raw.get("amount", 0)) == amount_minor
            ):
                rec_dt_str = str(raw.get("recordDate", ""))[:10]
                try:
                    rec_dt = date.fromisoformat(rec_dt_str)
                except ValueError:
                    continue
                day_diff = abs((rec_dt - target_dt).days)
                if day_diff <= tolerance_days:
                    outflow_candidates.append((day_diff, raw))

        outflow_candidates.sort(key=lambda pair: (pair[0], str(pair[1].get("recordDate", ""))))
        for _diff, out_raw in outflow_candidates:
            tid = str(out_raw.get("transferId") or "")
            if not tid:
                continue
            for sibling in by_transfer_id.get(tid, ()):
                if (
                    sibling.get("_id") != out_raw.get("_id")
                    and sibling.get("accountId") == to_account_id
                    and int(sibling.get("type", 0)) == int(RecordType.INCOME)
                ):
                    return (dict(out_raw), dict(sibling))
        return None

    # -------------------------------------------------------------------------
    # Currency & Label Management (CRUD)
    # -------------------------------------------------------------------------

    def add_currency(
        self,
        code: str,
        ratio_to_referential: float,
        *,
        position: int = 3000,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a new -Currency_<uuid> document in CouchDB."""
        clean_code = code.strip().upper()
        if len(clean_code) != 3 or not clean_code.isalpha():
            raise SchemaValidationError(f"Currency code must be a 3-letter ISO code, got {code!r}")
        if ratio_to_referential <= 0:
            raise SchemaValidationError(f"ratio_to_referential must be > 0, got {ratio_to_referential!r}")

        snap = self.cache.get_snapshot(force_refresh=not dry_run)
        for existing in snap.currencies:
            if existing.code.upper() == clean_code:
                return {
                    "dryRun": dry_run,
                    "operation": "CURRENCY_EXISTS",
                    "id": existing.doc_id,
                    "code": existing.code,
                    "ratioToReferential": existing.ratio_to_referential,
                }

        now_iso = utc_now_iso()
        doc_id = f"-Currency_{uuid.uuid4()}"
        payload: dict[str, Any] = {
            "_id": doc_id,
            "code": clean_code,
            "referential": False,
            "ratioToReferential": float(ratio_to_referential),
            "position": int(position),
            "reservedModelType": "Currency",
            "reservedOwnerId": self.replication.owner_id,
            "reservedAuthorId": self.replication.owner_id,
            "reservedCreatedAt": now_iso,
            "reservedUpdatedAt": now_iso,
            "reservedSource": "android",
        }
        CurrencyDocument.from_couch_dict(payload)

        if dry_run:
            return {
                "dryRun": True,
                "operation": "CURRENCY_CREATE",
                "id": doc_id,
                "code": clean_code,
                "ratioToReferential": float(ratio_to_referential),
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
        self.cache.refresh()
        return {
            "dryRun": False,
            "operation": "CURRENCY_CREATE",
            "id": doc_id,
            "rev": put_res.get("rev"),
            "code": clean_code,
            "ratioToReferential": float(ratio_to_referential),
            "journalEntryId": journal_entry.entry_id,
            "document": saved_doc,
        }

    def add_label(
        self,
        name: str,
        *,
        color: str = "#26c6da",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a new -HashTag_<uuid> label document in CouchDB."""
        clean_name = name.strip().lstrip("#").strip()
        if not clean_name:
            raise SchemaValidationError("Label name must be a non-empty string")

        snap = self.cache.get_snapshot(force_refresh=True)
        for existing in snap.labels:
            if existing.name.lower() == clean_name.lower():
                raise WalletError(
                    f"Label {existing.name!r} already exists ({existing.doc_id})"
                )

        now_iso = utc_now_iso()
        doc_id = f"-HashTag_{uuid.uuid4()}"
        payload: dict[str, Any] = {
            "_id": doc_id,
            "name": clean_name,
            "color": color,
            "archived": False,
            "reservedModelType": "HashTag",
            "reservedOwnerId": self.replication.owner_id,
            "reservedAuthorId": self.replication.owner_id,
            "reservedCreatedAt": now_iso,
            "reservedUpdatedAt": now_iso,
            "reservedSource": "web",
        }
        LabelDocument.from_couch_dict(payload)

        if dry_run:
            return {
                "dryRun": True,
                "operation": "LABEL_CREATE",
                "id": doc_id,
                "name": clean_name,
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
        self.cache.refresh()
        return {
            "dryRun": False,
            "operation": "LABEL_CREATE",
            "id": doc_id,
            "rev": put_res.get("rev"),
            "name": clean_name,
            "journalEntryId": journal_entry.entry_id,
            "document": saved_doc,
        }

    def rename_label(
        self,
        label_query: str,
        new_name: str,
        *,
        color: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Rename an existing label and/or update its color."""
        clean_name = new_name.strip().lstrip("#").strip()
        if not clean_name:
            raise SchemaValidationError("New label name must be a non-empty string")

        lbl = self.resolver.resolve_label(label_query)
        before_raw = self.client.get_doc(lbl.doc_id)
        after_payload = dict(before_raw)
        after_payload["name"] = clean_name
        if color is not None:
            after_payload["color"] = color
        after_payload["reservedUpdatedAt"] = utc_now_iso()
        LabelDocument.from_couch_dict(after_payload)

        if dry_run:
            return {
                "dryRun": True,
                "operation": "LABEL_UPDATE",
                "id": lbl.doc_id,
                "oldName": lbl.name,
                "newName": clean_name,
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
        self.cache.refresh()
        return {
            "dryRun": False,
            "operation": "LABEL_UPDATE",
            "id": lbl.doc_id,
            "rev": put_res.get("rev"),
            "oldName": lbl.name,
            "newName": clean_name,
            "journalEntryId": journal_entry.entry_id,
            "document": saved_after,
        }

    def delete_label(
        self,
        label_query: str,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Soft-delete a label (checking that 0 records reference it unless force=True)."""
        lbl = self.resolver.resolve_label(label_query)
        raw_records = self.client.all_docs_by_prefix("Record_")
        referencing = [
            r
            for r in raw_records
            if r.get("reservedModelType") == "Record"
            and lbl.doc_id in (r.get("labels") or [])
        ]
        if referencing and not force:
            raise WalletError(
                f"Label {lbl.name!r} ({lbl.doc_id}) is currently attached to {len(referencing)} record(s). "
                "Pass force=True (--force) to detach it from those records and delete."
            )

        before_lbl = self.client.get_doc(lbl.doc_id)
        before_docs: list[dict[str, Any]] = [dict(before_lbl)]
        docs_to_put: list[dict[str, Any]] = []
        now_iso = utc_now_iso()

        for raw_rec in referencing:
            before_docs.append(dict(raw_rec))
            rec = RecordDocument.from_couch_dict(raw_rec)
            remaining_labels = tuple(x for x in rec.labels if x != lbl.doc_id)
            updated_rec = replace(
                rec,
                labels=remaining_labels,
                reserved_updated_at=now_iso,
            )
            docs_to_put.append(updated_rec.to_couch_dict())

        tombstone = dict(before_lbl)
        tombstone["_deleted"] = True
        tombstone["reservedUpdatedAt"] = now_iso
        docs_to_put.append(tombstone)

        if dry_run:
            return {
                "dryRun": True,
                "operation": "LABEL_DELETE",
                "id": lbl.doc_id,
                "name": lbl.name,
                "detachedRecordCount": len(referencing),
                "before": before_docs,
                "after": docs_to_put,
            }

        bulk_res = self.client.bulk_docs(docs_to_put)
        saved_after: list[dict[str, Any]] = []
        for doc_dict, res_item in zip(docs_to_put, bulk_res):
            copy_doc = dict(doc_dict)
            copy_doc["_rev"] = res_item.get("rev")
            saved_after.append(copy_doc)

        journal_entry = self.journal.record(
            operation="DELETE",
            before_docs=before_docs,
            after_docs=saved_after,
        )
        self.cache.refresh()
        return {
            "dryRun": False,
            "operation": "LABEL_DELETE",
            "id": lbl.doc_id,
            "name": lbl.name,
            "detachedRecordCount": len(referencing),
            "journalEntryId": journal_entry.entry_id,
        }

    # -------------------------------------------------------------------------
    # Category Management (CRUD + Configure hidden_envelopes sync)
    # -------------------------------------------------------------------------

    def _resolve_parent_envelope_id(self, parent_query: str) -> int:
        q = parent_query.strip()
        if q.isdigit():
            return int(q)
        parent_cat = self.resolver.resolve_category(q)
        return parent_cat.envelope_id

    def _prepare_unhide_envelope(
        self, envelope_id: int, now_iso: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """If envelope_id is listed in -Configure_<uuid>.hidden_envelopes, prepare update to unhide it."""
        before_cfg: list[dict[str, Any]] = []
        after_cfg: list[dict[str, Any]] = []
        target_token = str(envelope_id)
        for cfg in self.client.all_docs_by_prefix("-Configure_"):
            hidden_raw = str(cfg.get("hidden_envelopes", "") or "")
            tokens = [t.strip() for t in hidden_raw.split(",") if t.strip()]
            if target_token in tokens:
                before_cfg.append(dict(cfg))
                updated_cfg = dict(cfg)
                updated_cfg["hidden_envelopes"] = ",".join(
                    t for t in tokens if t != target_token
                )
                updated_cfg["reservedUpdatedAt"] = now_iso
                after_cfg.append(updated_cfg)
        return before_cfg, after_cfg

    def add_category(
        self,
        name: str,
        *,
        parent_query: str,
        color: str = "#ff1744",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a custom subcategory under the specified parent envelope."""
        clean_name = name.strip()
        if not clean_name:
            raise SchemaValidationError("Category name must be a non-empty string")

        parent_env_id = self._resolve_parent_envelope_id(parent_query)
        snap = self.cache.get_snapshot(force_refresh=True)
        for existing in snap.categories:
            if (
                existing.envelope_id == parent_env_id
                and existing.display_name.lower() == clean_name.lower()
            ):
                raise WalletError(
                    f"Category {clean_name!r} already exists under envelope {parent_env_id} ({existing.doc_id})"
                )

        now_iso = utc_now_iso()
        doc_id = f"-Category_{uuid.uuid4()}"
        cat_payload: dict[str, Any] = {
            "_id": doc_id,
            "name": clean_name,
            "envelopeId": parent_env_id,
            "customCategory": True,
            "customName": True,
            "color": color,
            "icon": parent_env_id,
            "reservedModelType": "Category",
            "reservedOwnerId": self.replication.owner_id,
            "reservedAuthorId": self.replication.owner_id,
            "reservedCreatedAt": now_iso,
            "reservedUpdatedAt": now_iso,
            "reservedSource": "web",
        }
        CategoryDocument.from_couch_dict(cat_payload)

        before_cfg, after_cfg = self._prepare_unhide_envelope(parent_env_id, now_iso)
        docs_to_put = [cat_payload, *after_cfg]

        if dry_run:
            return {
                "dryRun": True,
                "operation": "CATEGORY_CREATE",
                "id": doc_id,
                "name": clean_name,
                "envelopeId": parent_env_id,
                "unhiddenConfigureCount": len(after_cfg),
                "documents": docs_to_put,
            }

        bulk_res = self.client.bulk_docs(docs_to_put)
        saved_after: list[dict[str, Any]] = []
        for doc_dict, res_item in zip(docs_to_put, bulk_res):
            copy_doc = dict(doc_dict)
            copy_doc["_rev"] = res_item.get("rev")
            saved_after.append(copy_doc)

        journal_entry = self.journal.record(
            operation="CREATE" if not before_cfg else "UPDATE",
            before_docs=before_cfg,
            after_docs=saved_after,
        )
        self.cache.refresh()
        return {
            "dryRun": False,
            "operation": "CATEGORY_CREATE",
            "id": doc_id,
            "rev": saved_after[0].get("_rev"),
            "name": clean_name,
            "envelopeId": parent_env_id,
            "journalEntryId": journal_entry.entry_id,
            "document": saved_after[0],
        }

    def rename_category(
        self,
        category_query: str,
        new_name: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Rename an existing category (built-in or custom) and set customName=True."""
        clean_name = new_name.strip()
        if not clean_name:
            raise SchemaValidationError("New category name must be a non-empty string")

        cat = self.resolver.resolve_category(category_query)
        before_raw = self.client.get_doc(cat.doc_id)
        after_payload = dict(before_raw)
        after_payload["name"] = clean_name
        after_payload["customName"] = True
        after_payload["reservedUpdatedAt"] = utc_now_iso()
        CategoryDocument.from_couch_dict(after_payload)

        if dry_run:
            return {
                "dryRun": True,
                "operation": "CATEGORY_RENAME",
                "id": cat.doc_id,
                "oldName": cat.display_name,
                "newName": clean_name,
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
        self.cache.refresh()
        return {
            "dryRun": False,
            "operation": "CATEGORY_RENAME",
            "id": cat.doc_id,
            "rev": put_res.get("rev"),
            "oldName": cat.display_name,
            "newName": clean_name,
            "journalEntryId": journal_entry.entry_id,
            "document": saved_after,
        }

    def move_category(
        self,
        category_query: str,
        *,
        parent_query: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Move/re-parent a custom subcategory under a different parent envelope."""
        cat = self.resolver.resolve_category(category_query)
        new_env_id = self._resolve_parent_envelope_id(parent_query)
        now_iso = utc_now_iso()

        before_raw = self.client.get_doc(cat.doc_id)
        after_payload = dict(before_raw)
        after_payload["envelopeId"] = new_env_id
        after_payload["icon"] = new_env_id
        after_payload["customCategory"] = True
        after_payload["reservedUpdatedAt"] = now_iso

        before_cfg, after_cfg = self._prepare_unhide_envelope(new_env_id, now_iso)
        before_docs = [before_raw, *before_cfg]
        docs_to_put = [after_payload, *after_cfg]

        if dry_run:
            return {
                "dryRun": True,
                "operation": "CATEGORY_MOVE",
                "id": cat.doc_id,
                "name": cat.display_name,
                "oldEnvelopeId": cat.envelope_id,
                "newEnvelopeId": new_env_id,
                "before": before_docs,
                "after": docs_to_put,
            }

        bulk_res = self.client.bulk_docs(docs_to_put)
        saved_after: list[dict[str, Any]] = []
        for doc_dict, res_item in zip(docs_to_put, bulk_res):
            copy_doc = dict(doc_dict)
            copy_doc["_rev"] = res_item.get("rev")
            saved_after.append(copy_doc)

        journal_entry = self.journal.record(
            operation="UPDATE",
            before_docs=before_docs,
            after_docs=saved_after,
        )
        self.cache.refresh()
        return {
            "dryRun": False,
            "operation": "CATEGORY_MOVE",
            "id": cat.doc_id,
            "rev": saved_after[0].get("_rev"),
            "name": cat.display_name,
            "oldEnvelopeId": cat.envelope_id,
            "newEnvelopeId": new_env_id,
            "journalEntryId": journal_entry.entry_id,
            "document": saved_after[0],
        }

    def delete_category(
        self,
        category_query: str,
        *,
        move_records_to: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Soft-delete a category (optionally migrating its records to `move_records_to` first)."""
        cat = self.resolver.resolve_category(category_query)
        target_cat: CategoryDocument | None = None
        if move_records_to is not None:
            target_cat = self.resolver.resolve_category(move_records_to)
            if target_cat.doc_id == cat.doc_id:
                raise WalletError("Target category for record migration cannot be the deleted category itself")

        raw_records = self.client.all_docs_by_prefix("Record_")
        referencing = [
            r
            for r in raw_records
            if r.get("reservedModelType") == "Record"
            and r.get("categoryId") == cat.doc_id
        ]
        if referencing and target_cat is None:
            raise WalletError(
                f"Category {cat.display_name!r} ({cat.doc_id}) is referenced by {len(referencing)} record(s). "
                "Specify move_records_to (--move-to <category>) to migrate those records before deleting."
            )

        before_cat = self.client.get_doc(cat.doc_id)
        before_docs: list[dict[str, Any]] = [dict(before_cat)]
        docs_to_put: list[dict[str, Any]] = []
        now_iso = utc_now_iso()

        if target_cat is not None:
            for raw_rec in referencing:
                before_docs.append(dict(raw_rec))
                rec = RecordDocument.from_couch_dict(raw_rec)
                updated_rec = replace(
                    rec,
                    category_id=target_cat.doc_id,
                    category_changed=True,
                    reserved_updated_at=now_iso,
                )
                docs_to_put.append(updated_rec.to_couch_dict())

        tombstone = dict(before_cat)
        tombstone["_deleted"] = True
        tombstone["reservedUpdatedAt"] = now_iso
        docs_to_put.append(tombstone)

        if dry_run:
            return {
                "dryRun": True,
                "operation": "CATEGORY_DELETE",
                "id": cat.doc_id,
                "name": cat.display_name,
                "migratedRecordCount": len(referencing),
                "migratedTo": target_cat.display_name if target_cat else None,
                "before": before_docs,
                "after": docs_to_put,
            }

        bulk_res = self.client.bulk_docs(docs_to_put)
        saved_after: list[dict[str, Any]] = []
        for doc_dict, res_item in zip(docs_to_put, bulk_res):
            copy_doc = dict(doc_dict)
            copy_doc["_rev"] = res_item.get("rev")
            saved_after.append(copy_doc)

        journal_entry = self.journal.record(
            operation="DELETE",
            before_docs=before_docs,
            after_docs=saved_after,
        )
        self.cache.refresh()
        return {
            "dryRun": False,
            "operation": "CATEGORY_DELETE",
            "id": cat.doc_id,
            "name": cat.display_name,
            "migratedRecordCount": len(referencing),
            "migratedTo": target_cat.display_name if target_cat else None,
            "journalEntryId": journal_entry.entry_id,
        }

    # -------------------------------------------------------------------------
    # Record & Transfer CRUD
    # -------------------------------------------------------------------------

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
        category = self.resolver.resolve_leaf_category(category_query)
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

    def link_transfer(
        self,
        first_record_id: str,
        second_record_id: str,
        *,
        note: str | None = None,
        payee: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Link two existing records on different accounts into an atomic paired transfer."""
        id_a = normalize_record_id(first_record_id)
        id_b = normalize_record_id(second_record_id)
        if id_a == id_b:
            raise SchemaValidationError("Cannot link a record to itself as a transfer pair")

        raw_a = self.client.get_doc(id_a)
        raw_b = self.client.get_doc(id_b)
        rec_a = RecordDocument.from_couch_dict(raw_a)
        rec_b = RecordDocument.from_couch_dict(raw_b)

        if rec_a.account_id == rec_b.account_id:
            raise SchemaValidationError(
                "Both records belong to the same account; transfer legs must be on different accounts"
            )

        # Auto-orient outflow (EXPENSE=1) and inflow (INCOME=0) if one is expense and one is income
        if rec_a.record_type == int(RecordType.INCOME) and rec_b.record_type == int(RecordType.EXPENSE):
            out_raw, in_raw = raw_b, raw_a
            out_rec, in_rec = rec_b, rec_a
        else:
            out_raw, in_raw = raw_a, raw_b
            out_rec, in_rec = rec_a, rec_b

        transfer_cat = self.resolver.resolve_transfer_category()
        shared_transfer_id = out_rec.transfer_id or in_rec.transfer_id or str(uuid.uuid4())
        now_iso = utc_now_iso()

        updated_out = replace(
            out_rec,
            category_id=transfer_cat.doc_id,
            record_type=int(RecordType.EXPENSE),
            transfer=True,
            transfer_id=shared_transfer_id,
            transfer_account_id=in_rec.account_id,
            category_changed=True,
            note=note if note is not None else out_rec.note,
            payee=payee if payee is not None else out_rec.payee,
            reserved_updated_at=now_iso,
        )
        updated_in = replace(
            in_rec,
            category_id=transfer_cat.doc_id,
            record_type=int(RecordType.INCOME),
            transfer=True,
            transfer_id=shared_transfer_id,
            transfer_account_id=out_rec.account_id,
            category_changed=True,
            note=note if note is not None else in_rec.note,
            payee=payee if payee is not None else in_rec.payee,
            reserved_updated_at=now_iso,
        )

        docs_payload = [updated_out.to_couch_dict(), updated_in.to_couch_dict()]
        snap = self.cache.get_snapshot()
        from_acc = snap.accounts_by_id.get(updated_out.account_id)
        to_acc = snap.accounts_by_id.get(updated_in.account_id)

        if dry_run:
            return {
                "dryRun": True,
                "operation": "TRANSFER_LINK",
                "transferId": shared_transfer_id,
                "outflowId": updated_out.doc_id,
                "inflowId": updated_in.doc_id,
                "fromAccount": from_acc.name if from_acc else updated_out.account_id,
                "toAccount": to_acc.name if to_acc else updated_in.account_id,
                "before": [out_raw, in_raw],
                "after": docs_payload,
            }

        bulk_res = self.client.bulk_docs(docs_payload)
        saved_docs: list[dict[str, Any]] = []
        for doc_dict, res_item in zip(docs_payload, bulk_res):
            item_copy = dict(doc_dict)
            item_copy["_rev"] = res_item.get("rev")
            saved_docs.append(item_copy)

        journal_entry = self.journal.record(
            operation="UPDATE",
            before_docs=[out_raw, in_raw],
            after_docs=saved_docs,
        )
        return {
            "dryRun": False,
            "operation": "TRANSFER_LINK",
            "transferId": shared_transfer_id,
            "outflowId": updated_out.doc_id,
            "inflowId": updated_in.doc_id,
            "fromAccount": from_acc.name if from_acc else updated_out.account_id,
            "toAccount": to_acc.name if to_acc else updated_in.account_id,
            "journalEntryId": journal_entry.entry_id,
            "documents": saved_docs,
        }

    def convert_to_transfer(
        self,
        record_id: str,
        *,
        target_account_query: str,
        target_amount: NumericAmount | None = None,
        note: str | None = None,
        payee: str | None = None,
        allow_bank_connected: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Convert an existing single-leg expense/income record into a paired transfer by creating the mirror leg."""
        norm_id = normalize_record_id(record_id)
        before_raw = self.client.get_doc(norm_id)
        existing = RecordDocument.from_couch_dict(before_raw)

        target_acc = self.resolver.resolve_account(target_account_query)
        if target_acc.doc_id == existing.account_id:
            raise SchemaValidationError(
                f"Target account {target_acc.name!r} is the same as the record's current account"
            )
        if target_acc.bank_connected and not allow_bank_connected:
            raise BankConnectedAccountError(
                f"Target account {target_acc.name!r} is bank-connected; pass allow_bank_connected=True (--allow-bank-connected) to override."
            )

        target_curr = self.resolver.resolve_currency(target_acc.currency_id)
        transfer_cat = self.resolver.resolve_transfer_category()
        shared_transfer_id = existing.transfer_id or str(uuid.uuid4())
        now_iso = utc_now_iso()

        if target_amount is not None:
            mirror_amount = decimal_to_minor_units(target_amount)
            mirror_ref = compute_ref_amount(mirror_amount, target_curr)
        elif target_curr.doc_id == existing.currency_id:
            mirror_amount = existing.amount
            mirror_ref = existing.ref_amount
        else:
            ratio = Decimal(str(target_curr.ratio_to_referential))
            mirror_amount = max(
                1,
                int((Decimal(existing.ref_amount) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            )
            mirror_ref = compute_ref_amount(mirror_amount, target_curr)

        mirror_type = (
            int(RecordType.INCOME)
            if existing.record_type == int(RecordType.EXPENSE)
            else int(RecordType.EXPENSE)
        )
        final_note = note if note is not None else existing.note
        final_payee = payee if payee is not None else existing.payee

        updated_existing = replace(
            existing,
            category_id=transfer_cat.doc_id,
            transfer=True,
            transfer_id=shared_transfer_id,
            transfer_account_id=target_acc.doc_id,
            category_changed=True,
            note=final_note,
            payee=final_payee,
            reserved_updated_at=now_iso,
        )

        mirror_doc = RecordDocument(
            doc_id=f"Record_{uuid.uuid4()}",
            account_id=target_acc.doc_id,
            category_id=transfer_cat.doc_id,
            currency_id=target_curr.doc_id,
            amount=mirror_amount,
            ref_amount=mirror_ref,
            record_type=mirror_type,
            payment_type=existing.payment_type,
            record_state=existing.record_state,
            record_date=existing.record_date,
            reserved_owner_id=existing.reserved_owner_id,
            reserved_author_id=existing.reserved_author_id,
            reserved_created_at=now_iso,
            reserved_updated_at=now_iso,
            reserved_source="web",
            reserved_model_type="Record",
            note=final_note,
            payee=final_payee,
            labels=existing.labels,
            transfer=True,
            transfer_id=shared_transfer_id,
            transfer_account_id=existing.account_id,
            category_changed=True,
            category_confirm_reason=existing.category_confirm_reason,
        )

        docs_payload = [updated_existing.to_couch_dict(), mirror_doc.to_couch_dict()]
        snap = self.cache.get_snapshot()
        source_acc = snap.accounts_by_id.get(existing.account_id)
        source_name = source_acc.name if source_acc else existing.account_id

        outflow_id = (
            updated_existing.doc_id
            if updated_existing.record_type == int(RecordType.EXPENSE)
            else mirror_doc.doc_id
        )
        inflow_id = (
            mirror_doc.doc_id
            if updated_existing.record_type == int(RecordType.EXPENSE)
            else updated_existing.doc_id
        )

        if dry_run:
            return {
                "dryRun": True,
                "operation": "TRANSFER_CONVERT",
                "transferId": shared_transfer_id,
                "existingId": updated_existing.doc_id,
                "mirrorId": mirror_doc.doc_id,
                "outflowId": outflow_id,
                "inflowId": inflow_id,
                "sourceAccount": source_name,
                "targetAccount": target_acc.name,
                "before": [before_raw],
                "after": docs_payload,
            }

        bulk_res = self.client.bulk_docs(docs_payload)
        saved_docs: list[dict[str, Any]] = []
        for doc_dict, res_item in zip(docs_payload, bulk_res):
            item_copy = dict(doc_dict)
            item_copy["_rev"] = res_item.get("rev")
            saved_docs.append(item_copy)

        journal_entry = self.journal.record(
            operation="TRANSFER_CONVERT",
            before_docs=[before_raw],
            after_docs=saved_docs,
        )
        return {
            "dryRun": False,
            "operation": "TRANSFER_CONVERT",
            "transferId": shared_transfer_id,
            "existingId": updated_existing.doc_id,
            "mirrorId": mirror_doc.doc_id,
            "outflowId": outflow_id,
            "inflowId": inflow_id,
            "sourceAccount": source_name,
            "targetAccount": target_acc.name,
            "journalEntryId": journal_entry.entry_id,
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
        lbl_names = [
            snap.labels_by_id[lid].name if lid in snap.labels_by_id else lid
            for lid in rec.labels
        ]
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
            "labels": lbl_names,
            "transfer": rec.transfer,
            "transferId": rec.transfer_id,
            "document": raw,
        }

    def list_records(
        self,
        *,
        account_query: str | None = None,
        category_query: str | None = None,
        label_query: str | None = None,
        unlabeled: bool = False,
        date_from: str | None = None,
        date_to: str | None = None,
        min_amount: NumericAmount | None = None,
        max_amount: NumericAmount | None = None,
        exact_amount: NumericAmount | None = None,
        record_type_filter: str | None = None,
        search: str | None = None,
        limit: int = 20,
        use_cache: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch, filter, and sort records newest-first with resolved account/category/label names."""
        snap = self.cache.get_snapshot()
        account_id_filter: str | None = None
        if account_query:
            account_id_filter = self.resolver.resolve_account(account_query).doc_id
        category_id_filter: str | None = None
        if category_query:
            category_id_filter = self.resolver.resolve_category(category_query).doc_id
        label_id_filter: str | None = None
        if label_query:
            label_id_filter = self.resolver.resolve_label(label_query).doc_id

        from_iso: str | None = None
        if date_from:
            from_iso = normalize_timestamp_input(date_from)
        to_iso: str | None = None
        if date_to:
            raw_to = date_to.strip()
            if len(raw_to) == 10 and raw_to[4] == "-" and raw_to[7] == "-":
                to_iso = f"{raw_to}T23:59:59.999Z"
            else:
                to_iso = normalize_timestamp_input(raw_to)

        min_minor = decimal_to_minor_units(min_amount) if min_amount is not None else None
        max_minor = decimal_to_minor_units(max_amount) if max_amount is not None else None
        exact_minor = decimal_to_minor_units(exact_amount) if exact_amount is not None else None

        type_mode: str | None = None
        transfer_cat_id: str | None = None
        if record_type_filter:
            type_mode = record_type_filter.strip().lower().replace("_", "-")
            if type_mode not in ("expense", "income", "transfer", "unlinked-transfer"):
                raise SchemaValidationError(
                    f"Invalid record type filter {record_type_filter!r}; expected expense, income, transfer, or unlinked-transfer"
                )
            try:
                transfer_cat_id = self.resolver.resolve_transfer_category().doc_id
            except EntityNotFoundError:
                transfer_cat_id = None

        raw_records: list[dict[str, Any]] | None = None
        if use_cache:
            raw_records = self.cache.get_cached_records()
        if raw_records is None:
            raw_records = self.client.all_docs_by_prefix("Record_")

        filtered: list[dict[str, Any]] = []
        q_search = search.strip().lower() if search else None

        for raw in raw_records:
            if raw.get("reservedModelType") != "Record" or raw.get("_deleted"):
                continue
            if account_id_filter and raw.get("accountId") != account_id_filter:
                continue
            if category_id_filter and raw.get("categoryId") != category_id_filter:
                continue

            rec_labels = raw.get("labels") or []
            if unlabeled and len(rec_labels) > 0:
                continue
            if label_id_filter and label_id_filter not in rec_labels:
                continue

            rec_date = str(raw.get("recordDate", ""))
            if from_iso and rec_date < from_iso:
                continue
            if to_iso and rec_date > to_iso:
                continue

            amt_minor = abs(int(round(float(raw.get("amount", 0)))))
            if exact_minor is not None and amt_minor != exact_minor:
                continue
            if min_minor is not None and amt_minor < min_minor:
                continue
            if max_minor is not None and amt_minor > max_minor:
                continue

            if type_mode:
                is_xfer = bool(raw.get("transfer")) or (
                    transfer_cat_id is not None and raw.get("categoryId") == transfer_cat_id
                )
                r_type = int(raw.get("type", 1))
                if type_mode == "expense" and (r_type != int(RecordType.EXPENSE) or is_xfer):
                    continue
                if type_mode == "income" and (r_type != int(RecordType.INCOME) or is_xfer):
                    continue
                if type_mode == "transfer" and not is_xfer:
                    continue
                if type_mode == "unlinked-transfer":
                    is_unlinked = is_xfer and (
                        not raw.get("transfer")
                        or not raw.get("transferId")
                        or not raw.get("transferAccountId")
                    )
                    if not is_unlinked:
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

        sliced = filtered if limit <= 0 else filtered[:limit]
        results: list[dict[str, Any]] = []
        for raw in sliced:
            rec_type = int(raw.get("type", 1))
            amt = float(raw.get("amount", 0)) / 100.0
            signed = -amt if rec_type == int(RecordType.EXPENSE) else amt
            acc = snap.accounts_by_id.get(str(raw.get("accountId", "")))
            cat = snap.categories_by_id.get(str(raw.get("categoryId", "")))
            cur = snap.currencies_by_id.get(str(raw.get("currencyId", "")))
            lbl_names = [
                snap.labels_by_id[str(lid)].name
                if str(lid) in snap.labels_by_id
                else str(lid)
                for lid in (raw.get("labels") or [])
            ]
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
                    "labels": lbl_names,
                    "transfer": bool(raw.get("transfer", False)),
                    "transferId": raw.get("transferId"),
                }
            )
        return results

    @staticmethod
    def _compute_updated_note(
        existing_note: str,
        note: str | None,
        prepend_note: str | None,
        append_note: str | None,
    ) -> str:
        res = note if note is not None else existing_note
        if prepend_note:
            if not res.startswith(prepend_note):
                res = f"{prepend_note}{res}"
        if append_note:
            if not res.endswith(append_note):
                res = f"{res}{append_note}"
        return res

    @staticmethod
    def _compute_updated_labels(
        existing_labels: tuple[str, ...],
        replace_ids: tuple[str, ...] | None,
        add_ids: tuple[str, ...],
        remove_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        base = list(replace_ids) if replace_ids is not None else list(existing_labels)
        for lid in add_ids:
            if lid not in base:
                base.append(lid)
        if remove_ids:
            remove_set = set(remove_ids)
            base = [lid for lid in base if lid not in remove_set]
        return tuple(base)

    def update_records(
        self,
        record_ids: Sequence[str],
        *,
        amount: NumericAmount | None = None,
        note: str | None = None,
        prepend_note: str | None = None,
        append_note: str | None = None,
        payee: str | None = None,
        category_query: str | None = None,
        account_query: str | None = None,
        record_type: RecordType | str | None = None,
        payment_type: PaymentType | str | None = None,
        record_date: str | None = None,
        labels: Iterable[str] | None = None,
        add_labels: Iterable[str] | None = None,
        remove_labels: Iterable[str] | None = None,
        sync_transfer_leg: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Batch-update one or more Record_<uuid> documents atomically via _bulk_docs."""
        norm_ids: list[str] = []
        for rid in record_ids:
            nid = normalize_record_id(rid)
            if nid not in norm_ids:
                norm_ids.append(nid)
        if not norm_ids:
            raise SchemaValidationError("At least one record_id is required for update")

        resolved_account = (
            self.resolver.resolve_account(account_query)
            if account_query is not None
            else None
        )
        resolved_category = (
            self.resolver.resolve_leaf_category(category_query)
            if category_query is not None
            else None
        )
        resolved_amount_minor = (
            decimal_to_minor_units(amount) if amount is not None else None
        )
        resolved_type = (
            int(
                RecordType.from_str(record_type)
                if isinstance(record_type, str)
                else record_type
            )
            if record_type is not None
            else None
        )
        resolved_payment_type = (
            int(
                PaymentType.from_str(payment_type)
                if isinstance(payment_type, str)
                else payment_type
            )
            if payment_type is not None
            else None
        )
        resolved_date = (
            normalize_timestamp_input(record_date) if record_date is not None else None
        )
        replace_label_ids = (
            self.resolver.resolve_labels(labels) if labels is not None else None
        )
        add_label_ids = self.resolver.resolve_labels(add_labels or ())
        remove_label_ids = self.resolver.resolve_labels(remove_labels or ())

        now_iso = utc_now_iso()
        before_docs: list[dict[str, Any]] = []
        after_docs: list[dict[str, Any]] = []
        processed_ids: set[str] = set()
        all_records_cache: list[dict[str, Any]] | None = None

        for nid in norm_ids:
            before_raw = self.client.get_doc(nid)
            existing = RecordDocument.from_couch_dict(before_raw)
            before_docs.append(before_raw)
            processed_ids.add(nid)

            new_account_id = (
                resolved_account.doc_id if resolved_account else existing.account_id
            )
            new_currency_id = (
                resolved_account.currency_id
                if resolved_account
                else existing.currency_id
            )
            currency = self.resolver.resolve_currency(new_currency_id)

            new_amount = (
                resolved_amount_minor
                if resolved_amount_minor is not None
                else existing.amount
            )
            new_ref_amount = compute_ref_amount(new_amount, currency)
            new_category_id = (
                resolved_category.doc_id
                if resolved_category
                else existing.category_id
            )
            cat_changed = (
                new_category_id != existing.category_id
            ) or existing.category_changed

            new_note = self._compute_updated_note(
                existing.note, note, prepend_note, append_note
            )
            new_labels = self._compute_updated_labels(
                existing.labels, replace_label_ids, add_label_ids, remove_label_ids
            )

            updated_model = replace(
                existing,
                account_id=new_account_id,
                currency_id=new_currency_id,
                category_id=new_category_id,
                amount=new_amount,
                ref_amount=new_ref_amount,
                record_type=(
                    resolved_type if resolved_type is not None else existing.record_type
                ),
                payment_type=(
                    resolved_payment_type
                    if resolved_payment_type is not None
                    else existing.payment_type
                ),
                record_date=(
                    resolved_date if resolved_date is not None else existing.record_date
                ),
                note=new_note,
                payee=payee if payee is not None else existing.payee,
                labels=new_labels,
                category_changed=cat_changed,
                reserved_updated_at=now_iso,
            )
            after_docs.append(updated_model.to_couch_dict())

            # Auto-synchronize paired transfer leg if applicable
            if sync_transfer_leg and existing.transfer and existing.transfer_id:
                if all_records_cache is None:
                    all_records_cache = self.client.all_docs_by_prefix("Record_")
                for candidate in all_records_cache:
                    cand_id = str(candidate.get("_id", ""))
                    if (
                        candidate.get("transferId") == existing.transfer_id
                        and cand_id != nid
                        and cand_id not in norm_ids
                        and cand_id not in processed_ids
                    ):
                        processed_ids.add(cand_id)
                        before_docs.append(dict(candidate))
                        pair_rec = RecordDocument.from_couch_dict(candidate)
                        pair_note = self._compute_updated_note(
                            pair_rec.note, note, prepend_note, append_note
                        )
                        pair_labels = self._compute_updated_labels(
                            pair_rec.labels,
                            replace_label_ids,
                            add_label_ids,
                            remove_label_ids,
                        )
                        pair_amount = pair_rec.amount
                        pair_ref = pair_rec.ref_amount
                        if (
                            resolved_amount_minor is not None
                            and pair_rec.currency_id == new_currency_id
                        ):
                            pair_amount = new_amount
                            pair_ref = new_ref_amount

                        updated_pair = replace(
                            pair_rec,
                            amount=pair_amount,
                            ref_amount=pair_ref,
                            transfer_account_id=(
                                new_account_id
                                if resolved_account is not None
                                else pair_rec.transfer_account_id
                            ),
                            record_date=(
                                resolved_date
                                if resolved_date is not None
                                else pair_rec.record_date
                            ),
                            note=pair_note,
                            payee=payee if payee is not None else pair_rec.payee,
                            labels=pair_labels,
                            reserved_updated_at=now_iso,
                        )
                        after_docs.append(updated_pair.to_couch_dict())

        if dry_run:
            return {
                "dryRun": True,
                "operation": "UPDATE",
                "updatedIds": [d["_id"] for d in after_docs],
                "before": before_docs,
                "after": after_docs,
            }

        if len(after_docs) == 1:
            put_res = self.client.put_doc(after_docs[0], retry_on_conflict=True)
            saved_one = dict(after_docs[0])
            saved_one["_rev"] = put_res.get("rev")
            saved_after = [saved_one]
        else:
            bulk_res = self.client.bulk_docs(after_docs)
            saved_after = []
            for doc_dict, res_item in zip(after_docs, bulk_res):
                copy_doc = dict(doc_dict)
                copy_doc["_rev"] = res_item.get("rev")
                saved_after.append(copy_doc)

        journal_entry = self.journal.record(
            operation="UPDATE",
            before_docs=before_docs,
            after_docs=saved_after,
        )
        return {
            "dryRun": False,
            "operation": "UPDATE",
            "updatedIds": [d["_id"] for d in saved_after],
            "journalEntryId": journal_entry.entry_id,
            "documents": saved_after,
        }

    def update_record(
        self,
        record_id: str,
        *,
        amount: NumericAmount | None = None,
        note: str | None = None,
        prepend_note: str | None = None,
        append_note: str | None = None,
        payee: str | None = None,
        category_query: str | None = None,
        account_query: str | None = None,
        record_type: RecordType | str | None = None,
        payment_type: PaymentType | str | None = None,
        record_date: str | None = None,
        labels: Iterable[str] | None = None,
        add_labels: Iterable[str] | None = None,
        remove_labels: Iterable[str] | None = None,
        sync_transfer_leg: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Safely update a single Record_<uuid> document (and its paired transfer leg if applicable)."""
        norm_id = normalize_record_id(record_id)
        batch_res = self.update_records(
            [norm_id],
            amount=amount,
            note=note,
            prepend_note=prepend_note,
            append_note=append_note,
            payee=payee,
            category_query=category_query,
            account_query=account_query,
            record_type=record_type,
            payment_type=payment_type,
            record_date=record_date,
            labels=labels,
            add_labels=add_labels,
            remove_labels=remove_labels,
            sync_transfer_leg=sync_transfer_leg,
            dry_run=dry_run,
        )
        if dry_run:
            return {
                "dryRun": True,
                "operation": "UPDATE",
                "id": norm_id,
                "updatedIds": batch_res["updatedIds"],
                "before": batch_res["before"][0],
                "after": batch_res["after"][0],
                "allAfter": batch_res["after"],
            }
        primary = batch_res["documents"][0]
        return {
            "dryRun": False,
            "operation": "UPDATE",
            "id": norm_id,
            "rev": primary.get("_rev"),
            "updatedIds": batch_res["updatedIds"],
            "journalEntryId": batch_res["journalEntryId"],
            "document": primary,
            "documents": batch_res["documents"],
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

    def list_journal(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent mutation journal entries newest-first."""
        entries = list(reversed(self.journal.list_entries()))
        sliced = entries if limit <= 0 else entries[:limit]
        return [
            {
                "entryId": e.entry_id,
                "timestamp": e.timestamp,
                "operation": e.operation,
                "beforeCount": len(e.before_docs),
                "afterCount": len(e.after_docs),
                "undone": e.undone,
            }
            for e in sliced
        ]

    def undo_last(
        self,
        dry_run: bool = False,
        *,
        entry_id: str | None = None,
    ) -> dict[str, Any]:
        """Revert the most recent non-undone mutation (or a specific journal entry by ID)."""
        if entry_id is not None:
            entry = self.journal.get_entry(entry_id)
            if entry.undone:
                raise WalletError(
                    f"Journal entry {entry.entry_id} has already been undone"
                )
        else:
            found = self.journal.get_last_undoable()
            if found is None:
                raise WalletError(
                    "No undoable operations found in the mutation journal"
                )
            entry = found

        if dry_run:
            return {
                "dryRun": True,
                "undoEntryId": entry.entry_id,
                "originalOperation": entry.operation,
                "originalTimestamp": entry.timestamp,
                "beforeDocs": entry.before_docs,
                "afterDocs": entry.after_docs,
            }

        before_by_id = {
            str(d["_id"]): d for d in entry.before_docs if isinstance(d, dict) and "_id" in d
        }
        after_by_id = {
            str(d["_id"]): d for d in entry.after_docs if isinstance(d, dict) and "_id" in d
        }

        reverted_ids: list[str] = []
        touched_metadata = False

        # 1. Restore any document that existed prior to the mutation
        for doc_id, prev_doc in before_by_id.items():
            restored = dict(prev_doc)
            restored.pop("_deleted", None)
            try:
                current = self.client.get_doc(doc_id)
                restored["_rev"] = current["_rev"]
            except EntityNotFoundError:
                restored.pop("_rev", None)
            self.client.put_doc(restored, retry_on_conflict=True)
            reverted_ids.append(doc_id)
            if doc_id.startswith(("-Category_", "-HashTag_", "-Account_", "-Currency_")):
                touched_metadata = True

        # 2. Soft-delete any document that was newly created in the mutation
        for doc_id in after_by_id:
            if doc_id not in before_by_id:
                try:
                    self.client.delete_doc(doc_id)
                except EntityNotFoundError:
                    pass
                reverted_ids.append(doc_id)
                if doc_id.startswith(("-Category_", "-HashTag_", "-Account_", "-Currency_")):
                    touched_metadata = True

        self.journal.mark_undone(entry.entry_id)
        if touched_metadata:
            self.cache.refresh()

        return {
            "dryRun": False,
            "undoneEntryId": entry.entry_id,
            "originalOperation": entry.operation,
            "revertedIds": reverted_ids,
        }
