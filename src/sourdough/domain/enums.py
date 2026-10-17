"""Enumerations and built-in category envelope mappings for Wallet by BudgetBakers."""

from enum import IntEnum
from typing import Dict


class RecordType(IntEnum):
    """Direction of a financial record (0 = Income, 1 = Expense)."""

    INCOME = 0
    EXPENSE = 1

    @classmethod
    def from_str(cls, val: str) -> "RecordType":
        normalized = val.strip().lower()
        if normalized in ("income", "in", "0", "+"):
            return cls.INCOME
        if normalized in ("expense", "expenses", "out", "1", "-"):
            return cls.EXPENSE
        raise ValueError(f"Invalid record type {val!r}; expected 'expense' or 'income'")


class PaymentType(IntEnum):
    """Wallet payment type integer codes."""

    CASH = 0
    DEBIT_CARD = 1
    CREDIT_CARD = 2
    BANK_TRANSFER = 3
    VOUCHER = 4
    MOBILE_PAYMENT = 5
    WEB_PAYMENT = 6

    @classmethod
    def from_str(cls, val: str) -> "PaymentType":
        mapping = {
            "cash": cls.CASH,
            "0": cls.CASH,
            "debit": cls.DEBIT_CARD,
            "debit_card": cls.DEBIT_CARD,
            "1": cls.DEBIT_CARD,
            "credit": cls.CREDIT_CARD,
            "credit_card": cls.CREDIT_CARD,
            "2": cls.CREDIT_CARD,
            "transfer": cls.BANK_TRANSFER,
            "bank_transfer": cls.BANK_TRANSFER,
            "3": cls.BANK_TRANSFER,
            "voucher": cls.VOUCHER,
            "4": cls.VOUCHER,
            "mobile": cls.MOBILE_PAYMENT,
            "mobile_payment": cls.MOBILE_PAYMENT,
            "upi": cls.MOBILE_PAYMENT,
            "5": cls.MOBILE_PAYMENT,
            "web": cls.WEB_PAYMENT,
            "web_payment": cls.WEB_PAYMENT,
            "6": cls.WEB_PAYMENT,
        }
        normalized = val.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized not in mapping:
            raise ValueError(
                f"Invalid payment type {val!r}; expected one of: "
                "cash, debit_card, credit_card, bank_transfer, voucher, mobile_payment, web_payment"
            )
        return mapping[normalized]


class RecordState(IntEnum):
    """Reconciliation state of a record."""

    RECONCILED = 0
    CLEARED = 1
    UNCLEARED = 2

    @classmethod
    def from_str(cls, val: str) -> "RecordState":
        mapping = {
            "reconciled": cls.RECONCILED,
            "0": cls.RECONCILED,
            "cleared": cls.CLEARED,
            "1": cls.CLEARED,
            "uncleared": cls.UNCLEARED,
            "2": cls.UNCLEARED,
        }
        normalized = val.strip().lower()
        if normalized not in mapping:
            raise ValueError(
                f"Invalid record state {val!r}; expected reconciled, cleared, or uncleared"
            )
        return mapping[normalized]


class CategoryConfirmReason(IntEnum):
    """Reason code for category confirmation in Wallet."""

    UNKNOWN = 0
    USER = 1
    AUTOMATIC = 2
    MANUAL_IMPORT = 3
    MANUAL_RECORD = 4


TRANSFER_ENVELOPE_ID = 20001
TRANSFER_SYSTEM_CATEGORY = 1

# Complete built-in envelopeId -> English display name mapping extracted from Wallet web bundle
ENVELOPE_NAMES: Dict[int, str] = {
    10: "Food & Drinks",
    20: "Shopping",
    30: "Housing",
    40: "Transportation",
    50: "Vehicle",
    60: "Life & Entertainment",
    70: "Communication, PC",
    80: "Financial expenses",
    90: "Investments",
    100: "Income",
    110: "Others",
    130: "Unknown",
    200: "System Categories",
    210: "Virtual Others",
    220: "Savings",
    230: "Reserves",
    1000: "Groceries",
    1001: "Restaurant, fast-food",
    1002: "Bar, cafe",
    1003: "Food & Drinks",
    2000: "Clothes & Footwear",
    2001: "Jewels, accessories",
    2002: "Health and beauty",
    2003: "Kids",
    2004: "Home, garden",
    2005: "Pets, animals",
    2006: "Electronics, accessories",
    2007: "Gifts, joy",
    2008: "Stationery, tools",
    2009: "Leisure time",
    2010: "Shopping",
    2011: "Drug-store, chemist",
    3000: "Rent",
    3001: "Mortgage",
    3002: "Energy, utilities",
    3003: "Services",
    3004: "Maintenance, repairs",
    3005: "Housing",
    3010: "Property insurance",
    4000: "Public transport",
    4001: "Taxi",
    4002: "Long distance",
    4003: "Business trips",
    4004: "Transportation",
    5000: "Fuel",
    5001: "Parking",
    5002: "Vehicle maintenance",
    5003: "Rentals",
    5004: "Vehicle",
    5010: "Vehicle insurance",
    6000: "Health care, doctor",
    6001: "Wellness, beauty",
    6002: "Active sport, fitness",
    6003: "Culture, sport events",
    6004: "Life events",
    6005: "Hobbies",
    6006: "Education, development",
    6007: "Books, audio, subscriptions",
    6008: "TV, Streaming",
    6009: "Holiday, trips, hotels",
    6010: "Charity, gifts",
    6011: "Alcohol, tobacco",
    6012: "Lottery, gambling",
    6013: "Life & Entertainment",
    7001: "Telephony, mobile phone",
    7002: "Internet",
    7003: "Software, apps, games",
    7004: "Postal services",
    7005: "Communication, PC",
    8000: "Taxes",
    8001: "Insurances",
    8002: "Loans, interests",
    8003: "Fines",
    8004: "Advisory",
    8005: "Charges, Fees",
    8006: "Leasing",
    8007: "Child Support",
    8008: "Financial expenses",
    9000: "Realty",
    9001: "Vehicles, chattels",
    9002: "Financial investments",
    9003: "Savings",
    9004: "Collections",
    9005: "Investments",
    10000: "Wage, invoices",
    10001: "Interests, dividends",
    10002: "Sale",
    10003: "Rental income",
    10004: "Dues & grants",
    10005: "Lending, renting",
    10006: "Checks, coupons",
    10007: "Lottery, gambling",
    10008: "Refunds (tax, purchase)",
    10009: "Child Support",
    10010: "Gifts",
    10011: "Income",
    11000: "Others",
    11001: "Missing",
    13000: "Unknown income",
    13001: "Unknown expense",
    20000: "Debt",
    20001: "Transfer, withdraw",
    20002: "Shopping list",
    20003: "Unknown",
    20004: "Wallet - OneTap record",
    20005: "Automatic bank statements reading",
}

