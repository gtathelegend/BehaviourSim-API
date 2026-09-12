"""Tests for API key security primitives."""

import re
from app.core.security import generate_api_key, hash_api_key, verify_api_key


def test_generate_api_key_format_and_entropy():
    """Verify generated API keys conform to expected prefix, length, and entropy."""
    raw_key, key_prefix, key_hash = generate_api_key(prefix="bs_live_")

    assert raw_key.startswith("bs_live_")
    assert key_prefix == raw_key[:16]
    assert len(key_prefix) == 16
    assert len(key_hash) == 64  # SHA-256 hex string

    # Check total length: "bs_live_" (8 chars) + 32 bytes urlsafe (~43 chars) >= 51 chars
    assert len(raw_key) >= 50
    # Ensure generated key contains urlsafe characters
    assert re.match(r"^bs_live_[A-Za-z0-9_-]+$", raw_key) is not None


def test_generate_api_key_uniqueness():
    """Verify consecutive generated keys are unique."""
    keys = {generate_api_key()[0] for _ in range(50)}
    assert len(keys) == 50


def test_hash_api_key_one_way_and_deterministic():
    """Verify hash is deterministic and does not leak raw secret."""
    raw_key = "bs_live_test_secret_value_12345"
    hash1 = hash_api_key(raw_key)
    hash2 = hash_api_key(raw_key)

    assert hash1 == hash2
    assert raw_key not in hash1
    assert len(hash1) == 64


def test_verify_api_key_valid_and_invalid():
    """Verify verification succeeds with correct key and fails with wrong key."""
    raw_key, _, key_hash = generate_api_key()

    assert verify_api_key(raw_key, key_hash) is True
    assert verify_api_key(raw_key + "tampered", key_hash) is False
    assert verify_api_key("bs_live_wrong_secret", key_hash) is False
    assert verify_api_key("", key_hash) is False
