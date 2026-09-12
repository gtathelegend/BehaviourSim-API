"""Cryptographic security primitives for API-key generation, hashing, and timing-safe verification."""

import hashlib
import hmac
import secrets
from typing import Tuple


def generate_api_key(prefix: str = "bs_live_") -> Tuple[str, str, str]:
    """Generate a high-entropy API key, non-secret prefix, and cryptographic hash.

    Key Structure:
    - Raw Key: `{prefix}{secret}` where secret has 256 bits (32 bytes) of cryptographic randomness.
    - Prefix: First 16 characters of the key (e.g. `bs_live_` + 8 chars) for indexing/logging.
    - Hash: SHA-256 digest of the raw key.

    Rationale for SHA-256 vs Password KDFs (bcrypt/argon2):
    Unlike human passwords (which possess low entropy and require slow memory-hard KDFs
    to impede dictionary attacks), generated API keys contain 256 bits of true cryptographic
    randomness ($2^{256}$ keyspace). Offline brute-force attacks are computationally infeasible.
    SHA-256 combined with constant-time verification (`hmac.compare_digest`) provides
    rigorous security while avoiding high-latency CPU bottlenecks on high-throughput API paths.

    Returns:
        Tuple of (raw_key, key_prefix, key_hash)
    """
    # 32 random bytes urlsafe-encoded provides ~43 base64 characters of high entropy
    secret = secrets.token_urlsafe(32)
    raw_key = f"{prefix}{secret}"
    key_prefix = raw_key[:16]
    key_hash = hash_api_key(raw_key)
    return raw_key, key_prefix, key_hash


def hash_api_key(raw_key: str) -> str:
    """Compute SHA-256 cryptographic hex digest of an API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def verify_api_key(raw_key: str, stored_hash: str) -> bool:
    """Verify an API key against a stored hash using constant-time comparison.

    Uses `hmac.compare_digest` to prevent side-channel timing attacks.
    """
    candidate_hash = hash_api_key(raw_key)
    return hmac.compare_digest(candidate_hash, stored_hash)
