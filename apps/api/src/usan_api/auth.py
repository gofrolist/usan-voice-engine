import hmac
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from usan_api.settings import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)

# RFC 7235 §3.1: a 401 MUST carry a WWW-Authenticate challenge so standards-compliant
# clients and gateways know how to authenticate.
_WWW_AUTH = {"WWW-Authenticate": "Bearer"}


def require_operator_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate an operator on the management plane via a static bearer token.

    Guards human/back-office routes (elders, DNC, outbound enqueue/lookup). The
    presented token is compared to OPERATOR_API_KEY in constant time. The mismatch
    message is deliberately generic so it leaks nothing about why it failed.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=_WWW_AUTH,
        )
    if not hmac.compare_digest(
        credentials.credentials, settings.operator_api_key.get_secret_value()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid operator token",
            headers=_WWW_AUTH,
        )


def require_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Verify a service-to-service JWT (HS256). Returns the decoded claims.

    Used for agent→API calls. The token must be signed with JWT_SIGNING_KEY and
    carry `exp` and `call_id` claims. The caller is responsible for checking that
    the `call_id` claim matches the resource being mutated.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=_WWW_AUTH,
        )
    try:
        return jwt.decode(
            credentials.credentials,
            settings.jwt_signing_key.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp", "call_id"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
            headers=_WWW_AUTH,
        ) from exc


def require_worker_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Verify a worker JWT that is NOT yet scoped to a specific call.

    Inbound calls have no call_id until the API mints one, so the agent cannot
    present a call-scoped token to the inbound-create endpoint. This verifies the
    HS256 signature + exp only; it proves the caller holds JWT_SIGNING_KEY (our
    agent worker). Endpoints using it CREATE a resource rather than mutate a named
    one; for mutating an existing call, use require_service_token.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=_WWW_AUTH,
        )
    try:
        return jwt.decode(
            credentials.credentials,
            settings.jwt_signing_key.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
            headers=_WWW_AUTH,
        ) from exc
