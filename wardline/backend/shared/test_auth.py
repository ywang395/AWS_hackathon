"""
Unit tests for `shared/auth.py`.

Covers both halves of the module described in its docstring: PIN hashing
(`hash_pin` / `verify_pin`) and the token lifecycle (`create_token` /
`verify_token` / `authenticate`), using the injectable `store=` /
`nurse_lookup=` seams so no real AWS access or file I/O is required.
"""

import time

import pytest

from backend.shared.auth import (
    AuthError,
    InMemoryTokenStore,
    authenticate,
    create_token,
    hash_pin,
    verify_pin,
    verify_token,
)


class FakeNurseLookup:
    """Minimal `NurseLookup` stand-in so tests don't touch data/nurses.json."""

    def __init__(self, pin_hashes: dict[str, str]) -> None:
        self._pin_hashes = pin_hashes

    def get_pin_hash(self, nurse_id: str):
        return self._pin_hashes.get(nurse_id)


# ---------------------------------------------------------------------------
# PIN hashing
# ---------------------------------------------------------------------------

def test_hash_pin_round_trip_succeeds_with_correct_pin():
    pin_hash = hash_pin("1234")
    assert verify_pin("1234", pin_hash) is True


def test_verify_pin_fails_with_wrong_pin():
    pin_hash = hash_pin("1234")
    assert verify_pin("9999", pin_hash) is False


def test_hash_pin_produces_different_salts_for_the_same_pin():
    # Same PIN, two different hashes -- confirms the salt is unique per call
    # (so storing the same PIN for multiple nurses doesn't leak a pattern).
    hash_a = hash_pin("1234")
    hash_b = hash_pin("1234")
    assert hash_a != hash_b
    assert verify_pin("1234", hash_a)
    assert verify_pin("1234", hash_b)


def test_hash_pin_rejects_non_numeric_pin():
    with pytest.raises(ValueError):
        hash_pin("abcd")


def test_hash_pin_rejects_empty_pin():
    with pytest.raises(ValueError):
        hash_pin("")


def test_verify_pin_fails_closed_on_corrupt_hash():
    assert verify_pin("1234", "not-a-real-hash") is False
    assert verify_pin("1234", "") is False


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------

def test_create_and_verify_token_round_trip():
    store = InMemoryTokenStore()
    token = create_token("N002", store=store)
    assert verify_token(token, store=store) == "N002"


def test_verify_token_returns_none_for_unknown_token():
    store = InMemoryTokenStore()
    assert verify_token("does-not-exist", store=store) is None


def test_verify_token_returns_none_once_expired():
    store = InMemoryTokenStore()
    token = create_token("N002", ttl_seconds=0, store=store)
    time.sleep(0.01)  # ensure we are past expiry
    assert verify_token(token, store=store) is None


def test_tokens_are_isolated_per_store():
    store_a = InMemoryTokenStore()
    store_b = InMemoryTokenStore()
    token = create_token("N002", store=store_a)
    assert verify_token(token, store=store_a) == "N002"
    assert verify_token(token, store=store_b) is None


# ---------------------------------------------------------------------------
# authenticate() -- the full POST /chat server-side check
# ---------------------------------------------------------------------------

def test_authenticate_succeeds_with_valid_token_and_pin():
    store = InMemoryTokenStore()
    nurse_lookup = FakeNurseLookup({"N002": hash_pin("1234")})
    token = create_token("N002", store=store)

    nurse_id = authenticate(token, "1234", store=store, nurse_lookup=nurse_lookup)

    assert nurse_id == "N002"


def test_authenticate_rejects_wrong_pin():
    store = InMemoryTokenStore()
    nurse_lookup = FakeNurseLookup({"N002": hash_pin("1234")})
    token = create_token("N002", store=store)

    with pytest.raises(AuthError):
        authenticate(token, "0000", store=store, nurse_lookup=nurse_lookup)


def test_authenticate_rejects_unknown_token():
    store = InMemoryTokenStore()
    nurse_lookup = FakeNurseLookup({"N002": hash_pin("1234")})

    with pytest.raises(AuthError):
        authenticate("bogus-token", "1234", store=store, nurse_lookup=nurse_lookup)


def test_authenticate_rejects_nurse_missing_from_lookup():
    store = InMemoryTokenStore()
    nurse_lookup = FakeNurseLookup({})  # nurse_id resolves from token, but has no pin_hash on file
    token = create_token("N999", store=store)

    with pytest.raises(AuthError):
        authenticate(token, "1234", store=store, nurse_lookup=nurse_lookup)