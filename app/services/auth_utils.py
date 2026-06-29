import time
from typing import Optional

from fastapi import HTTPException, Request
from jose import JWTError, jwt

from auth import ALGORITHM, AUTH_COOKIE_NAME, SECRET_KEY


def _decode_request_payload(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except (JWTError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _extract_request_token(
    authorization: Optional[str],
    access_token_cookie: Optional[str] = None,
) -> Optional[str]:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
        return None
    if access_token_cookie:
        return access_token_cookie
    return None


def _resolve_runtime_user_id_from_request(request: Request) -> Optional[int]:
    token = _extract_request_token(
        request.headers.get("authorization"),
        request.cookies.get(AUTH_COOKIE_NAME),
    )
    payload = _decode_request_payload(token)
    if not payload:
        return None
    try:
        return int(payload.get("sub"))
    except (TypeError, ValueError):
        return None


def _get_request_user_id(authorization: Optional[str]) -> Optional[int]:
    payload = _decode_request_payload(_extract_request_token(authorization))
    if not payload:
        return None
    try:
        return int(payload.get("sub"))
    except (TypeError, ValueError):
        return None


def _get_request_user_role(authorization: Optional[str]) -> str:
    payload = _decode_request_payload(_extract_request_token(authorization))
    if not payload:
        return ""
    return str(payload.get("role") or "")


def _task_elapsed_ms(started_at: Optional[float]) -> Optional[int]:
    if started_at is None:
        return None
    try:
        return int(max(0, (time.time() - float(started_at)) * 1000))
    except (TypeError, ValueError):
        return None
