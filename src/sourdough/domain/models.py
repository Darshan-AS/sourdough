"""Strictly validated domain models matching Wallet's PouchDB and SQLite indexer invariants."""

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any, Self

from sourdough.domain.enums import (
    ENVELOPE_NAMES,
    PaymentType,
    RecordState,
    RecordType,
    TRANSFER_ENVELOPE_ID,
    TRANSFER_SYSTEM_CATEGORY,
)
from sourdough.exceptions import SchemaValidationError

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
RECORD_ID_PATTERN = re.compile(rf"^Record_{UUID_RE}$")
ACCOUNT_ID_PATTERN = re.compile(rf"^-Account_{UUID_RE}$")
CATEGORY_ID_PATTERN = re.compile(rf"^-Category_{UUID_RE}$")
CURRENCY_ID_PATTERN = re.compile(rf"^-Currency_{UUID_RE}$")
LABEL_ID_PATTERN = re.compile(rf"^-HashTag_{UUID_RE}$")
OWNER_ID_PATTERN = re.compile(rf"^{UUID_RE}$")


def _is_strict_int(val: Any) -> bool:
    """Return True if val is an int and not a bool."""
    return isinstance(val, int) and not isinstance(val, bool)


def validate_iso8601_utc(value: str, field_name: str) -> str:
    """Validate that a string is a valid ISO-8601 timestamp parseable by dayjs/Date."""
    if not isinstance(value, str) or not value.strip():
        raise SchemaValidationError(f"{field_name} must be a non-empty ISO-8601 string")
    normalized = value.strip()
    check_str = normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
    try:
        dt = datetime.fromisoformat(check_str)
    except ValueError as exc:
        raise SchemaValidationError(
            f"{field_name} has invalid ISO-8601 timestamp {value!r}: {exc}"
        ) from exc
    if dt.tzinfo is None:
        raise SchemaValidationError(
            f"{field_name} must include a timezone offset or 'Z' UTC suffix, got {value!r}"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class CurrencyDocument:
    """Represents a -Currency_<uuid> document in CouchDB."""

    doc_id: str
    code: str
    referential: bool
    ratio_to_referential: float
    rev: str | None = None

    def __post_init__(self) -> None:
        if not CURRENCY_ID_PATTERN.match(self.doc_id):
            raise SchemaValidationError(f"Invalid Currency _id: {self.doc_id!r}")
        if not self.code or not isinstance(self.code, str):
            raise SchemaValidationError("Currency code must be a non-empty string")
        if not isinstance(self.ratio_to_referential, (int, float)) or self.ratio_to_referential <= 0:
            raise SchemaValidationError(
                f"Currency ratioToReferential must be > 0, got {self.ratio_to_referential!r}"
            )

    @classmethod
    def from_couch_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            doc_id=str(data.get("_id", "")),
            rev=data.get("_rev"),
            code=str(data.get("code", "")),
            referential=bool(data.get("referential", False)),
            ratio_to_referential=float(data.get("ratioToReferential", 1.0) or 1.0),
        )


@dataclass(frozen=True, slots=True)
class AccountDocument:
    """Represents a -Account_<uuid> document in CouchDB."""

    doc_id: str
    name: str
    currency_id: str
    account_type: int = 0
    init_amount: int = 0
    init_ref_amount: int = 0
    locked: bool = False
    archived: bool = False
    exclude_from_stats: bool = False
    bank_connected: bool = False
    rev: str | None = None

    def __post_init__(self) -> None:
        if not ACCOUNT_ID_PATTERN.match(self.doc_id):
            raise SchemaValidationError(f"Invalid Account _id: {self.doc_id!r}")
        if not self.name or not isinstance(self.name, str):
            raise SchemaValidationError("Account name must be a non-empty string")
        if not CURRENCY_ID_PATTERN.match(self.currency_id):
            raise SchemaValidationError(f"Invalid Account currencyId: {self.currency_id!r}")

    @classmethod
    def from_couch_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            doc_id=str(data.get("_id", "")),
            rev=data.get("_rev"),
            name=str(data.get("name", "")),
            currency_id=str(data.get("currencyId", "")),
            account_type=int(data.get("accountType", 0)),
            init_amount=int(data.get("initAmount", 0)),
            init_ref_amount=int(data.get("initRefAmount", 0)),
            locked=bool(data.get("locked", False)),
            archived=bool(data.get("archived", False)),
            exclude_from_stats=bool(data.get("excludeFromStats", False)),
            bank_connected=bool(data.get("reservedIntegrationConnection")),
        )


