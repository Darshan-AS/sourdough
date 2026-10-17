"""Sourdough: Local SDK and `dough` CLI for Wallet by BudgetBakers."""

from sourdough.config import ReplicationConfig, StateManager, WalletPaths, resolve_paths
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
    AuthError,
    BankConnectedAccountError,
    ConfigError,
    ConflictError,
    CouchDBError,
    EntityNotFoundError,
    SchemaValidationError,
    WalletError,
)
from sourdough.infra.couchdb import CouchDBClient
from sourdough.services.records import WalletService

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "AccountDocument",
    "AmbiguousEntityError",
    "AuthError",
    "BankConnectedAccountError",
    "CategoryDocument",
    "ConfigError",
    "ConflictError",
    "CouchDBClient",
    "CouchDBError",
    "CurrencyDocument",
    "EntityNotFoundError",
    "LabelDocument",
    "PaymentType",
    "RecordDocument",
    "RecordState",
    "RecordType",
    "ReplicationConfig",
    "SchemaValidationError",
    "StateManager",
    "WalletError",
    "WalletPaths",
    "WalletService",
    "resolve_paths",
]
