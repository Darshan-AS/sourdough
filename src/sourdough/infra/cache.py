"""Local metadata cache for Accounts, Categories, Currencies, and Labels."""

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any

from sourdough.config import write_secure_json
from sourdough.domain.models import (
    AccountDocument,
    CategoryDocument,
    CurrencyDocument,
    LabelDocument,
)
from sourdough.infra.couchdb import CouchDBClient

DEFAULT_CACHE_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class MetadataSnapshot:
    """In-memory snapshot of user reference entities."""

    fetched_at: float
    accounts: list[AccountDocument]
    categories: list[CategoryDocument]
    currencies: list[CurrencyDocument]
    labels: list[LabelDocument]

    @property
    def currencies_by_id(self) -> dict[str, CurrencyDocument]:
        return {c.doc_id: c for c in self.currencies}

    @property
    def accounts_by_id(self) -> dict[str, AccountDocument]:
        return {a.doc_id: a for a in self.accounts}

    @property
    def categories_by_id(self) -> dict[str, CategoryDocument]:
        return {c.doc_id: c for c in self.categories}

    @property
    def labels_by_id(self) -> dict[str, LabelDocument]:
        return {l.doc_id: l for l in self.labels}


class MetadataCache:
    """Manages reading, writing, and refreshing the local metadata cache."""

    def __init__(
        self,
        cache_file: Path,
        client: CouchDBClient,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self.cache_file = cache_file
        self.client = client
        self.ttl_seconds = ttl_seconds
        self._memory_snapshot: MetadataSnapshot | None = None

    def get_snapshot(self, force_refresh: bool = False) -> MetadataSnapshot:
        """Return valid metadata snapshot from memory, disk, or fresh CouchDB sync."""
        now = time.time()
        if not force_refresh and self._memory_snapshot is not None:
            if (now - self._memory_snapshot.fetched_at) < self.ttl_seconds:
                return self._memory_snapshot

        if not force_refresh and self.cache_file.exists():
            try:
                loaded = self._load_from_disk()
                if (now - loaded.fetched_at) < self.ttl_seconds:
                    self._memory_snapshot = loaded
                    return loaded
            except Exception:
                pass

        return self.refresh()

    def refresh(self, include_records: bool = False) -> MetadataSnapshot:
        """Fetch Accounts, Categories, Currencies, and Labels (and optionally Records) from CouchDB and persist."""
        raw_accounts = self.client.all_docs_by_prefix("-Account_")
        raw_categories = self.client.all_docs_by_prefix("-Category_")
        raw_currencies = self.client.all_docs_by_prefix("-Currency_")
        raw_labels = self.client.all_docs_by_prefix("-HashTag_")

        existing_records: list[dict[str, Any]] | None = None
        existing_records_ts: float | None = None
        if not include_records and self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    old_payload = json.load(f)
                if isinstance(old_payload.get("records"), list):
                    existing_records = old_payload["records"]
                    existing_records_ts = old_payload.get("records_fetched_at")
            except Exception:
                pass

        now = time.time()
        payload: dict[str, Any] = {
            "fetched_at": now,
            "accounts": raw_accounts,
            "categories": raw_categories,
            "currencies": raw_currencies,
            "labels": raw_labels,
        }
        if include_records:
            payload["records"] = self.client.all_docs_by_prefix("Record_")
            payload["records_fetched_at"] = now
        elif existing_records is not None:
            payload["records"] = existing_records
            payload["records_fetched_at"] = existing_records_ts

        write_secure_json(self.cache_file, payload)
        snapshot = self._parse_payload(payload)
        self._memory_snapshot = snapshot
        return snapshot

    def get_cached_records(self) -> list[dict[str, Any]] | None:
        """Return cached raw Record_ dicts from disk if present, otherwise None."""
        if not self.cache_file.exists():
            return None
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            recs = payload.get("records")
            if isinstance(recs, list):
                return [r for r in recs if isinstance(r, dict) and not r.get("_deleted")]
        except Exception:
            pass
        return None

    def _load_from_disk(self) -> MetadataSnapshot:
        with open(self.cache_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return self._parse_payload(payload)

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> MetadataSnapshot:
        accounts = [
            AccountDocument.from_couch_dict(d)
            for d in payload.get("accounts", [])
            if isinstance(d, dict) and d.get("reservedModelType") == "Account" and not d.get("_deleted")
        ]
        categories = [
            CategoryDocument.from_couch_dict(d)
            for d in payload.get("categories", [])
            if isinstance(d, dict) and d.get("reservedModelType") == "Category" and not d.get("_deleted")
        ]
        currencies = [
            CurrencyDocument.from_couch_dict(d)
            for d in payload.get("currencies", [])
            if isinstance(d, dict) and d.get("reservedModelType") == "Currency" and not d.get("_deleted")
        ]
        labels = [
            LabelDocument.from_couch_dict(d)
            for d in payload.get("labels", [])
            if isinstance(d, dict) and d.get("reservedModelType") == "HashTag" and not d.get("_deleted")
        ]
        return MetadataSnapshot(
            fetched_at=float(payload.get("fetched_at", 0.0)),
            accounts=accounts,
            categories=categories,
            currencies=currencies,
            labels=labels,
        )
