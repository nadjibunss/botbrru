"""
src/utils/headers.py
Builds realistic Chrome 124 / Windows 10 request headers for Shopee API calls.
Every header is chosen to match what a real browser sends so that Shopee's
bot-detection heuristics see a plausible client fingerprint.
"""
from __future__ import annotations

import logging
import os
import binascii

from src.config import settings

logger = logging.getLogger(__name__)

# ── Static sec-ch-ua client-hint variants ────────────────────────────────────────
_BASE_SEC_CH: dict[str, str] = {
    "sec-ch-ua": settings.sec_ch_ua,
    "sec-ch-ua-mobile": settings.sec_ch_ua_mobile,
    "sec-ch-ua-platform": settings.sec_ch_ua_platform,
    "sec-ch-ua-platform-version": settings.sec_ch_ua_platform_version,
    # Low-entropy hints that Chrome sends unconditionally
    "sec-ch-ua-arch": '"x86"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-model": '""',
    "sec-ch-ua-wow64": "?0",
    # Full-version-list mirrors the sec-ch-ua value
    "sec-ch-ua-full-version-list": (
        '"Chromium";v="124.0.6367.201", '
        '"Google Chrome";v="124.0.6367.201", '
        '"Not-A.Brand";v="99.0.0.0"'
    ),
}


def _gen_trace_id() -> str:
    """Return a 32-character lowercase hex string suitable for X-Requestid."""
    return binascii.hexlify(os.urandom(16)).decode("ascii")


def build_search_headers(
    *,
    cookie: str,
    csrftoken: str,
    risktoken: str | None,
    referer: str,
) -> dict[str, str]:
    """
    Build a complete set of HTTP request headers that mimic Chrome 124 on
    Windows 10 for Shopee search API calls.

    Args:
        cookie:     Full SPC_EC or cookie string extracted from the browser.
        csrftoken:  Value extracted from the ``csrftoken`` cookie field.
        risktoken:  Optional SECSDK/risktoken string for anti-bot bypass.
        referer:    Full Referer URL for this specific request.

    Returns:
        An ordered dict of header name → value pairs.
    """
    headers: dict[str, str] = {
        "Host": "shopee.co.id",
        "Connection": "keep-alive",
        # Client hints — order matches Chrome wire order
        **_BASE_SEC_CH,
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": settings.user_agent,
        "Accept": "application/json",
        # Per-request trace ID
        "X-Requestid": _gen_trace_id(),
        # Shopee-specific headers
        "X-Shopee-Language": settings.shopee_language,
        "X-Shopee-Client-Timezone": settings.shopee_timezone,
        "X-Shopee-Client-Timezoneoffset": settings.shopee_timezone_offset,
        "X-Shopee-Client-Version": settings.shopee_client_version,
        "X-Shopee-Client-Type": "pc",
        "Content-Type": "application/json",
        "Origin": "https://shopee.co.id",
        "Referer": referer,
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cookie": cookie,
        "x-csrftoken": csrftoken,
    }

    if risktoken:
        headers["x-sz-secsdk-token"] = risktoken
        logger.debug("SECSDK risktoken included in headers (len=%d)", len(risktoken))
    else:
        logger.debug("No risktoken supplied; omitting x-sz-secsdk-token")

    return headers


def build_profile_headers(
    *,
    cookie: str,
    csrftoken: str,
) -> dict[str, str]:
    """
    Build headers for the profile/session-validation endpoint.
    Identical to search headers but with a fixed profile Referer and no risktoken.

    Args:
        cookie:     Full cookie string.
        csrftoken:  Value extracted from the ``csrftoken`` cookie field.

    Returns:
        An ordered dict of header name → value pairs.
    """
    return build_search_headers(
        cookie=cookie,
        csrftoken=csrftoken,
        risktoken=None,
        referer="https://shopee.co.id/user/account/profile",
    )
