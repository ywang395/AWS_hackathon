"""
shared/auth.py -- Wardline authentication helpers.

Implements the two pieces of the downtime login flow described in CLAUDE.md:

1. Secure-link tokens (mirrors the `Tokens` table: PK token, nurse_id,
   expires_at TTL) created by the alert agent when it emails a nurse a link
   (see `agents/alert_agent/app.py::build_secure_link`), and verified on
   every `POST /chat` request (CLAUDE.md section 7).
2. Hashed PIN verification (field `pin_hash` on the `Nurses` table) so a
   stolen link alone is never enough to open a nurse's chat -- the PIN is
   the second factor.

Non-negotiable (CLAUDE.md section 2): nurse_id always comes from a
*verified* token, server-side. Nothing in this module -- or anything that
calls it -- should ever trust a model- or client-supplied nurse_id without
first checking it against a token and a PIN hash.

This module is intentionally storage-agnostic so it is easy to extend
without touching callers elsewhere in the app:

    - `TokenStore`   -- Phase 1 uses `InMemoryTokenStore`; Phase 2 swaps in
                        a DynamoDB-backed store over the `Tokens` table
                        (via `shared/db.py`) behind the same interface.
    - `NurseLookup`  -- Phase 1 uses `JsonNurseLookup` (reads
                        `data/nurses.json`, same convention as
                        `tools/patients.py`); Phase 2 swaps in a lookup
                        backed by the `Nurses` table.

Both seams are passed in as optional keyword arguments, so production code
can do:

    authenticate(token, pin, store=dynamo_token_store, nurse_lookup=dynamo_nurse_lookup)

while tests and local scripts rely on the defaults below.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from backend.shared import db


# ---------------------------------------------------------------------------
# PIN hashing
# ---------------------------------------------------------------------------

_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERATIONS = 390_000  # OWASP 2023 guidance for PBKDF2-HMAC-SHA256
_SALT_BYTES = 16


def hash_pin(pin: str) -> str:
    """
    Hash a nurse's PIN for storage in `Nurses.pin_hash`.

    Uses PBKDF2-HMAC-SHA256 (stdlib `hashlib`, no extra dependency) with a
    fresh random salt per call. Output format is a single self-describing
    string:

        "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>"

    so `verify_pin` can re-derive the hash without any extra state, and the
    work factor (`_PBKDF2_ITERATIONS`) can be bumped later without
    invalidating hashes that were stored with the old value.
    """
    if not pin or not pin.isdigit():
        raise ValueError("PIN must be a non-empty numeric string")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, pin.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, pin_hash: str) -> bool:
    """
    Constant-time comparison of a submitted PIN against a stored hash
    produced by `hash_pin`. Returns False (never raises) on malformed
    input or a corrupt hash record, so a bad token/PIN pair always fails
    closed instead of 500-ing.
    """
    try:
        algo, iterations_s, salt_hex, hash_hex = pin_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    candidate = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, pin.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# Token store (seam #1 -- swap for a DynamoDB `Tokens` table in Phase 2)
# ---------------------------------------------------------------------------

DEFAULT_TOKEN_TTL_SECONDS = 12 * 60 * 60  # 12h comfortably covers a night shift


@dataclass
class TokenRecord:
    token: str
    nurse_id: str
    expires_at: float  # unix epoch seconds -- maps to the Tokens.expires_at TTL attr


class TokenStore(Protocol):
    """Storage interface for secure-link tokens (mirrors the `Tokens` table)."""

    def put(self, record: TokenRecord) -> None: ...

    def get(self, token: str) -> Optional[TokenRecord]: ...

    def delete(self, token: str) -> None: ...


class InMemoryTokenStore:
    """
    Default `TokenStore`, used in Phase 1 / unit tests / local dev. State
    is NOT shared across Lambda invocations -- Phase 2 must swap this for a
    DynamoDB-backed store (see `shared/db.py`) before relying on tokens
    surviving across requests in production.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, TokenRecord] = {}

    def put(self, record: TokenRecord) -> None:
        self._tokens[record.token] = record

    def get(self, token: str) -> Optional[TokenRecord]:
        return self._tokens.get(token)

    def delete(self, token: str) -> None:
        self._tokens.pop(token, None)


class DynamoTokenStore:
    """`Tokens` table (CLAUDE.md section 5): PK token, nurse_id, expires_at.

    expires_at is the table's TTL attribute, so it is stored as whole epoch
    seconds. DynamoDB deletes expired items lazily (up to ~48h late), which
    is why verify_token() still checks expiry itself.
    """

    def put(self, record: TokenRecord) -> None:
        db.put_item("tokens", {
            "token": record.token,
            "nurse_id": record.nurse_id,
            "expires_at": int(record.expires_at),
        })

    def get(self, token: str) -> Optional[TokenRecord]:
        item = db.get_item("tokens", token=token)
        if item is None:
            return None
        return TokenRecord(token=item["token"], nurse_id=item["nurse_id"], expires_at=float(item["expires_at"]))

    def delete(self, token: str) -> None:
        db.delete_item("tokens", token=token)