@dataclass(frozen=True, slots=True)
class CategoryDocument:
    """Represents a -Category_<uuid> document in CouchDB."""

    doc_id: str
    name: str
    envelope_id: int = 0
    custom_category: bool = False
    custom_name: bool = False
    system_category: int | None = None
    color: str | None = None
    rev: str | None = None

    def __post_init__(self) -> None:
        if not CATEGORY_ID_PATTERN.match(self.doc_id):
            raise SchemaValidationError(f"Invalid Category _id: {self.doc_id!r}")

    @property
    def display_name(self) -> str:
        if self.name and (self.custom_name or self.custom_category or self.envelope_id not in ENVELOPE_NAMES):
            return self.name
        return ENVELOPE_NAMES.get(self.envelope_id, self.name or f"Category {self.envelope_id}")

    @property
    def is_transfer_category(self) -> bool:
        return (
            self.system_category == TRANSFER_SYSTEM_CATEGORY
            or self.envelope_id == TRANSFER_ENVELOPE_ID
        )

    @classmethod
    def from_couch_dict(cls, data: dict[str, Any]) -> Self:
        envelope_id = int(data.get("envelopeId", 0) or 0)
        raw_name = str(data.get("name", "") or "").strip()
        if not raw_name and envelope_id in ENVELOPE_NAMES:
            raw_name = ENVELOPE_NAMES[envelope_id]
        sys_cat = data.get("systemCategory")
        return cls(
            doc_id=str(data.get("_id", "")),
            rev=data.get("_rev"),
            name=raw_name,
            envelope_id=envelope_id,
            custom_category=bool(data.get("customCategory", False)),
            custom_name=bool(data.get("customName", False)),
            system_category=int(sys_cat) if sys_cat is not None else None,
            color=data.get("color"),
        )


@dataclass(frozen=True, slots=True)
class LabelDocument:
    """Represents a -HashTag_<uuid> document in CouchDB."""

    doc_id: str
    name: str
    color: str | None = None
    archived: bool = False
    rev: str | None = None

    def __post_init__(self) -> None:
        if not LABEL_ID_PATTERN.match(self.doc_id):
            raise SchemaValidationError(f"Invalid Label _id: {self.doc_id!r}")
        if not self.name or not isinstance(self.name, str):
            raise SchemaValidationError("Label name must be a non-empty string")

    @classmethod
    def from_couch_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            doc_id=str(data.get("_id", "")),
            rev=data.get("_rev"),
            name=str(data.get("name", "")),
            color=data.get("color"),
            archived=bool(data.get("archived", False)),
        )


