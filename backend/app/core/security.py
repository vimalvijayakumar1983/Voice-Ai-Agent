import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

API_KEY_PREFIX = "vai"
API_KEY_SECRET_BYTES = 32
API_KEY_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    user_id: UUID, tenant_id: UUID, role: str, expires_delta: timedelta | None = None
) -> str:
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "role": role,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_refresh_token(
    user_id: UUID,
    tenant_id: UUID,
    *,
    jti: UUID,
    family_id: UUID,
    expires_at: datetime,
) -> str:
    """Create a refresh JWT tied to a persisted, one-time session record."""

    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "jti": str(jti),
        "family_id": str(family_id),
        "exp": expires_at,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None


def generate_api_key(api_key_id: UUID) -> str:
    """Generate a self-identifying API key whose secret is only shown once."""
    secret = secrets.token_urlsafe(API_KEY_SECRET_BYTES)
    return f"{API_KEY_PREFIX}_{api_key_id.hex}_{secret}"


def parse_api_key_id(api_key: str) -> UUID | None:
    """Extract the database identifier from a well-formed API key."""
    parts = api_key.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX:
        return None

    key_id, secret = parts[1:]
    if len(key_id) != 32 or not API_KEY_SECRET_PATTERN.fullmatch(secret):
        return None

    try:
        return UUID(hex=key_id)
    except ValueError:
        return None


def hash_api_key(api_key: str) -> str:
    """Return the deterministic digest persisted for an API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, stored_digest: str) -> bool:
    """Compare an API key digest without content-dependent timing."""
    return hmac.compare_digest(hash_api_key(api_key), stored_digest)
