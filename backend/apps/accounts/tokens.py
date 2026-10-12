"""Signed JWT access/refresh tokens (HS256) using PyJWT directly."""
import time
import uuid

import jwt
from django.conf import settings

ALGORITHM = "HS256"


class TokenError(Exception):
    pass


def _encode(user, token_type, lifetime):
    now = int(time.time())
    payload = {
        "token_type": token_type,
        "user_id": str(user.id),
        "role": user.role,
        "tenant_id": str(user.tenant_id) if user.tenant_id else None,
        "tv": user.token_version,
        "iat": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.JWT_SIGNING_KEY, algorithm=ALGORITHM)


def issue_tokens(user):
    """Tokens carry role + tenant + token_version. They are signed, so the client cannot
    change them; the server still re-checks them against the (RLS-protected) user row on
    every request (see authentication.py)."""
    return {
        "access": _encode(user, "access", settings.JWT_ACCESS_LIFETIME_SECONDS),
        "refresh": _encode(user, "refresh", settings.JWT_REFRESH_LIFETIME_SECONDS),
    }


def decode_token(raw, expected_type):
    try:
        payload = jwt.decode(
            raw,
            settings.JWT_SIGNING_KEY,
            algorithms=[ALGORITHM],  # pinned: never trust the token's own "alg"
            options={"require": ["exp", "iat", "token_type", "user_id"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc))
    if payload.get("token_type") != expected_type:
        raise TokenError("wrong token type")
    return payload
