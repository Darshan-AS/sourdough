"""CouchDB HTTP client and BudgetBakers session refresh client."""

import base64
from collections.abc import Callable
import json
import os
import ssl
import subprocess
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from sourdough.config import ReplicationConfig
from sourdough.exceptions import (
    AuthError,
    ConflictError,
    CouchDBError,
    EntityNotFoundError,
)

CURL_BIN = "/usr/bin/curl"

type HttpCallable = Callable[
    [str, str, dict[str, str], bytes | None],
    tuple[int, dict[str, str], bytes],
]


def _create_tls_context() -> ssl.SSLContext:
    """Create a strict TLSv1.2+ SSLContext using certifi or system CA bundle."""
    cafile: str | None = None
    try:
        import certifi

        cafile = certifi.where()
    except ImportError:
        if os.path.exists("/etc/ssl/cert.pem"):
            cafile = "/etc/ssl/cert.pem"

    ctx = ssl.create_default_context(cafile=cafile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _curl_http_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Execute an HTTPS request via /usr/bin/curl (uses macOS SecureTransport/Keychain TLS verification)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise CouchDBError(f"Refusing non-HTTPS request to {url!r}")

    cmd: list[str] = [
        CURL_BIN,
        "-s",
        "-S",
        "-g",
        "--proto",
        "=https",
        "--tlsv1.2",
        "--max-time",
        "20",
        "-X",
        method,
        "-w",
        "\n__HTTP_STATUS__:%{http_code}",
    ]
    for k, v in headers.items():
        cmd.extend(["-H", f"{k}: {v}"])
    if body is not None:
        cmd.extend(["--data-binary", "@-"])
    cmd.append(url)

    proc = subprocess.run(
        cmd,
        input=body,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CouchDBError(
            f"Network error connecting to {parsed.netloc}: {err_msg or f'curl exit {proc.returncode}'}"
        )

    stdout = proc.stdout
    marker = b"\n__HTTP_STATUS__:"
    idx = stdout.rfind(marker)
    if idx == -1:
        raise CouchDBError("Malformed response from curl transport")
    resp_body = stdout[:idx]
    status_code = int(stdout[idx + len(marker) :].decode("ascii").strip())
    return status_code, {}, resp_body


def default_http_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Execute an HTTPS request using urllib with strict TLS, falling back to /usr/bin/curl on macOS AIA cert gaps."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise CouchDBError(f"Refusing non-HTTPS request to {url!r}")

    req = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    ctx = _create_tls_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            status = resp.getcode()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            data = resp.read()
            return status, resp_headers, data
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        err_body = exc.read() if exc.fp else b""
        return exc.code, resp_headers, err_body
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError) and os.path.exists(CURL_BIN):
            return _curl_http_transport(method, url, headers, body)
        raise CouchDBError(f"Network error connecting to {parsed.netloc}: {exc.reason}") from exc


class CouchDBClient:
    """Client for BudgetBakers per-user CouchDB instance."""

    def __init__(
        self,
        replication: ReplicationConfig,
        transport: HttpCallable | None = None,
    ) -> None:
        self.replication = replication
        self._transport = transport or default_http_transport
        creds = f"{replication.login}:{replication.token}".encode("utf-8")
        self._auth_header = "Basic " + base64.b64encode(creds).decode("ascii")

    def _request_json(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        payload: Any = None,
    ) -> Any:
        base = self.replication.database_url
        url = f"{base}/{path.lstrip('/')}" if path else base
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "User-Agent": "sourdough/0.1.0",
        }
        body_bytes: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(payload).encode("utf-8")

        status, _, raw = self._transport(method, url, headers, body_bytes)
        text = raw.decode("utf-8", errors="replace") if raw else ""
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise CouchDBError(
                f"Invalid JSON from CouchDB (HTTP {status}): {text[:200]}",
                status_code=status,
            ) from exc

        if status in (200, 201, 202):
            return parsed

        error_code = parsed.get("error", "") if isinstance(parsed, dict) else ""
        reason = parsed.get("reason", "") if isinstance(parsed, dict) else text[:200]

        if status in (401, 403):
            raise AuthError(
                f"CouchDB authentication failed (HTTP {status}: {error_code} - {reason}). "
                "Run 'dough auth refresh' to refresh replication credentials."
            )
        if status == 404:
            raise EntityNotFoundError(f"Document not found in CouchDB ({reason})")
        if status == 409:
            raise ConflictError(
                f"Document revision conflict (409): {reason}",
                status_code=409,
                reason=reason,
            )
        raise CouchDBError(
            f"CouchDB HTTP {status} ({error_code}): {reason}",
            status_code=status,
            reason=reason,
        )

    def db_info(self) -> dict[str, Any]:
        """Return database metadata (doc_count, update_seq, etc.)."""
        return self._request_json("GET", "")

    def get_doc(self, doc_id: str, rev: str | None = None) -> dict[str, Any]:
        """Fetch a single document by _id (and optional _rev)."""
        safe_id = urllib.parse.quote(doc_id, safe="")
        query = {"rev": rev} if rev else None
        return self._request_json("GET", safe_id, query=query)

    def put_doc(
        self,
        doc: dict[str, Any],
        retry_on_conflict: bool = False,
    ) -> dict[str, Any]:
        """
        Create or update a document via PUT /{db}/{_id}.
        If retry_on_conflict is True, fetches the latest _rev once on 409 and retries.
        """
        doc_id = str(doc.get("_id", ""))
        if not doc_id:
            raise CouchDBError("Document is missing '_id'")
        safe_id = urllib.parse.quote(doc_id, safe="")
        try:
            return self._request_json("PUT", safe_id, payload=doc)
        except ConflictError:
            if not retry_on_conflict:
                raise
            latest = self.get_doc(doc_id)
            updated_doc = dict(doc)
            updated_doc["_rev"] = latest["_rev"]
            return self._request_json("PUT", safe_id, payload=updated_doc)

    def delete_doc(self, doc_id: str, rev: str | None = None) -> dict[str, Any]:
        """Soft-delete a document in CouchDB via PUT with _deleted: true (matching PouchDB V0())."""
        current_rev = rev
        current_doc: dict[str, Any] = {}
        if not current_rev:
            current_doc = self.get_doc(doc_id)
            current_rev = str(current_doc["_rev"])
        else:
            try:
                current_doc = self.get_doc(doc_id, rev=current_rev)
            except EntityNotFoundError:
                current_doc = {"_id": doc_id, "_rev": current_rev}

        tombstone = dict(current_doc)
        tombstone["_id"] = doc_id
        tombstone["_rev"] = current_rev
        tombstone["_deleted"] = True
        safe_id = urllib.parse.quote(doc_id, safe="")
        return self._request_json("PUT", safe_id, payload=tombstone)

    def bulk_docs(self, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write multiple documents in a single request via POST /{db}/_bulk_docs."""
        result = self._request_json("POST", "_bulk_docs", payload={"docs": docs})
        if not isinstance(result, list):
            raise CouchDBError(f"Unexpected _bulk_docs response: {result!r}")
        errors = [item for item in result if isinstance(item, dict) and item.get("error")]
        if errors:
            raise CouchDBError(f"_bulk_docs reported errors: {errors}")
        return result

    def all_docs_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Fetch all non-deleted documents whose _id starts with `prefix`."""
        query = {
            "startkey": json.dumps(prefix),
            "endkey": json.dumps(f"{prefix}\ufff0"),
            "include_docs": "true",
        }
        resp = self._request_json("GET", "_all_docs", query=query)
        rows = resp.get("rows", []) if isinstance(resp, dict) else []
        docs: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict) and not row.get("value", {}).get("deleted"):
                doc = row.get("doc")
                if isinstance(doc, dict) and not doc.get("_deleted"):
                    docs.append(doc)
        return docs


