"""
Authentication utilities — JWT tokens, bcrypt password hashing, FastAPI dependency.

This module provides the core authentication primitives used across the application:
  - Password hashing and verification via bcrypt (through passlib)
  - JWT access token creation and decoding (through python-jose)
  - A FastAPI dependency (``get_current_user``) that extracts and validates
    the JWT from the Authorization header on every protected request

Security notes:
  - bcrypt is pinned to <4.1 in requirements.txt because passlib 1.7.4
    crashes with bcrypt 4.1+ (deprecated internal API usage)
  - JWT tokens use HS256 (symmetric) — the SECRET_KEY must be kept secret
  - Token lifetime defaults to 8 hours (one broadcast shift), configurable
    via MCS_TOKEN_EXPIRE_MIN

Future: Add LDAP/OAuth validation paths here alongside local auth.
"""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from backend.config import SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES
from backend.database import get_db
from backend.models import User

# Passlib context configured for bcrypt hashing. "deprecated=auto" means
# it will automatically re-hash passwords using outdated schemes on verify.
password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT signing algorithm — HS256 is symmetric (same key signs and verifies)
ALGORITHM = "HS256"

# FastAPI's OAuth2 password bearer scheme. The tokenUrl tells the Swagger UI
# where to send login requests; it doesn't affect application behavior.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(plain_password: str) -> str:
    """
    Hash a plaintext password using bcrypt.

    Args:
        plain_password: The user's plaintext password.

    Returns:
        A bcrypt hash string (e.g., ``$2b$12$...``).
    """
    return password_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plaintext password against a stored bcrypt hash.

    Uses constant-time comparison internally to prevent timing attacks.

    Args:
        plain_password: The password provided by the user at login.
        hashed_password: The bcrypt hash stored in the database.

    Returns:
        True if the password matches, False otherwise.
    """
    return password_context.verify(plain_password, hashed_password)


def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a signed JWT access token.

    The token payload contains:
      - ``sub``: The username (used to look up the user on each request)
      - ``exp``: Expiration timestamp

    Args:
        subject: The username to encode in the token's ``sub`` claim.
        expires_delta: Custom token lifetime. Defaults to
                       ACCESS_TOKEN_EXPIRE_MINUTES from config.

    Returns:
        An encoded JWT string.
    """
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return jwt.encode({"sub": subject, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    """
    Decode and validate a JWT access token.

    Checks the signature and expiration. If valid, returns the username
    from the ``sub`` claim. If invalid or expired, returns None.

    Args:
        token: The raw JWT string from the Authorization header.

    Returns:
        The username string if the token is valid, or None.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        # Covers expired tokens, invalid signatures, malformed JWTs
        return None


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency that authenticates the current request.

    Extracts the JWT from the ``Authorization: Bearer <token>`` header,
    decodes it, looks up the user in the database, and verifies the
    account is active. Raises HTTP 401 if any step fails.

    This is injected into route handlers via FastAPI's dependency system::

        @router.get("/protected")
        def protected_route(user: User = Depends(get_current_user)):
            ...

    Args:
        token: The JWT extracted from the Authorization header by OAuth2PasswordBearer.
        db: The database session provided by the get_db dependency.

    Returns:
        The authenticated User ORM object.

    Raises:
        HTTPException: 401 Unauthorized if the token is invalid, expired,
                       or the user doesn't exist / is deactivated.
    """
    username = decode_access_token(token)
    if username is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    user = db.query(User).filter(User.username == username).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User not found or inactive",
                            headers={"WWW-Authenticate": "Bearer"})
    return user
