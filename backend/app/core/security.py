"""
Low-level security primitives: password hashing, JWT encode/decode, and
opaque-token generation/hashing (used for refresh + password-reset tokens).

Kept separate from `services/` because these are pure, stateless crypto
helpers with no DB/business logic dependency.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum

import jwt
from passlib.context import CryptContext

from app.core.config import settings

# bcrypt is deliberately used (not a faster hash) - the whole point of bcrypt
# is that it's slow, which makes offline brute-forcing of leaked hashes expensive.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _pwd_context.verify(plain_password, hashed_password)


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


def create_access_token(user_id: uuid.UUID) -> str:
    """Short-lived JWT used to authenticate API requests (`Authorization: Bearer <token>`)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": TokenType.ACCESS,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Raises jwt.PyJWTError (or subclasses) on invalid/expired tokens - caller handles it."""
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def generate_opaque_token() -> str:
    """
    Generates a cryptographically random, URL-safe token used as the RAW
    refresh token / password-reset token handed to the client. Only its hash
    is ever persisted server-side (see `hash_token`).
    """
    return secrets.token_urlsafe(64)


def hash_token(raw_token: str) -> str:
    """SHA-256 is fine here (not bcrypt): these tokens are already high-entropy
    random values, not low-entropy human passwords, so we don't need a slow
    KDF - we just need a fast, deterministic, irreversible lookup key."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
