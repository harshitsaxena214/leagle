"""
backend/core/auth.py

Clerk JWT verification for FastAPI.

Every protected endpoint gains a `Depends(get_current_user)` which:
  1. Reads the Bearer token from the Authorization header.
  2. Fetches / caches Clerk's JWKS (refreshed at most every 10 minutes).
  3. Decodes the JWT, verifying: algorithm, signature, exp, nbf, and issuer.
  4. Returns the decoded payload dict so endpoints can read user ID / org role.

For admin-only endpoints, add `Depends(require_admin)` in addition.

Required env vars (see .env.example):
  CLERK_JWKS_URL  — e.g. https://<your-clerk-domain>/.well-known/jwks.json
  CLERK_ISSUER    — e.g. https://<your-clerk-domain>
  CLERK_AUDIENCE  — optional; if set, the `azp` claim must match this value
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwk, jwt
from jose.exceptions import ExpiredSignatureError

from core.config import settings

logger = logging.getLogger(__name__)

# ── JWKS cache ────────────────────────────────────────────────────────────────
_JWKS_CACHE: dict[str, Any] = {}
_JWKS_FETCHED_AT: float = 0.0
_JWKS_TTL_SECONDS: int = 600  # 10 minutes


def _get_jwks() -> dict:
    """Return Clerk JWKS, fetching from the network at most once per TTL."""
    global _JWKS_CACHE, _JWKS_FETCHED_AT

    now = time.monotonic()
    if _JWKS_CACHE and (now - _JWKS_FETCHED_AT) < _JWKS_TTL_SECONDS:
        return _JWKS_CACHE

    url = settings.clerk_jwks_url
    logger.info(f"Fetching Clerk JWKS from {url}")
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        _JWKS_CACHE = resp.json()
        _JWKS_FETCHED_AT = now
        return _JWKS_CACHE
    except Exception as exc:
        logger.error(f"Failed to fetch Clerk JWKS: {exc}")
        if _JWKS_CACHE:
            # Return stale cache rather than crashing on a transient network error
            logger.warning("Returning stale JWKS cache after fetch failure")
            return _JWKS_CACHE
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service temporarily unavailable — cannot verify token",
        )


# ── Bearer scheme ─────────────────────────────────────────────────────────────
class SafeHTTPBearer(HTTPBearer):
    async def __call__(self, request: Request) -> HTTPAuthorizationCredentials | None:
        try:
            return await super().__call__(request)
        except HTTPException as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=e.detail,
                headers={"WWW-Authenticate": "Bearer"},
            )

_bearer = SafeHTTPBearer(auto_error=False)


def _find_key(kid: str | None, jwks: dict) -> dict | None:
    """Return the JWK whose `kid` matches the token header, or the first key."""
    keys = jwks.get("keys", [])
    if not keys:
        return None
    if kid:
        for k in keys:
            if k.get("kid") == kid:
                return k
    return keys[0]


# ── Core dependency ───────────────────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """
    FastAPI dependency — returns the decoded Clerk JWT payload.
    Raises HTTP 401 on any failure.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Decode header without verification to find the key id
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    kid = header.get("kid")
    alg = header.get("alg", "RS256")

    jwks = _get_jwks()
    key_data = _find_key(kid, jwks)
    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No matching signing key found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Build the public key object
    try:
        public_key = jwk.construct(key_data, algorithm=alg)
    except Exception as exc:
        logger.error(f"Failed to construct JWK: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signing key",
        )

    # Decode and verify the JWT
    options = {
        "verify_exp": True,
        "verify_nbf": True,
        "verify_iss": True,
        "verify_aud": False,  # We check azp manually below
    }

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[alg],
            issuer=settings.clerk_issuer,
            options=options,
        )
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError as exc:
        logger.warning(f"JWT verification failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Optional: verify azp (authorised party) against CLERK_AUDIENCE
    if settings.clerk_audience:
        azp = payload.get("azp", "")
        if azp != settings.clerk_audience:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token audience mismatch",
            )

    return payload


# ── Admin guard ───────────────────────────────────────────────────────────────
async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """
    Additional dependency that enforces Clerk org:admin role.
    The org_role claim is injected by Clerk when the user belongs to an
    organisation and the session has org context (org_role = 'org:admin').

    Add this as a second dependency on destructive endpoints:
        @router.post("/ingest", dependencies=[Depends(require_admin)])
    """
    org_role: str = user.get("org_role", "")
    if org_role != "org:admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return user
