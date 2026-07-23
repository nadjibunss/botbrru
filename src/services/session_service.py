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
    has_phone: bool | None = None
    requires_captcha: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_str(s: str | None) -> str | None:
    """Strip surrogate characters that cannot be UTF-8 encoded."""
    if s is None:
        return None
    return s.encode("utf-8", errors="ignore").decode("utf-8")


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


def _build_shopee_headers(cookie: str, risktoken: str | None = None) -> dict[str, str]:
    """Build headers required for Shopee API requests."""
    headers = {
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
    if risktoken:
        headers["x-sz-secsdk-token"] = risktoken
    return headers


async def validate_cookie(cookie: str, risktoken: str | None = None) -> SessionValidation:
    """
    Validate a cookie by hitting Shopee's get_profile endpoint.

    If risktoken is provided, it is sent as x-sz-secsdk-token header
    and appended as RiskSessionID to the cookie string.

    Does not automate login, password, or OTP.
    """
    if not is_cookie_shape_valid(cookie):
        return SessionValidation(False, reason="Format cookie tidak valid")

    # Append RiskSessionID to cookie if risktoken provided
    effective_cookie = cookie
    if risktoken:
        effective_cookie = f"{cookie}; RiskSessionID={risktoken}"

    headers = _build_shopee_headers(effective_cookie, risktoken=risktoken)

    # --- Primary validation: Shopee get_profile ---
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(SHOPEE_PROFILE_URL, headers=headers)
    except httpx.HTTPError:
        logger.exception("Shopee profile request failed")
        return SessionValidation(False, reason="Tidak bisa terhubung ke Shopee")

    username: str | None = None
    has_phone: bool | None = None

    if response.status_code == 200:
        try:
            body = response.json()
        except (ValueError, AttributeError, TypeError):
            body = {}

        # Detect captcha/fingerprint requirement
        error_code = body.get("error")
        if error_code in (4, 40001, 40002) or body.get("captcha_verification_required"):
            return SessionValidation(
                False,
                requires_captcha=True,
                reason="Fingerprint/CAPTCHA diperlukan",
            )

        data = body.get("data", {})
        user_profile = data.get("user_profile", {}) if isinstance(data, dict) else {}

        if user_profile:
            username = _safe_str(
                user_profile.get("username")
                or user_profile.get("nickname")
            )

            # Extract has_phone
            phone_value = user_profile.get("phone") or user_profile.get("phone_number")
            has_phone = bool(phone_value)

            # Check if is_login is explicitly false
            if body.get("is_login") is False:
                return SessionValidation(
                    False, reason="Cookie sudah tidak valid (not logged in)"
                )
        else:
            # No user_profile in response, check is_login
            if body.get("is_login") is False:
                return SessionValidation(
                    False, reason="Cookie sudah tidak valid (not logged in)"
                )

    elif response.status_code == 401:
        return SessionValidation(False, reason="Cookie sudah tidak valid atau expired")

    elif response.status_code == 403:
        try:
            body = response.json()
        except (ValueError, AttributeError, TypeError):
            body = {}

        # Detect captcha/fingerprint on 403
        error_code = body.get("error")
        if body.get("is_login") is True and error_code in (4, 40001, 40002):
            return SessionValidation(
                False,
                requires_captcha=True,
                reason="Fingerprint/CAPTCHA diperlukan",
            )

        if body.get("is_login") is True:
            # Anti-bot block, cookie is still valid
            username = None
        else:
            return SessionValidation(
                False, reason="Cookie sudah tidak valid atau expired"
            )

    else:
        return SessionValidation(
            False,
            reason=f"Shopee mengembalikan HTTP {response.status_code}",
        )

    return SessionValidation(True, account_username=username, has_phone=has_phone)
