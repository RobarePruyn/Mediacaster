"""
OIDC service — handles OpenID Connect Authorization Code flow.

Supports any OIDC-compliant identity provider (Cognito, Azure AD, Okta,
Keycloak, etc.) via standard discovery document (.well-known/openid-configuration).

Flow:
  1. Frontend calls /api/auth/oidc/authorize → backend builds the authorization URL
  2. User is redirected to the IdP, authenticates, and is sent back with a code
  3. Frontend calls /api/auth/oidc/callback with the code → backend exchanges it
     for tokens, validates the ID token via JWKS, and returns user claims

State is stored in-memory with a 5-minute TTL (sufficient for auth code flow).
Discovery config is cached for 5 minutes to avoid hitting the IdP on every request.
"""

import time
import secrets
import logging
from typing import Optional

import httpx
from authlib.jose import jwt as jose_jwt, JsonWebKey

from backend import config

logger = logging.getLogger("oidc")

# In-memory caches
_discovery_cache: dict = {}  # {"config": {...}, "fetched_at": float}
_jwks_cache: dict = {}       # {"keys": [...], "fetched_at": float}
_state_store: dict = {}      # {state_string: {"created_at": float}}

# Cache TTLs in seconds
_DISCOVERY_TTL = 300  # 5 minutes
_JWKS_TTL = 300       # 5 minutes
_STATE_TTL = 300      # 5 minutes


def _cleanup_expired_states():
    """Remove expired state entries to prevent unbounded memory growth."""
    now = time.time()
    expired = [k for k, v in _state_store.items() if now - v["created_at"] > _STATE_TTL]
    for k in expired:
        del _state_store[k]


async def get_provider_config(discovery_url: str) -> dict:
    """Fetch and cache the OIDC discovery document.

    Args:
        discovery_url: The full URL to the provider's .well-known/openid-configuration.

    Returns:
        The parsed JSON discovery document.

    Raises:
        ValueError: If the discovery URL is empty or the fetch fails.
    """
    if not discovery_url:
        raise ValueError("OIDC discovery URL is not configured")

    now = time.time()
    # Return cached config if still fresh
    if (_discovery_cache.get("url") == discovery_url
            and now - _discovery_cache.get("fetched_at", 0) < _DISCOVERY_TTL):
        return _discovery_cache["config"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(discovery_url)
        resp.raise_for_status()
        provider_config = resp.json()

    # Validate required fields
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if field not in provider_config:
            raise ValueError(f"OIDC discovery document missing required field: {field}")

    _discovery_cache.update({
        "url": discovery_url,
        "config": provider_config,
        "fetched_at": now,
    })
    logger.info("OIDC discovery config fetched from %s", discovery_url)
    return provider_config


async def _get_jwks(jwks_uri: str) -> dict:
    """Fetch and cache the JWKS (JSON Web Key Set) from the provider.

    Args:
        jwks_uri: The JWKS endpoint URL from the discovery document.

    Returns:
        The parsed JWKS as a dict with a "keys" array.
    """
    now = time.time()
    if (_jwks_cache.get("uri") == jwks_uri
            and now - _jwks_cache.get("fetched_at", 0) < _JWKS_TTL):
        return _jwks_cache["keys"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        jwks = resp.json()

    _jwks_cache.update({
        "uri": jwks_uri,
        "keys": jwks,
        "fetched_at": now,
    })
    return jwks


async def build_authorization_url(redirect_uri: str) -> str:
    """Build the OIDC authorization URL that the frontend redirects to.

    Generates a random state parameter for CSRF protection and stores it
    in the in-memory state store with a 5-minute TTL.

    Args:
        redirect_uri: The callback URL the IdP will redirect back to after auth.

    Returns:
        The full authorization URL to redirect the user to.
    """
    provider = await get_provider_config(config.OIDC_DISCOVERY_URL)
    authorization_endpoint = provider["authorization_endpoint"]

    # Generate and store state for CSRF protection
    _cleanup_expired_states()
    state = secrets.token_urlsafe(32)
    _state_store[state] = {"created_at": time.time()}

    # Build the authorization URL with standard OIDC parameters
    params = httpx.QueryParams({
        "response_type": "code",
        "client_id": config.OIDC_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
    })
    return f"{authorization_endpoint}?{params}"


async def exchange_code(
    code: str, state: str, redirect_uri: str
) -> dict:
    """Exchange an authorization code for tokens and validate the ID token.

    Performs the OIDC token exchange, validates the ID token signature using
    the provider's JWKS, and returns the parsed claims.

    Args:
        code: The authorization code from the callback.
        state: The state parameter from the callback (must match a stored value).
        redirect_uri: The same redirect_uri used in the authorize request.

    Returns:
        A dict of ID token claims (sub, email, preferred_username, etc.).

    Raises:
        ValueError: If state is invalid/expired or token exchange fails.
    """
    # Validate state to prevent CSRF
    _cleanup_expired_states()
    if state not in _state_store:
        raise ValueError("Invalid or expired state parameter")
    del _state_store[state]  # One-time use

    provider = await get_provider_config(config.OIDC_DISCOVERY_URL)
    token_endpoint = provider["token_endpoint"]

    # Exchange the code for tokens
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": config.OIDC_CLIENT_ID,
                "client_secret": config.OIDC_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            logger.error("OIDC token exchange failed: %s %s",
                         token_resp.status_code, token_resp.text)
            raise ValueError(f"Token exchange failed: {token_resp.status_code}")
        token_data = token_resp.json()

    id_token = token_data.get("id_token")
    if not id_token:
        raise ValueError("No id_token in token response")

    # Validate the ID token signature using the provider's JWKS
    jwks = await _get_jwks(provider["jwks_uri"])
    claims = jose_jwt.decode(id_token, JsonWebKey.import_key_set(jwks))
    claims.validate()

    logger.info("OIDC login successful for sub=%s", claims.get("sub"))
    return dict(claims)


def derive_username(claims: dict) -> str:
    """Derive a username from OIDC claims.

    Tries preferred_username first, then the local part of the email,
    and falls back to the sub claim as a last resort.

    Args:
        claims: The validated ID token claims dict.

    Returns:
        A username string suitable for the User model.
    """
    # Try preferred_username first (Keycloak, some Azure AD configs)
    username = claims.get("preferred_username", "").strip()
    if username:
        return username

    # Fall back to email local part
    email = claims.get("email", "").strip()
    if email and "@" in email:
        return email.split("@")[0]

    # Last resort: use the sub claim
    return claims.get("sub", "oidc_user")
