"""Safe session-cookie validation service."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

SHOPEE_PROFILE_URL = "https://shopee.co.id/api/v4/account/get_profile"


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


def _extract_csrftoken(cookie: str) -> str:
    """Extract csrftoken value from cookie string."""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("csrftoken="):
            return part.split("=", 1)[1]
    return ""


def _build_shopee_headers(cookie: str) -> dict[str, str]:
    """Build headers required for Shopee API requests."""
    return {
        "Cookie": cookie,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "id-ID,id;q=0.9",
        "x-shopee-language": "id",
        "x-api-source": "pc",
        "x-csrftoken": _extract_csrftoken(cookie),
        "Referer": "https://shopee.co.id",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def validate_cookie(cookie: str) -> SessionValidation:
    """
    Validate a cookie by hitting Shopee's get_profile endpoint.

    Always hits the profile endpoint to validate AND retrieve username.
    If SESSION_VALIDATION_URL is also configured, hits that as additional check.
    Does not automate login, password, or OTP.
    """
    if not is_cookie_shape_valid(cookie):
        return SessionValidation(False, reason="Format cookie tidak valid")

    headers = _build_shopee_headers(cookie)

    # --- Primary validation: Shopee get_profile ---
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(SHOPEE_PROFILE_URL, headers=headers)
    except httpx.HTTPError:
        logger.exception("Shopee profile request failed")
        return SessionValidation(False, reason="Tidak bisa terhubung ke Shopee")

    username: str | None = None

    if response.status_code == 200:
        try:
            body = response.json()
            data = body.get("data", {})
            user_profile = data.get("user_profile", {})

            if user_profile:
                username = (
                    user_profile.get("username")
                    or user_profile.get("nickname")
                )
                # Also check if is_login is explicitly false
                if body.get("is_login") is False:
                    return SessionValidation(
                        False, reason="Cookie sudah tidak valid (not logged in)"
                    )

                # Success: profile found
            else:
                # No user_profile in response, check is_login
                if body.get("is_login") is False:
                    return SessionValidation(
                        False, reason="Cookie sudah tidak valid (not logged in)"
                    )
        except (ValueError, AttributeError, TypeError):
            pass

    elif response.status_code == 401:
        return SessionValidation(False, reason="Cookie sudah tidak valid atau expired")

    elif response.status_code == 403:
        # 403 with is_login: true means anti-bot, not invalid session
        try:
            body = response.json()
            if body.get("is_login") is True:
                # Anti-bot block, cookie is still valid
                username = None  # Cannot retrieve username due to block
            else:
                return SessionValidation(
                    False, reason="Cookie sudah tidak valid atau expired"
                )
        except (ValueError, AttributeError, TypeError):
            # Cannot parse body, assume anti-bot
            pass

    else:
        return SessionValidation(
            False,
            reason=f"Shopee mengembalikan HTTP {response.status_code}",
        )

    # --- Optional additional validation via SESSION_VALIDATION_URL ---
    extra_url = settings.session_validation_url.strip()
    if extra_url:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                extra_response = await client.get(extra_url, headers=headers)

            if extra_response.status_code in (401, 403):
                return SessionValidation(
                    False, reason="Cookie tidak valid (validasi tambahan gagal)"
                )
        except httpx.HTTPError:
            logger.warning("Additional validation endpoint unreachable, skipping")

    return SessionValidation(True, account_username=username)
