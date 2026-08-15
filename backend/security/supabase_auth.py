"""Verified Supabase authentication for Calibration v2.

Production accepts only cryptographically verified JWTs.  Modern Supabase
projects use asymmetric signing keys exposed through JWKS.  A server-only
``SUPABASE_JWT_SECRET`` fallback is supported for legacy HS256 projects during
migration; the secret must never be exposed to the browser.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import logging
import os
import threading
import time
from typing import Any, Dict, FrozenSet, Optional
from urllib.parse import urlparse

import requests
from fastapi import HTTPException, Request, status
from jose import JWTError, jwt


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: Optional[str]
    claims: Dict[str, Any]
    roles: FrozenSet[str] = frozenset()


_JWKS_LOCK = threading.RLock()
_JWKS_CACHE: Dict[str, Any] = {"keys": [], "fetched_at": 0.0, "source": None}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _is_production() -> bool:
    value = str(os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "production"))).strip().lower()
    return value in {"production", "prod", "live"}


def _bearer_token(request: Request) -> str:
    value = str(request.headers.get("authorization") or "").strip()
    parts = value.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1].strip()


def _supabase_url() -> str:
    return str(os.getenv("SUPABASE_URL", "")).strip().rstrip("/")


def _jwks_url() -> str:
    explicit = str(os.getenv("SUPABASE_JWKS_URL", "")).strip().rstrip("/")
    if explicit:
        return explicit
    base = _supabase_url()
    return f"{base}/auth/v1/.well-known/jwks.json" if base else ""


def _issuer() -> Optional[str]:
    explicit = str(os.getenv("SUPABASE_JWT_ISSUER", "")).strip().rstrip("/")
    if explicit:
        return explicit

    base = _supabase_url()
    if base:
        return f"{base}/auth/v1"

    # Permit deployments that supplied only a correct JWKS URL.
    jwks_url = _jwks_url()
    marker = "/auth/v1/"
    if marker in jwks_url:
        return jwks_url.split(marker, 1)[0].rstrip("/") + "/auth/v1"
    return None


def _load_jwks(*, force: bool = False) -> Dict[str, Any]:
    url = _jwks_url()
    if not url:
        raise RuntimeError(
            "Supabase JWKS URL is not configured. Set SUPABASE_URL or SUPABASE_JWKS_URL."
        )
    if ".supabase.co" in url and "https://.supabase.co" in url:
        raise RuntimeError("SUPABASE_JWKS_URL is malformed; the project reference is missing")

    ttl_s = max(30, int(str(os.getenv("SUPABASE_JWKS_CACHE_SECONDS", "600")) or "600"))
    now = time.time()
    with _JWKS_LOCK:
        if (
            not force
            and _JWKS_CACHE.get("keys")
            and _JWKS_CACHE.get("source") == url
            and now - float(_JWKS_CACHE.get("fetched_at") or 0.0) < ttl_s
        ):
            return dict(_JWKS_CACHE)

        response = requests.get(url, timeout=8)
        response.raise_for_status()
        payload = response.json()
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list) or not keys:
            raise RuntimeError("Supabase JWKS endpoint returned no signing keys")

        _JWKS_CACHE.update({"keys": keys, "fetched_at": now, "source": url})
        return dict(_JWKS_CACHE)


def _decode_unverified_for_local_development(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            raise ValueError("token has fewer than two segments")
        segment = parts[1] + ("=" * ((4 - len(parts[1]) % 4) % 4))
        payload = json.loads(base64.urlsafe_b64decode(segment.encode("utf-8")).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("token payload is not an object")
        return payload
    except Exception as exc:  # pragma: no cover - defensive local-only path
        raise HTTPException(status_code=401, detail="Invalid development token") from exc


def _decode_options(audience: str, issuer: Optional[str]) -> Dict[str, bool]:
    return {
        "verify_aud": bool(audience),
        "verify_iss": bool(issuer),
        "verify_exp": True,
        "verify_nbf": True,
    }


def _decode_legacy_hmac(
    token: str,
    *,
    algorithm: str,
    audience: str,
    issuer: Optional[str],
) -> Dict[str, Any]:
    secret = str(os.getenv("SUPABASE_JWT_SECRET", "")).strip()
    if not secret:
        raise HTTPException(
            status_code=401,
            detail="Legacy HMAC token received but SUPABASE_JWT_SECRET is not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if algorithm not in {"HS256", "HS384", "HS512"}:
        raise HTTPException(status_code=401, detail="Unsupported HMAC JWT algorithm")
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            audience=audience or None,
            issuer=issuer,
            options=_decode_options(audience, issuer),
        )
    except JWTError as exc:
        logger.info("supabase_legacy_jwt_verification_failed")
        raise HTTPException(
            status_code=401,
            detail="Token verification failed",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def verify_token(token: str) -> Dict[str, Any]:
    allow_unverified = _env_bool("ALLOW_UNVERIFIED_JWT", default=False)
    if allow_unverified:
        if _is_production():
            raise RuntimeError("ALLOW_UNVERIFIED_JWT must never be enabled in production")
        return _decode_unverified_for_local_development(token)

    audience = str(os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")).strip() or "authenticated"
    issuer = _issuer()
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token header") from exc

    algorithm = str(header.get("alg") or "").strip().upper()
    if algorithm.startswith("HS"):
        return _decode_legacy_hmac(
            token,
            algorithm=algorithm,
            audience=audience,
            issuer=issuer,
        )

    jwks = _load_jwks()
    kid = str(header.get("kid") or "").strip()
    candidates = [key for key in jwks.get("keys", []) if not kid or key.get("kid") == kid]
    if not candidates and kid:
        candidates = [key for key in _load_jwks(force=True).get("keys", []) if key.get("kid") == kid]
    if not candidates:
        raise HTTPException(status_code=401, detail="No matching JWT signing key")

    last_error: Optional[Exception] = None
    for key in candidates:
        try:
            key_algorithm = str(key.get("alg") or algorithm or "RS256").upper()
            if key_algorithm.startswith("HS"):
                # Never treat remotely supplied JWKS material as an HMAC secret.
                continue
            return jwt.decode(
                token,
                key,
                algorithms=[key_algorithm],
                audience=audience or None,
                issuer=issuer,
                options=_decode_options(audience, issuer),
            )
        except Exception as exc:  # pragma: no cover - token/key failure variants
            last_error = exc

    logger.info(
        "supabase_jwt_verification_failed kid=%s alg=%s issuer_host=%s",
        kid or "none",
        algorithm or "unknown",
        urlparse(issuer or "").hostname or "none",
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token verification failed",
        headers={"WWW-Authenticate": "Bearer"},
    ) from last_error


def require_authenticated_user(request: Request) -> AuthenticatedUser:
    token = _bearer_token(request)
    claims = verify_token(token)
    user_id = str(claims.get("sub") or claims.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Token has no subject")

    app_metadata = claims.get("app_metadata") if isinstance(claims.get("app_metadata"), dict) else {}
    token_roles = app_metadata.get("roles") or claims.get("roles") or []
    if isinstance(token_roles, str):
        token_roles = [token_roles]
    normalized_roles = frozenset(
        str(role).strip().lower() for role in token_roles if str(role).strip()
    )

    return AuthenticatedUser(
        user_id=user_id,
        email=(str(claims.get("email")).strip() if claims.get("email") else None),
        claims=claims,
        roles=normalized_roles,
    )