@dataclass(frozen=True, slots=True)
class RecordDocument:
    """
    Strictly validated CouchDB Record_<uuid> document.

    Enforces every NOT NULL and type invariant required by Wallet's PouchDB-to-SQLite
    indexer (module 15168) and upsertRecordQuery (module 88437).
    """

    doc_id: str
    account_id: str
    category_id: str
    currency_id: str
    amount: int
    ref_amount: int
    record_type: int
    payment_type: int
    record_state: int
    record_date: str
    reserved_owner_id: str
    reserved_author_id: str
    reserved_created_at: str
    reserved_updated_at: str
    reserved_source: str = "web"
    reserved_model_type: str = "Record"
    note: str = ""
    payee: str = ""
    labels: tuple[str, ...] = field(default_factory=tuple)
    transfer: bool = False
    transfer_id: str | None = None
    transfer_account_id: str | None = None
    category_changed: bool = True
    category_confirm_reason: int = 4
    suggested_envelope_id: int = 0
    warranty_in_month: int = 0
    latitude: float = 0.0
    longitude: float = 0.0
    accuracy: int = 0
    photos: tuple[str, ...] = field(default_factory=tuple)
    ref_objects: tuple[Any, ...] = field(default_factory=tuple)
    rev: str | None = None
    extra_preserved_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not RECORD_ID_PATTERN.match(self.doc_id):
            raise SchemaValidationError(
                f"Invalid Record _id {self.doc_id!r}; must match 'Record_<uuid4>'"
            )
        if not ACCOUNT_ID_PATTERN.match(self.account_id):
            raise SchemaValidationError(
                f"Invalid accountId {self.account_id!r}; must match '-Account_<uuid>'"
            )
        if not CATEGORY_ID_PATTERN.match(self.category_id):
            raise SchemaValidationError(
                f"Invalid categoryId {self.category_id!r}; must match '-Category_<uuid>'"
            )
        if not CURRENCY_ID_PATTERN.match(self.currency_id):
            raise SchemaValidationError(
                f"Invalid currencyId {self.currency_id!r}; must match '-Currency_<uuid>'"
            )
        if not _is_strict_int(self.amount) or self.amount <= 0:
            raise SchemaValidationError(
                f"amount must be a positive integer in minor units (paise/cents), got {self.amount!r}"
            )
        if not _is_strict_int(self.ref_amount) or self.ref_amount <= 0:
            raise SchemaValidationError(
                f"refAmount must be a positive integer in minor units (paise/cents), got {self.ref_amount!r}"
            )
        if not _is_strict_int(self.record_type) or self.record_type not in (
            int(RecordType.INCOME),
            int(RecordType.EXPENSE),
        ):
            raise SchemaValidationError(
                f"type must be 0 (Income) or 1 (Expense), got {self.record_type!r}"
            )
        if not _is_strict_int(self.payment_type) or self.payment_type not in tuple(
            int(pt) for pt in PaymentType
        ):
            raise SchemaValidationError(f"Invalid paymentType: {self.payment_type!r}")
        if not _is_strict_int(self.record_state) or self.record_state not in tuple(
            int(rs) for rs in RecordState
        ):
            raise SchemaValidationError(f"Invalid recordState: {self.record_state!r}")

        validate_iso8601_utc(self.record_date, "recordDate")
        validate_iso8601_utc(self.reserved_created_at, "reservedCreatedAt")
        validate_iso8601_utc(self.reserved_updated_at, "reservedUpdatedAt")

        if not OWNER_ID_PATTERN.match(self.reserved_owner_id):
            raise SchemaValidationError(
                f"Invalid reservedOwnerId: {self.reserved_owner_id!r}"
            )
        if not OWNER_ID_PATTERN.match(self.reserved_author_id):
            raise SchemaValidationError(
                f"Invalid reservedAuthorId: {self.reserved_author_id!r}"
            )
        if self.reserved_model_type != "Record":
            raise SchemaValidationError(
                f"reservedModelType must be 'Record', got {self.reserved_model_type!r}"
            )
        if self.reserved_source not in ("web", "android", "ios", "backend"):
            raise SchemaValidationError(
                f"Invalid reservedSource: {self.reserved_source!r}"
            )

        if not isinstance(self.labels, tuple):
            raise SchemaValidationError("labels must be a tuple of label IDs")
        for lbl in self.labels:
            if not isinstance(lbl, str) or not LABEL_ID_PATTERN.match(lbl):
                raise SchemaValidationError(
                    f"Invalid label ID in labels array: {lbl!r}; must match '-HashTag_<uuid>'"
                )

        if self.transfer_account_id is not None:
            if not ACCOUNT_ID_PATTERN.match(self.transfer_account_id):
                raise SchemaValidationError(
                    f"Invalid transferAccountId: {self.transfer_account_id!r}"
                )
            if self.transfer_account_id == self.account_id:
                raise SchemaValidationError(
                    "transferAccountId cannot be the same as accountId"
                )
        if self.transfer_id is not None and not OWNER_ID_PATTERN.match(self.transfer_id):
            raise SchemaValidationError(
                f"Invalid transferId {self.transfer_id!r}; must be a valid UUID"
            )

    @property
    def signed_decimal_amount(self) -> float:
        sign = -1.0 if self.record_type == int(RecordType.EXPENSE) else 1.0
        return sign * (self.amount / 100.0)

    def to_couch_dict(self) -> dict[str, Any]:
        """Serialize to the exact JSON payload expected by Wallet's CouchDB and PouchDB."""
        doc: dict[str, Any] = dict(self.extra_preserved_fields)
        doc.update(
            {
                "_id": self.doc_id,
                "note": self.note,
                "latitude": float(self.latitude),
                "longitude": float(self.longitude),
                "accuracy": int(self.accuracy),
                "type": int(self.record_type),
                "photos": list(self.photos),
                "paymentType": int(self.payment_type),
                "reservedOwnerId": self.reserved_owner_id,
                "reservedAuthorId": self.reserved_author_id,
                "reservedSource": self.reserved_source,
                "reservedModelType": self.reserved_model_type,
                "reservedCreatedAt": self.reserved_created_at,
                "reservedUpdatedAt": self.reserved_updated_at,
                "recordDate": self.record_date,
                "recordState": int(self.record_state),
                "warrantyInMonth": int(self.warranty_in_month),
                "currencyId": self.currency_id,
                "accountId": self.account_id,
                "categoryId": self.category_id,
                "amount": int(self.amount),
                "refAmount": int(self.ref_amount),
                "suggestedEnvelopeId": int(self.suggested_envelope_id),
                "labels": list(self.labels),
                "transfer": bool(self.transfer),
                "categoryChanged": bool(self.category_changed),
                "categoryConfirmReason": int(self.category_confirm_reason),
                "refObjects": list(self.ref_objects),
            }
        )
        if self.payee:
            doc["payee"] = self.payee
        elif "payee" in doc:
            doc["payee"] = ""
        if self.transfer_id:
            doc["transferId"] = self.transfer_id
        if self.transfer_account_id:
            doc["transferAccountId"] = self.transfer_account_id
        if self.rev:
            doc["_rev"] = self.rev
        return doc

    @classmethod
    def from_couch_dict(cls, data: dict[str, Any]) -> Self:
        """Parse an existing CouchDB Record document while preserving any extra fields."""
        known_keys = {
            "_id",
            "_rev",
            "note",
            "payee",
            "latitude",
            "longitude",
            "accuracy",
            "type",
            "photos",
            "paymentType",
            "reservedOwnerId",
            "reservedAuthorId",
            "reservedSource",
            "reservedModelType",
            "reservedCreatedAt",
            "reservedUpdatedAt",
            "recordDate",
            "recordState",
            "warrantyInMonth",
            "currencyId",
            "accountId",
            "categoryId",
            "amount",
            "refAmount",
            "suggestedEnvelopeId",
            "labels",
            "transfer",
            "transferId",
            "transferAccountId",
            "categoryChanged",
            "categoryConfirmReason",
            "refObjects",
        }
        extras = {k: v for k, v in data.items() if k not in known_keys}
        raw_amount = int(round(float(data.get("amount", 0))))
        raw_ref = int(round(float(data.get("refAmount", raw_amount))))
        created_at = str(data.get("reservedCreatedAt") or data.get("recordDate") or "")
        updated_at = str(data.get("reservedUpdatedAt") or created_at)
        return cls(
            doc_id=str(data.get("_id", "")),
            rev=data.get("_rev"),
            account_id=str(data.get("accountId", "")),
            category_id=str(data.get("categoryId", "")),
            currency_id=str(data.get("currencyId", "")),
            amount=abs(raw_amount) if raw_amount != 0 else 1,
            ref_amount=abs(raw_ref) if raw_ref != 0 else 1,
            record_type=int(data.get("type", 1)),
            payment_type=int(data.get("paymentType", 0)),
            record_state=int(data.get("recordState", 1)),
            record_date=str(data.get("recordDate", "")),
            reserved_owner_id=str(data.get("reservedOwnerId", "")),
            reserved_author_id=str(data.get("reservedAuthorId", "")),
            reserved_created_at=created_at,
            reserved_updated_at=updated_at,
            reserved_source=str(data.get("reservedSource", "web")),
            reserved_model_type=str(data.get("reservedModelType", "Record")),
            note=str(data.get("note", "") or ""),
            payee=str(data.get("payee", "") or ""),
            labels=tuple(str(x) for x in (data.get("labels") or [])),
            transfer=bool(data.get("transfer", False)),
            transfer_id=data.get("transferId"),
            transfer_account_id=data.get("transferAccountId"),
            category_changed=bool(data.get("categoryChanged", True)),
            category_confirm_reason=int(data.get("categoryConfirmReason", 4)),
            suggested_envelope_id=int(data.get("suggestedEnvelopeId", 0)),
            warranty_in_month=int(data.get("warrantyInMonth", 0)),
            latitude=float(data.get("latitude", 0.0) or 0.0),
            longitude=float(data.get("longitude", 0.0) or 0.0),
            accuracy=int(data.get("accuracy", 0) or 0),
            photos=tuple(data.get("photos") or []),
            ref_objects=tuple(data.get("refObjects") or []),
            extra_preserved_fields=extras,
        )
