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


if __name__ == "__main__":
    unittest.main()

