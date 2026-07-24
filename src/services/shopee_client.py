"""
src/services/shopee_client.py
Async Shopee API client with anti-bot resilience.

Strategy overview
─────────────────
1. Realistic browser headers (see utils/headers.py).
2. Optional SECSDK risktoken forwarded verbatim from the user's browser.
3. Conservative error classification: distinguish session expiry, captcha,
   generic anti-bot, and rate-limiting so callers react appropriately.
4. Exponential-backoff retry on transient network errors and HTTP 429.
5. Optional residential proxy via PROXY_URL environment variable.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import settings
from src.services.fingerprint import RiskToken, parse_risktoken
from src.utils.headers import build_profile_headers, build_search_headers

logger = logging.getLogger(__name__)

# ── Shopee API endpoints ──────────────────────────────────────────────────
SHOPEE_SEARCH_URL = "https://shopee.co.id/api/v4/search/search_items"
SHOPEE_PROFILE_URL = "https://shopee.co.id/api/v4/account/get_profile"

# ── Custom exceptions ─────────────────────────────────────────────────────


class SessionExpired(Exception):
    """Cookie has been invalidated server-side (HTTP 401 or is_login=False)."""

    def __init__(self, reason: str = "Session expired") -> None:
        super().__init__(reason)
        self.reason = reason


class AntiBotBlocked(Exception):
    """Bot detection triggered but the session itself is still alive."""

    def __init__(self, reason: str = "Anti-bot block") -> None:
        super().__init__(reason)
        self.reason = reason


class RateLimited(Exception):
    """Shopee returned HTTP 429 — back off before retrying."""

    def __init__(self, reason: str = "Rate limited") -> None:
        super().__init__(reason)
        self.reason = reason


class CaptchaRequired(Exception):
    """Shopee is demanding a CAPTCHA / fresh fingerprint token."""

    def __init__(self, reason: str = "Captcha required") -> None:
        super().__init__(reason)
        self.reason = reason


# ── Domain model ────────────────────────────────────────────────────────


@dataclass(slots=True)
class ShopeeItem:
    """
    A single product listing returned by the Shopee search API.

    Attributes:
        item_id:  Shopee item identifier.
        shop_id:  Shopee shop identifier.
        name:     Product name / title.
        price:    Price in IDR (integer, e.g. 15000 means Rp 15.000).
        stock:    Available stock count.
        location: City / warehouse location string.
        url:      Direct Shopee product URL.
    """

    item_id: int
    shop_id: int
    name: str
    price: int
    stock: int
    location: str
    url: str

    @property
    def is_in_stock(self) -> bool:
        """Return True when at least one unit is available."""
        return self.stock > 0


# ── Internal helpers ──────────────────────────────────────────────────────

_CSRFTOKEN_RE = re.compile(r"(?:^|;)\s*csrftoken=([^;]+)")


def _extract_csrftoken(cookie: str) -> str:
    """
    Extract the ``csrftoken`` value from a raw Cookie header string.

    Returns an empty string when the token is absent so callers can still
    include the ``x-csrftoken`` header (Shopee ignores an empty value less
    aggressively than a missing header).
    """
    m = _CSRFTOKEN_RE.search(cookie)
    return m.group(1).strip() if m else ""


def _normalize_cookie(cookie: str, risktoken: str | None) -> str:
    """
    Ensure the cookie string contains a ``RiskSessionID`` field.

    Some Shopee endpoints expect the risktoken to appear both in the
    ``x-sz-secsdk-token`` header *and* in the Cookie header as
    ``RiskSessionID``.  If ``risktoken`` is provided and the field is not
    already present, it is appended.
    """
    if not risktoken:
        return cookie
    if "RiskSessionID=" in cookie:
        return cookie
    separator = "; " if cookie.rstrip() else ""
    return f"{cookie.rstrip()}{separator}RiskSessionID={risktoken}"


def _safe_str(s: Any) -> str:
    """
    Convert *s* to a str and strip lone UTF-16 surrogates.

    Python's ``str.encode('utf-8')`` raises ``UnicodeEncodeError`` on lone
    surrogates (U+D800–U+DFFF).  Encoding with ``'surrogatepass'`` and
    decoding with ``'replace'`` substitutes them with the replacement character
    so that Telegram's bot API never receives malformed UTF-8.
    """
    text = str(s) if not isinstance(s, str) else s
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


def _build_client() -> httpx.AsyncClient:
    """
    Construct a shared :class:`httpx.AsyncClient` with anti-bot-friendly settings.

    Key choices:
    * ``http2=False`` — HTTP/2 fingerprinting is a known bot signal; staying
      on HTTP/1.1 matches a common residential Chrome profile.
    * ``follow_redirects=False`` — unexpected redirects should surface as errors.
    * Proxy forwarded from ``settings.proxy_url`` when set.
    """
    proxy: str | None = settings.proxy_url or None
    return httpx.AsyncClient(
        timeout=settings.request_timeout,
        http2=False,
        follow_redirects=False,
        proxy=proxy,  # type: ignore[arg-type]
        headers={"User-Agent": settings.user_agent},
    )


async def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict, dict]:
    """
    Execute an HTTP request with bounded exponential-backoff retry.

    Retries on:
    * ``httpx.TransportError`` / ``httpx.TimeoutException`` (network layer).
    * HTTP 429 (rate-limited).

    Non-retriable responses are returned immediately regardless of status.

    Args:
        method:    HTTP method string (``"GET"`` or ``"POST"``).
        url:       Full request URL.
        headers:   Request headers dict.
        params:    Optional query parameters.
        json_body: Optional JSON-serialisable request body.

    Returns:
        A 3-tuple ``(status_code, body_dict, response_headers_dict)``.
        ``body_dict`` is empty when the response body is not valid JSON.
    """
    last_exc: Exception | None = None

    for attempt in range(1, settings.max_retries + 1):
        try:
            async with _build_client() as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )

            status = response.status_code
            resp_headers: dict = dict(response.headers)

            try:
                body: dict = response.json()
            except Exception:
                body = {}

            logger.debug(
                "_request_with_retry attempt=%d url=%s status=%d",
                attempt,
                url,
                status,
            )

            if status == 429:
                backoff = settings.retry_backoff_base ** attempt + random.uniform(0, 2)
                logger.warning(
                    "HTTP 429 on attempt %d/%d; sleeping %.1fs before retry",
                    attempt,
                    settings.max_retries,
                    backoff,
                )
                await asyncio.sleep(backoff)
                last_exc = RateLimited(f"HTTP 429 on attempt {attempt}")
                continue

            return status, body, resp_headers

        except (httpx.TransportError, httpx.TimeoutException) as exc:
            backoff = settings.retry_backoff_base ** attempt + random.uniform(0, 3)
            logger.warning(
                "Network error on attempt %d/%d (%s); sleeping %.1fs",
                attempt,
                settings.max_retries,
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)
            last_exc = exc

    logger.error(
        "_request_with_retry exhausted %d attempts for %s",
        settings.max_retries,
        url,
    )
    raise last_exc or RateLimited("All retry attempts exhausted")


def _classify_response(status: int, body: dict) -> None:
    """
    Inspect an API response and raise the appropriate exception when needed.

    Classification table
    ────────────────────
    200 + error == 0                              → OK (no exception)
    200 + error in {4, 40001, 40002}              → CaptchaRequired
    200 + "captcha" in error_msg                  → CaptchaRequired
    200 + error == 90309999                       → AntiBotBlocked
    200 + other non-zero error                    → AntiBotBlocked
    401                                           → SessionExpired
    403 + is_login == False                       → SessionExpired
    403 + is_login == True                        → AntiBotBlocked
    429                                           → RateLimited
    other                                         → AntiBotBlocked

    Args:
        status: HTTP response status code.
        body:   Parsed JSON response body (may be empty dict).

    Raises:
        SessionExpired, AntiBotBlocked, RateLimited, CaptchaRequired
    """
    error_code: int = body.get("error", 0)
    error_msg: str = str(body.get("error_msg") or body.get("message") or "").lower()
    is_login: bool | None = body.get("is_login")

    if status == 200:
        if error_code == 0:
            return  # success

        captcha_codes = {4, 40001, 40002}
        if error_code in captcha_codes or "captcha" in error_msg:
            raise CaptchaRequired(
                f"Shopee captcha required (error={error_code}, msg={error_msg!r})"
            )

        if error_code == 90309999:
            raise AntiBotBlocked(
                f"Anti-bot error 90309999 (msg={error_msg!r})"
            )

        raise AntiBotBlocked(
            f"Non-zero Shopee error (error={error_code}, msg={error_msg!r})"
        )

    if status == 401:
        raise SessionExpired("HTTP 401 — cookie invalidated")

    if status == 403:
        if is_login is False:
            raise SessionExpired("HTTP 403 + is_login=False — session invalidated")
        raise AntiBotBlocked(
            f"HTTP 403 + is_login={is_login!r} — anti-bot block (session alive)"
        )

    if status == 429:
        raise RateLimited("HTTP 429 — rate limited")

    raise AntiBotBlocked(
        f"Unexpected HTTP {status} — treating as anti-bot block"
    )


# ── Public API ──────────────────────────────────────────────────────────


async def validate_session(
    cookie: str,
    risktoken: str | None = None,
) -> dict:
    """
    Check whether the supplied cookie represents a live Shopee session.

    Hits ``/api/v4/account/get_profile`` and inspects the response.

    Args:
        cookie:    Raw cookie string from the user's browser.
        risktoken: Optional SECSDK token string.

    Returns:
        A dict with keys:
        - ``valid`` (bool)
        - ``username`` (str | None)
        - ``has_phone`` (bool | None)
        - ``requires_captcha`` (bool)
        - ``reason`` (str | None) — human-readable explanation on failure.
    """
    norm_cookie = _normalize_cookie(cookie, risktoken)
    csrftoken = _extract_csrftoken(norm_cookie)
    headers = build_profile_headers(cookie=norm_cookie, csrftoken=csrftoken)

    try:
        status, body, _ = await _request_with_retry(
            "GET",
            settings.session_validation_url or SHOPEE_PROFILE_URL,
            headers=headers,
        )
    except (httpx.TransportError, httpx.TimeoutException, RateLimited) as exc:
        logger.warning("validate_session network/rate error: %s", exc)
        return {
            "valid": False,
            "username": None,
            "has_phone": None,
            "requires_captcha": False,
            "reason": f"Network error: {exc}",
        }

    logger.debug("validate_session status=%d error=%s", status, body.get("error"))

    # ── Captcha check ─────────────────────────────────────────────────────────
    error_code: int = body.get("error", 0)
    error_msg: str = str(body.get("error_msg") or body.get("message") or "").lower()
    captcha_codes = {4, 40001, 40002}
    if error_code in captcha_codes or "captcha" in error_msg:
        return {
            "valid": False,
            "username": None,
            "has_phone": None,
            "requires_captcha": True,
            "reason": f"Shopee demands captcha (error={error_code})",
        }

    # ── Session-expired signals ─────────────────────────────────────────────
    if status == 401 or (status == 403 and body.get("is_login") is False):
        return {
            "valid": False,
            "username": None,
            "has_phone": None,
            "requires_captcha": False,
            "reason": f"Session expired (HTTP {status})",
        }

    # ── Anti-bot without session invalidation ───────────────────────────────
    if status == 403 or (status == 200 and error_code == 90309999):
        return {
            "valid": False,
            "username": None,
            "has_phone": None,
            "requires_captcha": False,
            "reason": f"Anti-bot block (HTTP {status}, error={error_code})",
        }

    # ── Success ─────────────────────────────────────────────────────────────
    if status == 200 and error_code == 0:
        profile: dict = (body.get("data") or {}).get("user_profile") or {}
        if not profile:
            profile = body.get("user_profile") or {}

        raw_username: str | None = profile.get("username") or profile.get("nickname")
        username: str | None = _safe_str(raw_username) if raw_username else None

        phone: str | None = profile.get("phone") or profile.get("phone_number")
        has_phone: bool = bool(phone)

        return {
            "valid": True,
            "username": username,
            "has_phone": has_phone,
            "requires_captcha": False,
            "reason": None,
        }

    # ── Unknown response ──────────────────────────────────────────────────────
    return {
        "valid": False,
        "username": None,
        "has_phone": None,
        "requires_captcha": False,
        "reason": f"Unexpected response (status={status}, error={error_code})",
    }


def _extract_items(body: dict) -> list:
    """
    Try multiple response shapes to locate the item list.

    Shopee's API response envelope has changed across versions.  We probe
    the most common structures in priority order.

    Args:
        body: Parsed JSON response body.

    Returns:
        A list of raw item dicts, possibly empty.
    """
    candidates = [
        (body.get("data") or {}).get("items"),
        body.get("items"),
        (body.get("result") or {}).get("items"),
        (body.get("result") or {}).get("item"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def _normalize_item(raw: dict) -> ShopeeItem | None:
    """
    Convert a raw Shopee item dict into a :class:`ShopeeItem`.

    Handles multiple field-name variants across different API versions.
    Price is stored by Shopee as (IDR × 100 000); values above 100 000 are
    divided to recover the IDR amount.

    Args:
        raw: A single raw item dict from the Shopee API.

    Returns:
        A :class:`ShopeeItem` on success, or ``None`` when required fields
        are absent / unparseable.
    """
    try:
        item_id: int = int(raw.get("itemid") or raw.get("item_id") or 0)
        shop_id: int = int(raw.get("shopid") or raw.get("shop_id") or 0)

        if not item_id or not shop_id:
            logger.debug("_normalize_item: missing item_id or shop_id in %r", list(raw.keys()))
            return None

        name: str = _safe_str(raw.get("name") or raw.get("title") or "")
        raw_price: int = int(raw.get("price") or raw.get("price_min") or 0)
        # Shopee encodes price as IDR * 100_000
        price: int = raw_price // 100_000 if raw_price > 100_000 else raw_price

        stock: int = int(raw.get("stock") or raw.get("total_stock") or 0)

        loc_obj: dict = raw.get("shop_location") or raw.get("warehouse_location") or {}
        if isinstance(loc_obj, dict):
            location: str = _safe_str(loc_obj.get("city") or loc_obj.get("region") or "")
        else:
            location = _safe_str(loc_obj)

        url = f"https://shopee.co.id/product/{shop_id}/{item_id}"

        return ShopeeItem(
            item_id=item_id,
            shop_id=shop_id,
            name=name,
            price=price,
            stock=stock,
            location=location,
            url=url,
        )
    except (TypeError, ValueError) as exc:
        logger.debug("_normalize_item failed: %s — raw keys: %s", exc, list(raw.keys()))
        return None


async def search_items(
    *,
    cookie: str,
    keyword: str,
    risktoken: str | None = None,
    newest: int = 0,
    limit: int = 30,
) -> list[ShopeeItem]:
    """
    Search Shopee for items matching *keyword* and return parsed results.

    Args:
        cookie:    Raw browser cookie string.
        keyword:   Search keyword.
        risktoken: Optional SECSDK token for anti-bot bypass.
        newest:    Pagination offset (0-based).
        limit:     Maximum number of results to return.

    Returns:
        A list of :class:`ShopeeItem` objects (may be empty).

    Raises:
        SessionExpired, CaptchaRequired, AntiBotBlocked, RateLimited
    """
    norm_cookie = _normalize_cookie(cookie, risktoken)
    csrftoken = _extract_csrftoken(norm_cookie)
    encoded_kw = urllib.parse.quote(keyword)
    referer = f"https://shopee.co.id/search?keyword={encoded_kw}"

    headers = build_search_headers(
        cookie=norm_cookie,
        csrftoken=csrftoken,
        risktoken=risktoken,
        referer=referer,
    )

    params: dict[str, Any] = {
        "keyword": keyword,
        "limit": limit,
        "newest": newest,
        "order": "asc",
        "page_type": "search",
        "scenario": "PAGE_GLOBAL_SEARCH",
        "version": "2",
        "by": "relevancy",
        "match_id": 0,
        "src": "search",
        "fs_only": 0,
    }

    status, body, _ = await _request_with_retry(
        "GET",
        SHOPEE_SEARCH_URL,
        headers=headers,
        params=params,
    )

    _classify_response(status, body)

    raw_items = _extract_items(body)
    items: list[ShopeeItem] = []
    for raw in raw_items:
        item = _normalize_item(raw)
        if item is not None:
            items.append(item)

    logger.info(
        "search_items keyword=%r returned %d/%d parsed items",
        keyword,
        len(items),
        len(raw_items),
    )
    return items


def parse_risktoken_input(text: str) -> RiskToken | None:
    """
    Re-export of :func:`fingerprint.parse_risktoken` for use in command handlers.

    Args:
        text: Raw text input from the Telegram user.

    Returns:
        A :class:`RiskToken` on success, ``None`` otherwise.
    """
    return parse_risktoken(text)
