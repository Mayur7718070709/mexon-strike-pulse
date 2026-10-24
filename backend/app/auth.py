import asyncio

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)


async def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    settings = get_settings()
    if not settings.supabase_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Supabase Auth is not configured")
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        return await decode_supabase_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired Supabase session") from exc
    except (httpx.RequestError, PyJWKClientError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Supabase Auth validation is unavailable") from exc


async def require_admin(user: dict = Depends(require_user)) -> dict:
    """Admin authorization comes only from trusted Supabase app_metadata claims."""
    app_metadata = user.get("app_metadata") or {}
    if not isinstance(app_metadata, dict) or app_metadata.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
    return user


async def decode_supabase_token(token: str) -> dict:
    settings = get_settings()
    if not settings.supabase_url:
        raise InvalidTokenError("Supabase Auth is not configured")
    issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1"
    algorithm = jwt.get_unverified_header(token).get("alg")
    if algorithm == "HS256":
        if not settings.supabase_anon_key:
            raise InvalidTokenError("Supabase anon key is required to validate legacy tokens")
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{issuer}/user",
                headers={"apikey": settings.supabase_anon_key, "Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 500:
            raise httpx.RequestError("Supabase Auth server returned an error")
        if response.status_code != 200:
            raise InvalidTokenError("Supabase rejected the session")
        user = response.json()
        if not user.get("id"):
            raise InvalidTokenError("Supabase session has no user ID")
        # Supabase already validated this token through /user. Retain its
        # expiry claim so long-lived sockets can close when the session expires.
        expiry = jwt.decode(token, options={"verify_signature": False}).get("exp")
        return {"id": user["id"], "email": user.get("email"), "app_metadata": user.get("app_metadata", {}),
                "expires_at": expiry}
    else:
        jwks = PyJWKClient(f"{issuer}/.well-known/jwks.json")
        signing_key = await asyncio.to_thread(jwks.get_signing_key_from_jwt, token)
        claims = jwt.decode(token, signing_key.key, algorithms=["ES256", "RS256", "EdDSA"], audience="authenticated", issuer=issuer)
    if not claims.get("sub"):
        raise InvalidTokenError("Token has no subject")
    return {
        "id": claims["sub"],
        "email": claims.get("email"),
        "expires_at": claims.get("exp"),
        # app_metadata is server-managed in Supabase; user_metadata is not trusted for authorization.
        "app_metadata": claims.get("app_metadata", {}),
    }
