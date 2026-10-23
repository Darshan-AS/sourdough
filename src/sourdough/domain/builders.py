"""Safe factory builders for Record and paired Transfer documents."""

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import uuid

from sourdough.domain.enums import (
    CategoryConfirmReason,
    PaymentType,
    RecordState,
    RecordType,
)
from sourdough.domain.models import (
    AccountDocument,
    CategoryDocument,
    CurrencyDocument,
    RecordDocument,
    validate_iso8601_utc,
)
from sourdough.exceptions import SchemaValidationError

type NumericAmount = Decimal | int | float | str


def utc_now_iso() -> str:
    """Return current UTC timestamp formatted as YYYY-MM-DDTHH:MM:SS.sssZ."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_timestamp_input(value: str | None) -> str:
    """Normalize a user-provided ISO date or datetime string into UTC ISO-8601 format."""
    if not value:
        return utc_now_iso()
    raw = value.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        raw = f"{raw}T00:00:00.000Z"
    elif " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T", 1)
        if not (raw.endswith("Z") or "+" in raw[10:] or "-" in raw[10:]):
            raw += "Z"
    elif not (raw.endswith("Z") or "+" in raw[10:] or "-" in raw[10:]):
        raw += "Z"
    return validate_iso8601_utc(raw, "recordDate")


def decimal_to_minor_units(value: NumericAmount) -> int:
    """
    Convert a major-unit currency amount (e.g. 1000 or '1000.50') to positive minor units (paise/cents).

    Rejects booleans, zero, negative numbers, NaN/Infinity, or values with >2 decimal places.
    """
    if isinstance(value, bool):
        raise SchemaValidationError("Amount cannot be a boolean")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SchemaValidationError(f"Invalid numeric amount {value!r}: {exc}") from exc

    if not dec.is_finite():
        raise SchemaValidationError(f"Amount must be finite, got {value!r}")
    if dec <= 0:
        raise SchemaValidationError(
            f"Amount must be strictly positive (direction is set via type=expense|income), got {value!r}"
        )

    scaled = (dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if (dec * Decimal("100")) != scaled:
        raise SchemaValidationError(
            f"Amount {value!r} has more than 2 decimal places"
        )
    minor = int(scaled)
    if minor <= 0:
        raise SchemaValidationError(f"Amount {value!r} rounds to 0 minor units")
    return minor


def compute_ref_amount(amount_minor: int, currency: CurrencyDocument) -> int:
    """
    Compute refAmount in referential currency minor units matching Wallet web app's formula:
    refAmount = round(amount / ratioToReferential).
    """
    if currency.referential or currency.ratio_to_referential in (1.0, 0.0):
        return amount_minor
    ratio = Decimal(str(currency.ratio_to_referential))
    ref = (Decimal(amount_minor) / ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(1, int(ref))


def build_record(
    *,
    owner_id: str,
    account: AccountDocument,
    currency: CurrencyDocument,
    category: CategoryDocument,
    amount_major: NumericAmount,
    ref_amount_major: NumericAmount | None = None,
    record_type: RecordType = RecordType.EXPENSE,
    payment_type: PaymentType = PaymentType.CASH,
    record_state: RecordState = RecordState.CLEARED,
    note: str = "",
    payee: str = "",
    record_date: str | None = None,
    labels: Iterable[str] | None = None,
    record_id: str | None = None,
) -> RecordDocument:
    """Construct a validated RecordDocument (supporting both account-currency and multi-currency records)."""
    if category.envelope_id == 8002 and record_type != RecordType.EXPENSE:
        raise SchemaValidationError(
            "Category 'Loan and Debts' (envelopeId=8002) is only valid for EXPENSE records (Me -> Person); "
            "use 'Lending, renting' (envelopeId=10005) for INCOME repayments."
        )
    if category.envelope_id == 10005 and record_type != RecordType.INCOME:
        raise SchemaValidationError(
            "Category 'Lending, renting' (envelopeId=10005) is only valid for INCOME records (Person -> Me); "
            "use 'Loan and Debts' (envelopeId=8002) for EXPENSE lending."
        )
    now_iso = utc_now_iso()
    norm_date = normalize_timestamp_input(record_date) if record_date else now_iso
    doc_id = record_id or f"Record_{uuid.uuid4()}"
    amount_minor = decimal_to_minor_units(amount_major)
    ref_minor = (
        decimal_to_minor_units(ref_amount_major)
        if ref_amount_major is not None
        else compute_ref_amount(amount_minor, currency)
    )

    return RecordDocument(
        doc_id=doc_id,
        account_id=account.doc_id,
        category_id=category.doc_id,
        currency_id=currency.doc_id,
        amount=amount_minor,
        ref_amount=ref_minor,
        record_type=int(record_type),
        payment_type=int(payment_type),
        record_state=int(record_state),
        record_date=norm_date,
        reserved_owner_id=owner_id,
        reserved_author_id=owner_id,
        reserved_created_at=now_iso,
        reserved_updated_at=now_iso,
        reserved_source="web",
        reserved_model_type="Record",
        note=note,
        payee=payee,
        labels=tuple(labels or ()),
        transfer=False,
        category_changed=True,
        category_confirm_reason=int(CategoryConfirmReason.MANUAL_RECORD),
    )


def build_transfer_pair(
    *,
    owner_id: str,
    from_account: AccountDocument,
    from_currency: CurrencyDocument,
    to_account: AccountDocument,
    to_currency: CurrencyDocument,
    transfer_category: CategoryDocument,
    from_amount_major: NumericAmount,
    to_amount_major: NumericAmount | None = None,
    payment_type: PaymentType = PaymentType.BANK_TRANSFER,
    record_state: RecordState = RecordState.CLEARED,
    note: str = "",
    record_date: str | None = None,
) -> tuple[RecordDocument, RecordDocument]:
    """
    Construct an atomic pair of linked transfer RecordDocuments matching module 88437 in Wallet Web:
    - Outflow record (EXPENSE) on from_account with transferAccountId = to_account.doc_id
    - Inflow record (INCOME) on to_account with transferAccountId = from_account.doc_id
    - Shared transferId UUID
    """
    if from_account.doc_id == to_account.doc_id:
        raise SchemaValidationError("Cannot create a transfer to the same account")

    now_iso = utc_now_iso()
    norm_date = normalize_timestamp_input(record_date) if record_date else now_iso
    shared_transfer_id = str(uuid.uuid4())

    from_minor = decimal_to_minor_units(from_amount_major)
    from_ref = compute_ref_amount(from_minor, from_currency)

    if to_amount_major is not None:
        to_minor = decimal_to_minor_units(to_amount_major)
    elif from_currency.doc_id == to_currency.doc_id:
        to_minor = from_minor
    else:
        ratio = Decimal(str(to_currency.ratio_to_referential))
        to_minor = max(
            1,
            int((Decimal(from_ref) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        )

    to_ref = compute_ref_amount(to_minor, to_currency)

    outflow_doc = RecordDocument(
        doc_id=f"Record_{uuid.uuid4()}",
        account_id=from_account.doc_id,
        category_id=transfer_category.doc_id,
        currency_id=from_currency.doc_id,
        amount=from_minor,
        ref_amount=from_ref,
        record_type=int(RecordType.EXPENSE),
        payment_type=int(payment_type),
        record_state=int(record_state),
        record_date=norm_date,
        reserved_owner_id=owner_id,
        reserved_author_id=owner_id,
        reserved_created_at=now_iso,
        reserved_updated_at=now_iso,
        reserved_source="web",
        reserved_model_type="Record",
        note=note,
        transfer=True,
        transfer_id=shared_transfer_id,
        transfer_account_id=to_account.doc_id,
        category_changed=True,
        category_confirm_reason=int(CategoryConfirmReason.MANUAL_RECORD),
    )

    inflow_doc = replace(
        outflow_doc,
        doc_id=f"Record_{uuid.uuid4()}",
        account_id=to_account.doc_id,
        currency_id=to_currency.doc_id,
        amount=to_minor,
        ref_amount=to_ref,
        record_type=int(RecordType.INCOME),
        transfer_account_id=from_account.doc_id,
    )

    return outflow_doc, inflow_doc
