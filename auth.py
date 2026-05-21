"""
Aurelius — auth.py
Google OAuth2 + JWT authentication.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

log = logging.getLogger("aurelius.auth")

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
JWT_SECRET           = os.environ.get("JWT_SECRET", "aurelius-dev-secret-change-in-prod")
JWT_ALGORITHM        = "HS256"
JWT_EXPIRE_DAYS      = 30

bearer_scheme = HTTPBearer(auto_error=False)


# =============================================================================
#  JWT helpers
# =============================================================================

def create_jwt(user_id: str, email: str, name: str, avatar: str) -> str:
    from jose import jwt
    payload = {
        "sub":    user_id,
        "email":  email,
        "name":   name,
        "avatar": avatar,
        "exp":    datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> Optional[dict]:
    try:
        from jose import jwt, JWTError
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
) -> dict:
    """Dependency — returns user dict or raises 401."""
    if not credentials:
        raise HTTPException(401, detail="Not authenticated")
    payload = decode_jwt(credentials.credentials)
    if not payload:
        raise HTTPException(401, detail="Invalid or expired token")
    return payload


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
) -> Optional[dict]:
    """Dependency — returns user dict or None (for public endpoints)."""
    if not credentials:
        return None
    return decode_jwt(credentials.credentials)


# =============================================================================
#  Google token verification
# =============================================================================

async def verify_google_token(id_token: str) -> dict:
    """Verify Google ID token and return user info."""
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": id_token}
        )
        if r.status_code != 200:
            raise HTTPException(401, detail="Invalid Google token")
        data = r.json()

    if data.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(401, detail="Token audience mismatch")
    if data.get("email_verified") != "true":
        raise HTTPException(401, detail="Email not verified")

    return {
        "google_id": data["sub"],
        "email":     data["email"],
        "name":      data.get("name", data["email"].split("@")[0]),
        "avatar":    data.get("picture", ""),
    }