# Process-wide store for DATA_BACKEND=json, so tokens minted by the alert
# agent are visible to /chat in the same (single-process) server.
_memory_store = InMemoryTokenStore()


def default_token_store() -> TokenStore:
    """Store used when no `store=` is passed: DynamoDB or process memory (shared/db.py switch)."""
    return DynamoTokenStore() if db.use_dynamodb() else _memory_store


def create_token(
    nurse_id: str,
    *,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    store: Optional[TokenStore] = None,
) -> str:
    """
    Mint a new secure-link token for `nurse_id`. Called by the alert agent
    when it builds the email link (see
    `agents/alert_agent/app.py::build_secure_link`) -- a fresh token is
    minted per send/resend so an intercepted text can't be replayed
    indefinitely.
    """
    active_store = store or default_token_store()
    token = secrets.token_urlsafe(32)
    active_store.put(TokenRecord(token=token, nurse_id=nurse_id, expires_at=time.time() + ttl_seconds))
    return token


def verify_token(token: str, *, store: Optional[TokenStore] = None) -> Optional[str]:
    """
    Resolve a token to its owning `nurse_id`, or None if the token is
    missing or expired (expired tokens are evicted on read). This is the
    ONLY way `nurse_id` should ever enter the rest of the system -- never
    accept it as a raw request parameter from the client or the model.
    """
    active_store = store or default_token_store()
    record = active_store.get(token)
    if record is None:
        return None
    if record.expires_at < time.time():
        active_store.delete(token)
        return None
    return record.nurse_id


# ---------------------------------------------------------------------------
# Nurse lookup (seam #2 -- swap for a DynamoDB `Nurses` table in Phase 2)
# ---------------------------------------------------------------------------

_DEFAULT_NURSES_PATH = Path(__file__).resolve().parents[2] / "data" / "nurses.json"


class NurseLookup(Protocol):
    def get_pin_hash(self, nurse_id: str) -> Optional[str]: ...


class JsonNurseLookup:
    """
    Default `NurseLookup`: reads `data/nurses.json` directly, matching the
    convention used by `tools/patients.py`. Phase 2 should swap this for a
    lookup backed by `shared/db.py`'s `Nurses` table -- `authenticate()`
    below does not need to change when that happens.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _DEFAULT_NURSES_PATH

    def _load(self) -> list[dict]:
        with open(self._path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def get_pin_hash(self, nurse_id: str) -> Optional[str]:
        for nurse in self._load():
            if nurse.get("nurse_id") == nurse_id:
                return nurse.get("pin_hash")
        return None


class DynamoNurseLookup:
    """Reads pin_hash from the `Nurses` table (PK nurse_id)."""

    def get_pin_hash(self, nurse_id: str) -> Optional[str]:
        item = db.get_item("nurses", nurse_id=nurse_id)
        return item.get("pin_hash") if item else None


def default_nurse_lookup() -> NurseLookup:
    """Lookup used when no `nurse_lookup=` is passed: DynamoDB or data/nurses.json."""
    return DynamoNurseLookup() if db.use_dynamodb() else JsonNurseLookup()


class AuthError(Exception):
    """Raised by `authenticate()` when a token/PIN pair does not check out."""


def authenticate(
    token: str,
    pin: str,
    *,
    store: Optional[TokenStore] = None,
    nurse_lookup: Optional[NurseLookup] = None,
) -> str:
    """
    Full server-side auth check for `POST /chat` (CLAUDE.md section 7):
    resolves `token` -> `nurse_id`, then verifies `pin` against that
    nurse's stored hash. Returns the verified `nurse_id` on success.

    Raises `AuthError` on any failure (expired/unknown token, wrong PIN,
    unknown nurse). Callers should treat every `AuthError` the same way
    ("show the PIN screen again") and never leak *why* it failed to the
    client -- that distinction is only for server-side logs.
    """
    nurse_id = verify_token(token, store=store)
    if nurse_id is None:
        raise AuthError("invalid or expired token")

    lookup = nurse_lookup or default_nurse_lookup()
    pin_hash = lookup.get_pin_hash(nurse_id)
    if pin_hash is None:
        raise AuthError("unknown nurse")

    if not verify_pin(pin, pin_hash):
        raise AuthError("incorrect pin")

    return nurse_id