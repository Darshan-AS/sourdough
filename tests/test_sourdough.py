"""Unit and integration tests for sourdough SDK and `dough` CLI."""

from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, Optional, Tuple
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sourdough.cli.app import run_cli
from sourdough.config import ReplicationConfig, StateManager, WalletPaths
from sourdough.domain.builders import (
    build_record,
    build_transfer_pair,
    compute_ref_amount,
    decimal_to_minor_units,
    normalize_timestamp_input,
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
    AmbiguousEntityError,
    BankConnectedAccountError,
    ConflictError,
    EntityNotFoundError,
    SchemaValidationError,
    WalletError,
)
from sourdough.services.records import WalletService


OWNER_ID = "00000000-1111-4222-8333-444444444444"
INR_ID = "-Currency_10000000-0000-4000-8000-000000000001"
THB_ID = "-Currency_10000000-0000-4000-8000-000000000002"
CASH_ACC_ID = "-Account_20000000-0000-4000-8000-000000000001"
HDFC_ACC_ID = "-Account_20000000-0000-4000-8000-000000000002"
THAI_ACC_ID = "-Account_20000000-0000-4000-8000-000000000003"
GROC_CAT_ID = "-Category_30000000-0000-4000-8000-000000000001"
XFER_CAT_ID = "-Category_11111111-2222-4333-8444-555555555555"
LABEL_ID = "-HashTag_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FakeCouchServer:
    """In-memory fake CouchDB + web.budgetbakers.com server for deterministic testing."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {
            INR_ID: {
                "_id": INR_ID,
                "_rev": "1-inr",
                "code": "INR",
                "referential": True,
                "ratioToReferential": 1.0,
                "reservedModelType": "Currency",
            },
            THB_ID: {
                "_id": THB_ID,
                "_rev": "1-thb",
                "code": "THB",
                "referential": False,
                "ratioToReferential": 0.4,
                "reservedModelType": "Currency",
            },
            CASH_ACC_ID: {
                "_id": CASH_ACC_ID,
                "_rev": "1-cash",
                "name": "Cash",
                "currencyId": INR_ID,
                "accountType": 1,
                "locked": True,
                "archived": False,
                "reservedModelType": "Account",
            },
            HDFC_ACC_ID: {
                "_id": HDFC_ACC_ID,
                "_rev": "1-hdfc",
                "name": "HDFC",
                "currencyId": INR_ID,
                "accountType": 0,
                "locked": False,
                "archived": False,
                "reservedModelType": "Account",
            },
            THAI_ACC_ID: {
                "_id": THAI_ACC_ID,
                "_rev": "1-thai",
                "name": "Thai Cash",
                "currencyId": THB_ID,
                "accountType": 1,
                "locked": True,
                "archived": False,
                "reservedIntegrationConnection": {"provider": "saltedge"},
                "reservedModelType": "Account",
            },
            GROC_CAT_ID: {
                "_id": GROC_CAT_ID,
                "_rev": "1-groc",
                "name": "Groceries",
                "envelopeId": 1000,
                "customCategory": False,
                "reservedModelType": "Category",
            },
            XFER_CAT_ID: {
                "_id": XFER_CAT_ID,
                "_rev": "1-xfer",
                "name": "Transfer, withdraw",
                "envelopeId": 20001,
                "systemCategory": 1,
                "customCategory": False,
                "reservedModelType": "Category",
            },
            LABEL_ID: {
                "_id": LABEL_ID,
                "_rev": "1-lbl",
                "name": "vacation",
                "archived": False,
                "reservedModelType": "HashTag",
            },
        }
        self.rev_counter = 2

    def handle(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        if "web.budgetbakers.com/api/trpc/user.getUser" in url:
            resp = [
                {
                    "result": {
                        "data": {
                            "json": {
                                "userId": OWNER_ID,
                                "email": "itisdarshan@gmail.com",
                                "fullName": "Darshan A S",
                                "replication": {
                                    "dbName": f"bb-{OWNER_ID}",
                                    "url": "https://couch-prod-asia-1.budgetbakers.com",
                                    "login": OWNER_ID,
                                    "token": "refreshed-token-999",
                                    "ownerId": OWNER_ID,
                                },
                            }
                        }
                    }
                }
            ]
            return 200, {"content-type": "application/json"}, json.dumps(resp).encode()

        base_prefix = f"https://couch-prod-asia-1.budgetbakers.com/bb-{OWNER_ID}"
        rel = url[len(base_prefix) :].lstrip("/")
        path_part, _, query_part = rel.partition("?")

        if method == "GET" and path_part == "":
            active = [d for d in self.docs.values() if not d.get("_deleted")]
            deleted = [d for d in self.docs.values() if d.get("_deleted")]
            return (
                200,
                {},
                json.dumps(
                    {
                        "db_name": f"bb-{OWNER_ID}",
                        "doc_count": len(active),
                        "doc_del_count": len(deleted),
                        "update_seq": self.rev_counter,
                    }
                ).encode(),
            )

        if method == "GET" and path_part == "_all_docs":
            import urllib.parse

            qs = urllib.parse.parse_qs(query_part)
            startkey = json.loads(qs["startkey"][0])
            endkey = json.loads(qs["endkey"][0])
            rows = []
            for doc_id in sorted(self.docs.keys()):
                if startkey <= doc_id <= endkey:
                    doc = self.docs[doc_id]
                    if not doc.get("_deleted"):
                        rows.append({"id": doc_id, "key": doc_id, "value": {"rev": doc["_rev"]}, "doc": doc})
            return 200, {}, json.dumps({"rows": rows}).encode()

        if method == "POST" and path_part == "_bulk_docs":
            payload = json.loads(body.decode()) if body else {}
            results = []
            for item in payload.get("docs", []):
                doc_id = item["_id"]
                self.rev_counter += 1
                new_rev = f"{self.rev_counter}-bulk"
                stored = dict(item)
                stored["_rev"] = new_rev
                self.docs[doc_id] = stored
                results.append({"ok": True, "id": doc_id, "rev": new_rev})
            return 201, {}, json.dumps(results).encode()

        import urllib.parse

        doc_id = urllib.parse.unquote(path_part)
        if method == "GET":
            doc = self.docs.get(doc_id)
            if not doc or doc.get("_deleted"):
                return 404, {}, json.dumps({"error": "not_found", "reason": "missing"}).encode()
            return 200, {}, json.dumps(doc).encode()

        if method == "PUT":
            incoming = json.loads(body.decode()) if body else {}
            existing = self.docs.get(doc_id)
            if existing and not existing.get("_deleted"):
                if incoming.get("_rev") != existing.get("_rev"):
                    return (
                        409,
                        {},
                        json.dumps({"error": "conflict", "reason": "Document update conflict"}).encode(),
                    )
            self.rev_counter += 1
            new_rev = f"{self.rev_counter}-rev"
            stored = dict(incoming)
            stored["_id"] = doc_id
            stored["_rev"] = new_rev
            self.docs[doc_id] = stored
            return 201, {}, json.dumps({"ok": True, "id": doc_id, "rev": new_rev}).encode()

        return 400, {}, json.dumps({"error": "bad_request"}).encode()


class DomainValidationTests(unittest.TestCase):
    """Tests for strict domain invariants and minor-unit conversions."""

    def setUp(self) -> None:
        self.inr = CurrencyDocument(
            doc_id=INR_ID, code="INR", referential=True, ratio_to_referential=1.0
        )
        self.thb = CurrencyDocument(
            doc_id=THB_ID, code="THB", referential=False, ratio_to_referential=0.4
        )
        self.cash_acc = AccountDocument(
            doc_id=CASH_ACC_ID, name="Cash", currency_id=INR_ID
        )
        self.groc_cat = CategoryDocument(
            doc_id=GROC_CAT_ID, name="Groceries", envelope_id=1000
        )

    def test_decimal_to_minor_units_valid_and_invalid(self) -> None:
        self.assertEqual(decimal_to_minor_units(1000), 100000)
        self.assertEqual(decimal_to_minor_units("250.75"), 25075)
        self.assertEqual(decimal_to_minor_units(Decimal("0.05")), 5)

        for bad in (0, -100, "0.00", "-50", "10.999", True, False, "abc", "NaN", "Infinity"):
            with self.assertRaises(SchemaValidationError, msg=f"Expected failure for {bad!r}"):
                decimal_to_minor_units(bad)  # type: ignore[arg-type]

    def test_fx_ref_amount_calculation(self) -> None:
        self.assertEqual(compute_ref_amount(10000, self.inr), 10000)
        # 100.00 THB (10000 satang) / 0.4 = 25000 paise (250.00 INR)
        self.assertEqual(compute_ref_amount(10000, self.thb), 25000)

    def test_record_document_rejects_invalid_invariants(self) -> None:
        valid = build_record(
            owner_id=OWNER_ID,
            account=self.cash_acc,
            currency=self.inr,
            category=self.groc_cat,
            amount_major="1000.00",
            note="Valid record",
        )
        self.assertEqual(valid.amount, 100000)
        self.assertEqual(valid.ref_amount, 100000)

        # Reject float or bool amount
        with self.assertRaises(SchemaValidationError):
            RecordDocument(
                doc_id=valid.doc_id,
                account_id=valid.account_id,
                category_id=valid.category_id,
                currency_id=valid.currency_id,
                amount=True,  # type: ignore[arg-type]
                ref_amount=100000,
                record_type=1,
                payment_type=0,
                record_state=1,
                record_date=valid.record_date,
                reserved_owner_id=OWNER_ID,
                reserved_author_id=OWNER_ID,
                reserved_created_at=valid.reserved_created_at,
                reserved_updated_at=valid.reserved_updated_at,
            )

        # Reject bad ID prefix
        with self.assertRaises(SchemaValidationError):
            RecordDocument(
                doc_id="BadPrefix_0248e561-4f76-4bab-96cc-75132c1f396c",
                account_id=valid.account_id,
                category_id=valid.category_id,
                currency_id=valid.currency_id,
                amount=100000,
                ref_amount=100000,
                record_type=1,
                payment_type=0,
                record_state=1,
                record_date=valid.record_date,
                reserved_owner_id=OWNER_ID,
                reserved_author_id=OWNER_ID,
                reserved_created_at=valid.reserved_created_at,
                reserved_updated_at=valid.reserved_updated_at,
            )

        # Reject naive timestamp without Z or offset
        with self.assertRaises(SchemaValidationError):
            RecordDocument(
                doc_id=valid.doc_id,
                account_id=valid.account_id,
                category_id=valid.category_id,
                currency_id=valid.currency_id,
                amount=100000,
                ref_amount=100000,
                record_type=1,
                payment_type=0,
                record_state=1,
                record_date="2026-10-17T10:00:00",
                reserved_owner_id=OWNER_ID,
                reserved_author_id=OWNER_ID,
                reserved_created_at=valid.reserved_created_at,
                reserved_updated_at=valid.reserved_updated_at,
            )


class ServiceAndCliIntegrationTests(unittest.TestCase):
    """End-to-end tests of WalletService and CLI against FakeCouchServer."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.paths = WalletPaths(
            state_file=base / ".wallet_state.json",
            cache_file=base / "cache.json",
            journal_file=base / "journal.jsonl",
        )
        initial_state = {
            "email": "itisdarshan@gmail.com",
            "userId": OWNER_ID,
            "sessionToken": "valid-session-token",
            "replication": {
                "dbName": f"bb-{OWNER_ID}",
                "url": "https://couch-prod-asia-1.budgetbakers.com",
                "login": OWNER_ID,
                "token": "initial-token",
                "ownerId": OWNER_ID,
            },
        }
        self.paths.state_file.write_text(json.dumps(initial_state), encoding="utf-8")
        self.server = FakeCouchServer()
        self.service = WalletService(paths=self.paths, transport=self.server.handle)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_crud_and_undo_lifecycle(self) -> None:
        # 1. Create record
        created = self.service.add_record(
            account_query="cash",
            category_query="groceries",
            amount="1000",
            note="SDK test entry",
            payee="Local Market",
            labels=["vacation"],
        )
        rec_id = created["id"]
        self.assertTrue(rec_id.startswith("Record_"))
        self.assertEqual(created["document"]["amount"], 100000)
        self.assertEqual(created["document"]["labels"], [LABEL_ID])

        # Verify secure 0600 file permissions on cache and journal
        cache_mode = os.stat(self.paths.cache_file).st_mode & 0o777
        journal_mode = os.stat(self.paths.journal_file).st_mode & 0o777
        self.assertEqual(cache_mode, 0o600)
        self.assertEqual(journal_mode, 0o600)

        # 2. Update record
        updated = self.service.update_record(rec_id, amount="1250.50", note="Updated note")
        self.assertEqual(updated["document"]["amount"], 125050)
        self.assertEqual(updated["document"]["note"], "Updated note")

        # 3. Undo the update -> reverts back to 1000.00 and "SDK test entry"
        undo_upd = self.service.undo_last()
        self.assertEqual(undo_upd["originalOperation"], "UPDATE")
        fetched = self.service.get_record(rec_id)
        self.assertEqual(fetched["signedAmount"], -1000.0)
        self.assertEqual(fetched["note"], "SDK test entry")

        # 4. Undo the create -> deletes the record
        undo_create = self.service.undo_last()
        self.assertEqual(undo_create["originalOperation"], "CREATE")
        with self.assertRaises(EntityNotFoundError):
            self.service.get_record(rec_id)

    def test_paired_transfer_and_cascading_delete(self) -> None:
        xfer = self.service.add_transfer(
            from_account_query="HDFC",
            to_account_query="Cash",
            amount="2500",
            note="ATM withdrawal",
        )
        out_id = xfer["outflowId"]
        in_id = xfer["inflowId"]

        out_doc = self.service.get_record(out_id)
        in_doc = self.service.get_record(in_id)
        self.assertEqual(out_doc["signedAmount"], -2500.0)
        self.assertEqual(in_doc["signedAmount"], 2500.0)
        self.assertEqual(
            out_doc["document"]["transferId"],
            in_doc["document"]["transferId"],
        )

        # Deleting one leg automatically deletes both legs
        del_res = self.service.delete_record(out_id)
        self.assertEqual(set(del_res["deletedIds"]), {out_id, in_id})
        with self.assertRaises(EntityNotFoundError):
            self.service.get_record(in_id)

    def test_bank_connected_guard(self) -> None:
        with self.assertRaises(BankConnectedAccountError):
            self.service.add_record(
                account_query="Thai Cash",
                category_query="Groceries",
                amount="500",
            )

    def test_auth_refresh_updates_replication_token(self) -> None:
        res = self.service.auth_refresh()
        self.assertTrue(res["refreshed"])
        reloaded = StateManager(self.paths.state_file).load_replication()
        self.assertEqual(reloaded.token, "refreshed-token-999")

    def test_cli_autocompletion_callbacks(self) -> None:
        from sourdough.cli.app import complete_account, complete_category, complete_label

        self.service.sync_cache()
        old_cache = os.environ.get("DOUGH_CACHE_FILE")
        os.environ["DOUGH_CACHE_FILE"] = str(self.paths.cache_file)
        try:
            self.assertEqual(complete_account("ca"), ["Cash"])
            self.assertEqual(complete_category("groc"), ["Groceries"])
            self.assertEqual(complete_label("vac"), ["vacation"])
        finally:
            if old_cache is None:
                os.environ.pop("DOUGH_CACHE_FILE", None)
            else:
                os.environ["DOUGH_CACHE_FILE"] = old_cache

    def test_cli_help_commands(self) -> None:
        import contextlib
        import io
        from sourdough.cli.app import run_cli

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_cli(["-h"]), 0)
            self.assertEqual(run_cli(["help"]), 0)
            self.assertEqual(run_cli(["help", "record", "add"]), 0)
            self.assertEqual(run_cli(["help", "transfer", "link"]), 0)
            self.assertEqual(run_cli(["help", "transfer", "convert"]), 0)
            self.assertEqual(run_cli(["help", "categories", "add"]), 0)
            self.assertEqual(run_cli(["help", "labels", "add"]), 0)

    def test_record_list_filters_and_local_cache(self) -> None:
        r1 = self.service.add_record(
            account_query="HDFC",
            category_query="Groceries",
            amount="250.00",
            record_type="expense",
            record_date="2026-01-05",
            note="Milk and bread",
            labels=["vacation"],
        )
        r2 = self.service.add_record(
            account_query="Cash",
            category_query="Groceries",
            amount="1500.00",
            record_type="income",
            record_date="2026-01-15",
            note="Cashback refund",
        )
        xfer = self.service.add_transfer(
            from_account_query="HDFC",
            to_account_query="Cash",
            amount="500.00",
            record_date="2026-01-20",
            note="ATM withdrawal",
        )

        # Label & unlabeled filters
        by_lbl = self.service.list_records(label_query="#vacation")
        self.assertEqual([r["id"] for r in by_lbl], [r1["id"]])

        unlabeled = self.service.list_records(unlabeled=True)
        self.assertIn(r2["id"], [r["id"] for r in unlabeled])
        self.assertNotIn(r1["id"], [r["id"] for r in unlabeled])

        # Date range filter
        jan_early = self.service.list_records(date_from="2026-01-01", date_to="2026-01-10")
        self.assertEqual([r["id"] for r in jan_early], [r1["id"]])

        # Amount filters
        exact = self.service.list_records(exact_amount="250")
        self.assertEqual([r["id"] for r in exact], [r1["id"]])
        mid_range = self.service.list_records(min_amount="400", max_amount="600")
        self.assertEqual({r["id"] for r in mid_range}, {xfer["outflowId"], xfer["inflowId"]})

        # Type filters
        only_exp = self.service.list_records(record_type_filter="expense")
        self.assertEqual([r["id"] for r in only_exp], [r1["id"]])
        only_inc = self.service.list_records(record_type_filter="income")
        self.assertEqual([r["id"] for r in only_inc], [r2["id"]])
        only_xfer = self.service.list_records(record_type_filter="transfer")
        self.assertEqual({r["id"] for r in only_xfer}, {xfer["outflowId"], xfer["inflowId"]})

        # Local record cache sync & query
        self.service.sync_cache(include_records=True)
        cached_rows = self.service.list_records(use_cache=True, limit=0)
        self.assertEqual(len(cached_rows), 4)

    def test_batch_update_incremental_labels_notes_and_transfer_sync(self) -> None:
        self.service.add_label("Family")
        self.service.add_label("Shared with BrotherMine")

        rec1 = self.service.add_record(
            account_query="HDFC",
            category_query="Groceries",
            amount="300",
            note="Flight ticket",
            labels=["vacation"],
        )
        rec2 = self.service.add_record(
            account_query="HDFC",
            category_query="Groceries",
            amount="450",
            note="Hotel stay",
        )

        batch_res = self.service.update_records(
            [rec1["id"], rec2["id"]],
            prepend_note="[Thailand Jan 2024] ",
            append_note=" (verified)",
            add_labels=["#Family", "Shared with BrotherMine"],
            remove_labels=["vacation"],
        )
        self.assertEqual(len(batch_res["updatedIds"]), 2)

        f1 = self.service.get_record(rec1["id"])
        f2 = self.service.get_record(rec2["id"])
        self.assertEqual(f1["note"], "[Thailand Jan 2024] Flight ticket (verified)")
        self.assertEqual(f2["note"], "[Thailand Jan 2024] Hotel stay (verified)")
        self.assertEqual(set(f1["labels"]), {"Family", "Shared with BrotherMine"})
        self.assertEqual(set(f2["labels"]), {"Family", "Shared with BrotherMine"})

        # Paired transfer leg auto-sync on update
        xfer = self.service.add_transfer(
            from_account_query="HDFC",
            to_account_query="Cash",
            amount="1000",
            note="Initial note",
        )
        upd_xfer = self.service.update_record(
            xfer["outflowId"],
            amount="1200",
            prepend_note="[Synced] ",
            payee="IndMoney",
            add_labels=["Family"],
        )
        self.assertEqual(set(upd_xfer["updatedIds"]), {xfer["outflowId"], xfer["inflowId"]})
        inflow_after = self.service.get_record(xfer["inflowId"])
        self.assertEqual(inflow_after["signedAmount"], 1200.0)
        self.assertEqual(inflow_after["note"], "[Synced] Initial note")
        self.assertEqual(inflow_after["payee"], "IndMoney")
        self.assertEqual(inflow_after["labels"], ["Family"])

    def test_transfer_link_and_convert_with_undo(self) -> None:
        # 1. Link two existing standalone records (passed inflow-first to verify auto-orientation)
        inflow_rec = self.service.add_record(
            account_query="Cash",
            category_query="Groceries",
            amount="2000",
            record_type="income",
            note="Unlinked cash deposit",
        )
        outflow_rec = self.service.add_record(
            account_query="HDFC",
            category_query="Groceries",
            amount="2000",
            record_type="expense",
            note="Unlinked bank withdrawal",
        )

        linked = self.service.link_transfer(
            inflow_rec["id"],
            outflow_rec["id"],
            note="Linked ATM transfer",
        )
        self.assertEqual(linked["outflowId"], outflow_rec["id"])
        self.assertEqual(linked["inflowId"], inflow_rec["id"])

        out_doc = self.service.get_record(outflow_rec["id"])
        in_doc = self.service.get_record(inflow_rec["id"])
        self.assertTrue(out_doc["transfer"])
        self.assertTrue(in_doc["transfer"])
        self.assertEqual(out_doc["transferId"], linked["transferId"])
        self.assertEqual(out_doc["document"]["transferAccountId"], CASH_ACC_ID)
        self.assertEqual(in_doc["document"]["transferAccountId"], HDFC_ACC_ID)

        # 2. Convert a single-leg expense into a paired cross-currency transfer (HDFC INR -> Thai Cash THB)
        single_exp = self.service.add_record(
            account_query="HDFC",
            category_query="Groceries",
            amount="10000",
            record_type="expense",
            note="Forex load",
            payee="OrientExchange",
        )
        converted = self.service.convert_to_transfer(
            single_exp["id"],
            target_account_query="Thai Cash",
            target_amount="4000",
            allow_bank_connected=True,
        )
        mirror_id = converted["mirrorId"]
        mirror_doc = self.service.get_record(mirror_id)
        self.assertEqual(mirror_doc["account"], "Thai Cash")
        self.assertEqual(mirror_doc["signedAmount"], 4000.0)
        self.assertEqual(mirror_doc["payee"], "OrientExchange")
        self.assertEqual(mirror_doc["transferId"], converted["transferId"])

        # 3. Undo the conversion -> restores single_exp to non-transfer Groceries AND deletes mirror_id!
        undo_res = self.service.undo_last(entry_id=converted["journalEntryId"][:8])
        self.assertIn(single_exp["id"], undo_res["revertedIds"])
        self.assertIn(mirror_id, undo_res["revertedIds"])

        restored_exp = self.service.get_record(single_exp["id"])
        self.assertFalse(restored_exp["transfer"])
        self.assertEqual(restored_exp["category"], "Groceries")
        with self.assertRaises(EntityNotFoundError):
            self.service.get_record(mirror_id)

    def test_category_and_label_crud_and_hierarchy_resolution(self) -> None:
        # Seed a -Configure_ document with hidden_envelopes containing 1001
        cfg_id = "-Configure_99999999-0000-4000-8000-000000000001"
        self.server.docs[cfg_id] = {
            "_id": cfg_id,
            "_rev": "1-cfg",
            "hidden_envelopes": "1001,1002",
            "reservedModelType": "Configure",
        }
        # Seed built-in 1001 category
        rest_cat_id = "-Category_30000000-0000-4000-8000-000000001001"
        self.server.docs[rest_cat_id] = {
            "_id": rest_cat_id,
            "_rev": "1-rest",
            "name": "",
            "envelopeId": 1001,
            "customCategory": False,
            "reservedModelType": "Category",
        }
        self.service.sync_cache()

        # Add custom subcategory under 1001 -> automatically unhides 1001 in -Configure_
        added_cat = self.service.add_category("Meals & Fast Food", parent_query="1001")
        self.assertEqual(added_cat["envelopeId"], 1001)
        self.assertEqual(self.server.docs[cfg_id]["hidden_envelopes"], "1002")

        # Resolve using Parent > Child syntax
        resolved_by_id = self.service.resolver.resolve_category("1001 > Meals & Fast Food")
        resolved_by_name = self.service.resolver.resolve_category(
            "Restaurant, fast-food > Meals & Fast Food"
        )
        self.assertEqual(resolved_by_id.doc_id, added_cat["id"])
        self.assertEqual(resolved_by_name.doc_id, added_cat["id"])

        # Rename & move category
        renamed = self.service.rename_category(added_cat["id"], "Quick Meals")
        self.assertEqual(renamed["newName"], "Quick Meals")
        moved = self.service.move_category(added_cat["id"], parent_query="1000")
        self.assertEqual(moved["newEnvelopeId"], 1000)

        # Attach a record to Quick Meals, verify delete blocks without move_records_to, then migrates with move_records_to
        rec = self.service.add_record(
            account_query="Cash",
            category_query="Quick Meals",
            amount="120",
        )
        with self.assertRaises(WalletError):
            self.service.delete_category("Quick Meals")

        del_cat = self.service.delete_category("Quick Meals", move_records_to="Groceries")
        self.assertEqual(del_cat["migratedRecordCount"], 1)
        self.assertEqual(self.service.get_record(rec["id"])["category"], "Groceries")

        # Label lifecycle: add, rename, delete (with guard and --force)
        lbl = self.service.add_label("#TripTag")
        self.assertEqual(lbl["name"], "TripTag")
        self.service.update_record(rec["id"], add_labels=["TripTag"])
        self.service.rename_label("TripTag", "HolidayTag")
        self.assertIn("HolidayTag", self.service.get_record(rec["id"])["labels"])

        with self.assertRaises(WalletError):
            self.service.delete_label("HolidayTag", force=False)
        del_lbl = self.service.delete_label("HolidayTag", force=True)
        self.assertEqual(del_lbl["detachedRecordCount"], 1)
        self.assertEqual(self.service.get_record(rec["id"])["labels"], [])
        self.assertEqual(len(self.service.list_journal(limit=5)), 5)

    def test_debt_direction_leaf_category_and_transfer_matching(self) -> None:
        # 1. Seed Loan and Debts (8002) and Lending, renting (10005)
        loan_cat_id = "-Category_30000000-0000-4000-8000-000000008002"
        lend_cat_id = "-Category_30000000-0000-4000-8000-000000010005"
        rest_parent_id = "-Category_30000000-0000-4000-8000-000000001001"
        self.server.docs[loan_cat_id] = {
            "_id": loan_cat_id,
            "_rev": "1-loan",
            "name": "Loan and Debts",
            "envelopeId": 8002,
            "customCategory": False,
            "reservedModelType": "Category",
        }
        self.server.docs[lend_cat_id] = {
            "_id": lend_cat_id,
            "_rev": "1-lend",
            "name": "Lending, renting",
            "envelopeId": 10005,
            "customCategory": False,
            "reservedModelType": "Category",
        }
        self.server.docs[rest_parent_id] = {
            "_id": rest_parent_id,
            "_rev": "1-rest",
            "name": "Restaurants & fast food",
            "envelopeId": 1001,
            "customCategory": False,
            "reservedModelType": "Category",
        }
        self.service.sync_cache()

        # Verify direction guardrails for 8002 and 10005
        with self.assertRaises(SchemaValidationError):
            self.service.add_record(
                account_query="HDFC",
                category_query="Loan and Debts",
                amount="500",
                record_type="income",
            )
        with self.assertRaises(SchemaValidationError):
            self.service.add_record(
                account_query="HDFC",
                category_query="Lending, renting",
                amount="500",
                record_type="expense",
            )

        # Valid directions succeed
        exp_debt = self.service.add_record(
            account_query="HDFC",
            category_query="Loan and Debts",
            amount="500",
            record_type="expense",
        )
        inc_repay = self.service.add_record(
            account_query="HDFC",
            category_query="Lending, renting",
            amount="500",
            record_type="income",
        )
        self.assertEqual(exp_debt["document"]["type"], 1)
        self.assertEqual(inc_repay["document"]["type"], 0)

        # 2. Leaf category enforcement when custom subcategory exists under 1001
        self.service.add_category("Meals & Fast Food", parent_query="1001")
        with self.assertRaises(SchemaValidationError):
            self.service.add_record(
                account_query="HDFC",
                category_query="Restaurants & fast food",
                amount="250",
            )
        leaf_rec = self.service.add_record(
            account_query="HDFC",
            category_query="Meals & Fast Food",
            amount="250",
        )
        self.assertEqual(leaf_rec["categoryName"], "Meals & Fast Food")

        # 3. Open debt resolution and transfer pair matching
        debt_id = "-Debt_99999999-1111-4222-8333-444444444444"
        self.server.docs[debt_id] = {
            "_id": debt_id,
            "_rev": "1-debt",
            "name": "BrotherMine",
            "note": "Shared iPad EMI",
            "amount": 3000000,
            "remainingAmount": 1500000,
            "paidBack": False,
            "date": "2026-01-10T00:00:00.000Z",
            "reservedModelType": "Debt",
        }
        open_debt = self.service.find_open_debt("BrotherMine")
        self.assertIsNotNone(open_debt)
        assert open_debt is not None
        self.assertEqual(open_debt["_id"], debt_id)

        xfer = self.service.add_transfer(
            from_account_query="HDFC",
            to_account_query="Cash",
            amount="7322.29",
            record_date="2026-03-14T12:00:00Z",
            note="CC bill payment",
        )
        matched_pair = self.service.find_matching_transfer(
            from_account_id=HDFC_ACC_ID,
            to_account_id=CASH_ACC_ID,
            amount_minor=732229,
            date_iso="2026-03-15",
            tolerance_days=2,
        )
        self.assertIsNotNone(matched_pair)
        assert matched_pair is not None
        self.assertEqual(matched_pair[0]["_id"], xfer["outflowId"])
        self.assertEqual(matched_pair[1]["_id"], xfer["inflowId"])


if __name__ == "__main__":
    unittest.main()