def refresh_replication_from_session(
    session_token: str,
    transport: HttpCallable | None = None,
) -> tuple[ReplicationConfig, dict[str, Any]]:
    """
    Use the web app's sessionToken cookie to call user.getUser on web.budgetbakers.com
    and retrieve fresh CouchDB replication credentials.
    """
    if not session_token or not session_token.strip():
        raise AuthError("sessionToken is missing in state file; cannot refresh replication credentials")

    http_fn = transport or default_http_transport
    url = (
        "https://web.budgetbakers.com/api/trpc/user.getUser"
        "?batch=1&input=%7B%220%22%3A%7B%22json%22%3Anull%7D%7D"
    )
    cookie = (
        f"__Secure-next-auth.session-token={session_token}; "
        f"next-auth.session-token={session_token}"
    )
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "User-Agent": "sourdough/0.1.0",
    }
    status, _, raw = http_fn("GET", url, headers, None)
    text = raw.decode("utf-8", errors="replace") if raw else ""
    if status != 200:
        raise AuthError(
            f"Session refresh failed with HTTP {status}. Your sessionToken may have expired."
        )
    try:
        parsed = json.loads(text)
        user_data = parsed[0]["result"]["data"]["json"]
        rep_data = user_data["replication"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise AuthError(f"Unexpected response structure from user.getUser: {text[:200]}") from exc

    rep = ReplicationConfig(
        db_name=str(rep_data.get("dbName", "")),
        url=str(rep_data.get("url", "")),
        login=str(rep_data.get("login", "")),
        token=str(rep_data.get("token", "")),
        owner_id=str(rep_data.get("ownerId", "") or user_data.get("userId", "")),
    )
    return rep, user_data
