"""Typed domain and infrastructure exceptions for sourdough."""


class WalletError(Exception):
    """Base exception for all sourdough errors."""


class ConfigError(WalletError):
    """Raised when configuration or state file is missing or invalid."""


class AuthError(WalletError):
    """Raised when authentication or session refresh fails."""


class SchemaValidationError(WalletError):
    """Raised when a document violates CouchDB / client SQLite schema invariants."""


class EntityNotFoundError(WalletError):
    """Raised when a referenced Account, Category, Currency, Label, or Record is not found."""


class AmbiguousEntityError(WalletError):
    """Raised when a name query matches multiple entities without an exact match."""


class BankConnectedAccountError(WalletError):
    """Raised when attempting an unsafe mutation on a bank-synchronized account."""


class CouchDBError(WalletError):
    """Raised on CouchDB HTTP or protocol errors."""

    def __init__(self, message: str, status_code: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class ConflictError(CouchDBError):
    """Raised on CouchDB 409 document revision conflict."""
