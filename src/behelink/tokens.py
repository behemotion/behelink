"""Token generation and hashing.

Unsalted SHA-256 over a server-generated CSPRNG secret — same pattern as
behetask API keys; safe because the input is already high-entropy.
"""

import hashlib
import hmac
import secrets
import string

_ALPHABET = string.ascii_letters + string.digits
_BODY_LEN = 43


def generate_token(prefix: str) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_BODY_LEN))
    return f"{prefix}_{body}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)
