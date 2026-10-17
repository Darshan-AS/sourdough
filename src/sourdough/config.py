"""Configuration and state file management for sourdough."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sourdough.exceptions import ConfigError


@dataclass(frozen=True, slots=True)
class ReplicationConfig:
    """CouchDB replication credentials for a user's Wallet database."""

    db_name: str
    url: str
    login: str
    token: str
    owner_id: str

    def __post_init__(self) -> None:
        if not self.db_name or not self.db_name.startswith("bb-"):
            raise ConfigError(f"Invalid replication.dbName: {self.db_name!r}")
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConfigError(f"replication.url must be a valid HTTPS URL, got {self.url!r}")
        if not self.login or not self.token or not self.owner_id:
            raise ConfigError("replication credentials (login, token, ownerId) must be non-empty")

    @property
    def database_url(self) -> str:
        return f"{self.url.rstrip('/')}/{self.db_name}"


@dataclass(frozen=True, slots=True)
class WalletPaths:
    """Resolved file paths for state, metadata cache, and mutation journal."""

    state_file: Path
    cache_file: Path
    journal_file: Path


def _can_use_dir(directory: Path) -> bool:
    """Return True if directory exists and is writable, or can be created."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return os.access(str(directory), os.W_OK)
    except OSError:
        return False


def resolve_paths(explicit_state_file: str | None = None) -> WalletPaths:
    """Resolve state, cache, and journal paths using XDG defaults with workspace fallback."""
    env_state = explicit_state_file or os.environ.get("DOUGH_STATE_FILE") or os.environ.get("WALLET_STATE_FILE")
    cwd_state = Path.cwd() / ".wallet_state.json"

    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sourdough"
    xdg_cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "sourdough"
    xdg_data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "sourdough"

    if env_state:
        state_path = Path(env_state).expanduser().resolve()
    elif cwd_state.exists():
        state_path = cwd_state.resolve()
    else:
        state_path = (xdg_config_home / "state.json").resolve()

    env_cache = os.environ.get("DOUGH_CACHE_FILE") or os.environ.get("WALLET_CACHE_FILE")
    if env_cache:
        cache_path = Path(env_cache).expanduser().resolve()
    elif _can_use_dir(xdg_cache_home):
        cache_path = (xdg_cache_home / "metadata_cache.json").resolve()
    else:
        cache_path = (state_path.parent / ".wallet_cache.json").resolve()

    env_journal = os.environ.get("DOUGH_JOURNAL_FILE") or os.environ.get("WALLET_JOURNAL_FILE")
    if env_journal:
        journal_path = Path(env_journal).expanduser().resolve()
    elif _can_use_dir(xdg_data_home):
        journal_path = (xdg_data_home / "journal.jsonl").resolve()
    else:
        journal_path = (state_path.parent / ".wallet_journal.jsonl").resolve()

    return WalletPaths(
        state_file=state_path,
        cache_file=cache_path,
        journal_file=journal_path,
    )


def write_secure_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON with 0600 permissions so tokens are never world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    os.replace(str(tmp_path), str(path))


class StateManager:
    """Loads and updates .wallet_state.json safely."""

    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file

    def load_raw(self) -> dict[str, Any]:
        if not self.state_file.exists():
            raise ConfigError(
                f"Wallet state file not found at {self.state_file}. "
                "Provide --state-file or place .wallet_state.json in the working directory."
            )
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Malformed JSON in state file {self.state_file}: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(f"Expected JSON object in {self.state_file}")
        return data

    def load_replication(self) -> ReplicationConfig:
        raw = self.load_raw()
        rep = raw.get("replication")
        if not isinstance(rep, dict):
            raise ConfigError(f"Missing 'replication' object in {self.state_file}")
        return ReplicationConfig(
            db_name=str(rep.get("dbName", "")),
            url=str(rep.get("url", "")),
            login=str(rep.get("login", "")),
            token=str(rep.get("token", "")),
            owner_id=str(rep.get("ownerId", "") or raw.get("userId", "")),
        )

    def update_last_record_id(self, record_id: str) -> None:
        raw = self.load_raw()
        raw["lastRecordId"] = record_id
        write_secure_json(self.state_file, raw)

    def update_replication(self, rep: ReplicationConfig) -> None:
        raw = self.load_raw()
        raw["replication"] = {
            "dbName": rep.db_name,
            "url": rep.url,
            "login": rep.login,
            "token": rep.token,
            "ownerId": rep.owner_id,
        }
        write_secure_json(self.state_file, raw)
