"""
JWT validation and RBAC for the EKAP Retrieval Service.
Validates Keycloak-issued tokens; degrades gracefully when REQUIRE_AUTH=false.
"""
import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException, Request
from jose import JWTError, jwt as jose_jwt

KEYCLOAK_URL    = os.getenv("KEYCLOAK_URL", "http://keycloak:8080/auth")
KEYCLOAK_REALM  = os.getenv("KEYCLOAK_REALM", "ekap")
REQUIRE_AUTH    = os.getenv("REQUIRE_AUTH", "false").lower() == "true"

# Classification levels each role grants access to
_ROLE_CLASSIFICATIONS: dict[str, frozenset[str]] = {
    "system-administrator": frozenset(["Public", "Internal", "Confidential", "Restricted"]),
    "administrator":        frozenset(["Public", "Internal", "Confidential", "Restricted"]),
    "knowledge-manager":    frozenset(["Public", "Internal", "Confidential", "Restricted"]),
    "power-user":           frozenset(["Public", "Internal", "Confidential"]),
    "user":                 frozenset(["Public", "Internal"]),
}
_DEFAULT_CLASSIFICATIONS = frozenset(["Public", "Internal"])


@dataclass
class UserContext:
    user_id:  str
    username: str
    roles:    list[str] = field(default_factory=list)
    groups:   list[str] = field(default_factory=list)

    @property
    def accessible_classifications(self) -> frozenset[str]:
        result: set[str] = set()
        for role in self.roles:
            result |= _ROLE_CLASSIFICATIONS.get(role, set())
        return frozenset(result) if result else _DEFAULT_CLASSIFICATIONS

    def has_role(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    def can_manage_documents(self) -> bool:
        return self.has_role("knowledge-manager", "administrator", "system-administrator")


# In dev mode (no auth), grant full admin access so all endpoints are reachable without a token
ANONYMOUS = UserContext(
    user_id="anonymous",
    username="anonymous",
    roles=[] if REQUIRE_AUTH else ["administrator"],
)

# ── JWKS cache (refreshed every 5 minutes) ────────────────────────────────────

_jwks_cache: dict = {"keys": None, "expires": 0.0}


async def _get_jwks() -> dict | None:
    if time.monotonic() < _jwks_cache["expires"] and _jwks_cache["keys"]:
        return _jwks_cache["keys"]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"
            )
            resp.raise_for_status()
            _jwks_cache["keys"] = resp.json()
            _jwks_cache["expires"] = time.monotonic() + 300
    except Exception:
        pass  # use stale cache if available
    return _jwks_cache["keys"]


# ── Token validation ───────────────────────────────────────────────────────────

async def _validate_token(token: str) -> UserContext:
    jwks = await _get_jwks()
    if not jwks:
        raise HTTPException(503, "Auth service (Keycloak) unavailable.")
    try:
        claims = jose_jwt.decode(
            token, jwks, algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise HTTPException(401, f"Invalid token: {exc}") from exc

    return UserContext(
        user_id=claims.get("sub", "unknown"),
        username=claims.get("preferred_username", claims.get("sub", "unknown")),
        roles=claims.get("realm_access", {}).get("roles", []),
        groups=claims.get("groups", []),
    )


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def get_user_context(request: Request) -> UserContext:
    """
    FastAPI dependency that resolves the current user.

    Priority:
    1. Bearer token in Authorization header  → validated against Keycloak JWKS
    2. X-User-Id header (internal calls)     → trusted, anonymous role
    3. No credentials + REQUIRE_AUTH=false   → ANONYMOUS context (dev mode)
    4. No credentials + REQUIRE_AUTH=true    → 401
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return await _validate_token(auth_header[7:])

    # Internal service-to-service calls may pass X-User-Id without a JWT
    if uid := request.headers.get("X-User-Id"):
        return UserContext(user_id=uid, username=uid, roles=["user"])

    if REQUIRE_AUTH:
        raise HTTPException(401, "Authorization required. Provide a Bearer token.")

    return ANONYMOUS
