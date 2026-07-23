"""Safe session-cookie validation service."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionValidation:
    valid: bool
    account_username: str | None = None
    reason: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_cookie_shape_valid(cookie: str) -> bool:
    """Perform minimal local cookie-shape validation."""
    return bool(cookie and "=" in cookie and len(cookie) >= 10)


async def validate_cookie(cookie: str) -> SessionValidation:
    """
    Validate a cookie only through a configured authorized endpoint.

    If SESSION_VALIDATION_URL is unset, only local structure validation runs.
    Does not automate login, password, or OTP.
    """
    if not is_cookie_shape_valid(cookie):
        return SessionValidation(False, reason="Format cookie tidak valid")

    url = settings.session_validation_url.strip()
    if not url:
        return SessionValidation(
            True,
            account_username=None,
            reason="Cookie disimpan; endpoint validasi belum dikonfigurasi",
        )

    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "User-Agent": "ShopeeMonitor/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        logger.exception("Session validation request failed")
        return SessionValidation(False, reason="Endpoint validasi tidak dapat diakses")

    if response.status_code in (401, 403):
        return SessionValidation(False, reason="Cookie sudah tidak valid atau expired")

    if response.status_code != 200:
        return SessionValidation(
            False,
            reason=f"Endpoint validasi mengembalikan HTTP {response.status_code}",
        )

    username: str | None = None

    try:
        body = response.json()
        data = body.get("data", body)
        username = (
            data.get("username")
            or data.get("user_name")
            or data.get("nickname")
        )
    except (ValueError, AttributeError, TypeError):
        pass

    return SessionValidation(True, account_username=username)
