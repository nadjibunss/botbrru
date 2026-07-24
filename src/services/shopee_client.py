"""
src/services/shopee_client.py
Async Shopee client with anti-bot resilience — web-scraping approach.

Strategy overview
─────────────────
1. Realistic browser headers mimicking a real HTML page fetch.
2. Optional SECSDK risktoken forwarded verbatim from the user's browser.
3. Parse __NEXT_DATA__ JSON embedded in the HTML response instead of
   hitting the internal API directly (which returns 403 anti-bot).
4. Conservative error classification: distinguish session expiry, captcha,
   generic anti-bot, and rate-limiting so callers react appropriately.
5. Exponential-backoff retry on transient network errors and HTTP 429.
6. Optional residential proxy via PROXY_URL environment variable.
"""
from __future__ import annotations

import asyncio
import json
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

# ── Shopee endpoints ──────────────────────────────────────────────────────
SHOPEE_SEARCH_PAGE_URL = "https://shopee.co.id/search"
SHOPEE_SHOP_DETAILS_URL = "https://shopee.co.id/shop/{shop_id}/details"
SHOPEE_PROFILE_URL = "https://shopee.co.id/api/v4/account/get_profile"

# ── Regex for __NEXT_DATA__ extraction ────────────────────────────────────
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

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
    A single product listing returned by Shopee search.

    Attributes:
        item_id:    Shopee item identifier.
        shop_id:    Shopee shop identifier.
        name:       Product name / title.
        price:      Price in IDR (integer, e.g. 15000 means Rp 15.000).
        stock:      Available stock count.
        location:   City / warehouse location string.
        shop_name:  Shop display name.
        url:        Direct Shopee product URL.
    """

    item_id: int
    shop_id: int
    name: str
    price: int
    stock: int
    location: str
    shop_name: str
    url: str

    @property
    def is_in_stock(self) -> bool:
        """Return True when at least one unit is available."""
        return self.stock > 0


# ── Internal helpers ──────────────────────────────────────────────────────

_CSRFTOKEN_RE = re.compile(r"(?:^|;)\s*csrftoken=([^;]+)")


def _extract_csrftoken(cookie: str) -> str:
    """Extract the ``csrftoken`` value from a raw Cookie header string."""
    m = _CSRFTOKEN_RE.search(cookie)
    return m.group(1).strip() if m else ""


def _normalize_cookie(cookie: str, risktoken: str | None) -> str:
    """Ensure the cookie string contains a ``RiskSessionID`` field."""
    if not risktoken:
        return cookie
    if "RiskSessionID=" in cookie:
        return cookie
    separator = "; " if cookie.rstrip() else ""
    return f"{cookie.rstrip()}{separator}RiskSessionID={risktoken}"


def _safe_str(s: Any) -> str:
    """Convert *s* to str and strip lone UTF-16 surrogates."""
    text = str(s) if not isinstance(s, str) else s
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


def _build_client() -> httpx.AsyncClient:
    """Construct a shared httpx.AsyncClient with anti-bot-friendly settings."""
    proxy: str | None = settings.proxy_url or None
    return httpx.AsyncClient(
        timeout=settings.request_timeout,
        http2=False,
        follow_redirects=True,
        proxy=proxy,  # type: ignore[arg-type]
        headers={"User-Agent": settings.user_agent},
    )


def _build_html_headers(
    cookie: str,
    csrftoken: str,
    risktoken: str | None = None,
    referer: str | None = None,
) -> dict[str, str]:
    """
    Build headers that mimic a real browser fetching an HTML page.

    Uses a subset of search headers but swaps JSON-specific ones for
    standard HTML Accept headers.
    """
    headers = build_search_headers(
        cookie=cookie,
        csrftoken=csrftoken,
        risktoken=risktoken,
        referer=referer or "https://shopee.co.id/",
    )

    # Override Accept to look like a standard page navigation
    headers["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    )
    headers["Upgrade-Insecure-Requests"] = "1"

    # Remove JSON-specific headers that would not appear in a page fetch
    headers.pop("Content-Type", None)
    headers.pop("content-type", None)
    headers.pop("X-Requested-With", None)
    headers.pop("x-requested-with", None)

    return headers


async def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    raw_response: bool = False,
) -> tuple[int, dict | str, dict]:
    """
    Execute an HTTP request with bounded exponential-backoff retry.

    Retries on:
    * ``httpx.TransportError`` / ``httpx.TimeoutException`` (network layer).
    * HTTP 429 (rate-limited).

    Args:
        method:       HTTP method string.
        url:          Full request URL.
        headers:      Request headers dict.
        params:       Optional query parameters.
        json_body:    Optional JSON-serialisable request body.
        raw_response: If True, return response text instead of parsed JSON.

    Returns:
        A 3-tuple ``(status_code, body, response_headers_dict)``.
        ``body`` is a dict (JSON) or str (HTML) depending on ``raw_response``.
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

            if raw_response:
                body: dict | str = response.text
            else:
                try:
                    body = response.json()
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
    """
    error_code: int = body.get("error", 0)
    error_msg: str = str(body.get("error_msg") or body.get("message") or "").lower()
    is_login: bool | None = body.get("is_login")

    if status == 200:
        if error_code == 0:
            return

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


def _classify_html_response(status: int, html: str) -> None:
    """
    Classify an HTML page response for common error signals.

    For HTML scraping responses we cannot parse Shopee JSON error codes,
    but we can detect common HTTP-level issues.
    """
    if status == 200:
        return

    if status == 401:
        raise SessionExpired("HTTP 401 — cookie invalidated")

    if status == 403:
        lower_html = html[:2000].lower() if html else ""
        if "captcha" in lower_html or "verify" in lower_html:
            raise CaptchaRequired(f"HTTP 403 with captcha/verify in response")
        raise AntiBotBlocked(f"HTTP 403 — anti-bot block on page fetch")

    if status == 429:
        raise RateLimited("HTTP 429 — rate limited")

    if 300 <= status < 400:
        # Redirects might indicate session issues
        raise AntiBotBlocked(f"HTTP {status} redirect — possible anti-bot")

    raise AntiBotBlocked(f"Unexpected HTTP {status} on page fetch")


def _parse_next_data(html: str) -> dict | None:
    """
    Extract and parse the __NEXT_DATA__ JSON blob from Shopee HTML.

    Returns the parsed dict, or None if not found.
    """
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse __NEXT_DATA__ JSON: %s", exc)
        return None


def _normalize_item(raw: dict) -> ShopeeItem | None:
    """
    Convert a raw item dict (from __NEXT_DATA__) into a ShopeeItem.

    The HTML-embedded structure may nest fields under ``item_basic`` or
    expose them directly. This function handles both cases.
    """
    try:
        # Try item_basic sub-object first (common in __NEXT_DATA__)
        item_data = raw.get("item_basic") or raw

        item_id: int = int(item_data.get("itemid") or item_data.get("item_id") or 0)
        shop_id: int = int(item_data.get("shopid") or item_data.get("shop_id") or 0)

        if not item_id or not shop_id:
            logger.debug(
                "_normalize_item: missing item_id or shop_id in %r",
                list(item_data.keys())[:10],
            )
            return None

        name: str = _safe_str(item_data.get("name") or item_data.get("title") or "")
        raw_price: int = int(item_data.get("price") or item_data.get("price_min") or 0)
        # Shopee encodes price as IDR * 100_000
        price: int = raw_price // 100_000 if raw_price > 100_000 else raw_price

        stock: int = int(item_data.get("stock") or item_data.get("total_stock") or 0)

        # Location: may be a string or a nested dict
        loc_obj = item_data.get("shop_location") or item_data.get("item_location") or ""
        if isinstance(loc_obj, dict):
            location: str = _safe_str(loc_obj.get("city") or loc_obj.get("region") or "")
        else:
            location = _safe_str(loc_obj)

        shop_name: str = _safe_str(
            item_data.get("shop_name")
            or raw.get("shop_name")
            or ""
        )

        url = f"https://shopee.co.id/product/{shop_id}/{item_id}"

        return ShopeeItem(
            item_id=item_id,
            shop_id=shop_id,
            name=name,
            price=price,
            stock=stock,
            location=location,
            shop_name=shop_name,
            url=url,
        )
    except (TypeError, ValueError) as exc:
        logger.debug("_normalize_item failed: %s — raw keys: %s", exc, list(raw.keys())[:10])
        return None


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

    if status == 401 or (status == 403 and body.get("is_login") is False):
        return {
            "valid": False,
            "username": None,
            "has_phone": None,
            "requires_captcha": False,
            "reason": f"Session expired (HTTP {status})",
        }

    if status == 403 or (status == 200 and error_code == 90309999):
        return {
            "valid": False,
            "username": None,
            "has_phone": None,
            "requires_captcha": False,
            "reason": f"Anti-bot block (HTTP {status}, error={error_code})",
        }

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

    return {
        "valid": False,
        "username": None,
        "has_phone": None,
        "requires_captcha": False,
        "reason": f"Unexpected response (status={status}, error={error_code})",
    }


async def search_items(
    *,
    cookie: str,
    keyword: str,
    risktoken: str | None = None,
    newest: int = 0,
    limit: int = 30,
    fe_filter_options: list[dict[str, Any]] | None = None,
) -> list[ShopeeItem]:
    """
    Search Shopee for items matching *keyword* via HTML page scraping.

    Fetches the search results page as HTML, parses the embedded
    ``__NEXT_DATA__`` JSON, and extracts product items from it.

    Args:
        cookie:             Raw browser cookie string.
        keyword:            Search keyword.
        risktoken:          Optional SECSDK token for anti-bot bypass.
        newest:             Pagination offset (0-based).
        limit:              Maximum number of results to return.
        fe_filter_options:  Optional filter list for location/shop type.
            Example: [{"group_name":"SHOP_TYPE","values":["OFFICIAL_MALL"]},
                      {"group_name":"LOCATIONS","values":["Jabodetabek"]}]

    Returns:
        A list of :class:`ShopeeItem` objects (may be empty).

    Raises:
        SessionExpired, CaptchaRequired, AntiBotBlocked, RateLimited
    """
    norm_cookie = _normalize_cookie(cookie, risktoken)
    csrftoken = _extract_csrftoken(norm_cookie)
    encoded_kw = urllib.parse.quote(keyword)
    referer = f"https://shopee.co.id/search?keyword={encoded_kw}"

    headers = _build_html_headers(
        cookie=norm_cookie,
        csrftoken=csrftoken,
        risktoken=risktoken,
        referer=referer,
    )

    # Build query parameters
    params: dict[str, Any] = {
        "keyword": keyword,
        "limit": limit,
        "offset": newest,
    }

    if fe_filter_options:
        params["fe_filter_options"] = json.dumps(fe_filter_options, separators=(",", ":"))

    # Fetch HTML page
    status, html, _ = await _request_with_retry(
        "GET",
        SHOPEE_SEARCH_PAGE_URL,
        headers=headers,
        params=params,
        raw_response=True,
    )

    # Classify HTTP-level errors
    _classify_html_response(status, html)  # type: ignore[arg-type]

    # Parse __NEXT_DATA__ from the HTML
    next_data = _parse_next_data(html)  # type: ignore[arg-type]

    if next_data is None:
        logger.warning(
            "search_items keyword=%r: __NEXT_DATA__ not found in HTML response "
            "(len=%d). Shopee may not be SSR-ing search results.",
            keyword,
            len(html) if html else 0,
        )
        return []

    # Navigate the JSON to find items — try multiple paths
    page_props = next_data.get("props", {}).get("pageProps", {}) or {}

    raw_items: list = (
        page_props.get("initialSearchResult", {}).get("items")
        or page_props.get("searchResult", {}).get("items")
        or page_props.get("data", {}).get("items")
        or []
    )

    if not raw_items:
        # Try deeper nested paths
        dehydrated = next_data.get("props", {}).get("dehydratedState", {})
        queries = dehydrated.get("queries", []) if isinstance(dehydrated, dict) else []
        for query in queries:
            state_data = (query.get("state", {}).get("data") or {})
            if isinstance(state_data, dict):
                candidate = state_data.get("items") or state_data.get("data", {}).get("items")
                if isinstance(candidate, list) and candidate:
                    raw_items = candidate
                    break

    if not raw_items:
        logger.warning(
            "search_items keyword=%r: __NEXT_DATA__ found but no items in known paths. "
            "Available pageProps keys: %s",
            keyword,
            list(page_props.keys())[:15],
        )
        return []

    # Normalize items
    items: list[ShopeeItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _normalize_item(raw)
        if item is not None:
            items.append(item)

    logger.info(
        "search_items keyword=%r returned %d/%d parsed items (via __NEXT_DATA__)",
        keyword,
        len(items),
        len(raw_items),
    )
    return items


async def get_shop_info(
    shop_id: int,
    cookie: str,
    risktoken: str | None = None,
) -> dict | None:
    """
    Fetch shop details from Shopee's shop details page.

    Parses the ``__NEXT_DATA__`` JSON embedded in the HTML of
    ``https://shopee.co.id/shop/{shop_id}/details``.

    Args:
        shop_id:   Shopee shop identifier.
        cookie:    Raw browser cookie string.
        risktoken: Optional SECSDK token for anti-bot bypass.

    Returns:
        A dict with keys: shop_name, username, is_official_shop,
        follower_count, rating. Returns None if info cannot be extracted.
    """
    norm_cookie = _normalize_cookie(cookie, risktoken)
    csrftoken = _extract_csrftoken(norm_cookie)

    url = SHOPEE_SHOP_DETAILS_URL.format(shop_id=shop_id)
    params = {"shopid": shop_id}

    headers = _build_html_headers(
        cookie=norm_cookie,
        csrftoken=csrftoken,
        risktoken=risktoken,
        referer=f"https://shopee.co.id/shop/{shop_id}",
    )

    try:
        status, html, _ = await _request_with_retry(
            "GET",
            url,
            headers=headers,
            params=params,
            raw_response=True,
        )
    except (httpx.TransportError, httpx.TimeoutException, RateLimited) as exc:
        logger.warning("get_shop_info shop_id=%d network/rate error: %s", shop_id, exc)
        return None

    if status != 200:
        logger.warning("get_shop_info shop_id=%d got HTTP %d", shop_id, status)
        return None

    next_data = _parse_next_data(html)  # type: ignore[arg-type]
    if next_data is None:
        logger.warning(
            "get_shop_info shop_id=%d: __NEXT_DATA__ not found in HTML", shop_id
        )
        return None

    # Navigate to shop info — try multiple paths
    page_props = next_data.get("props", {}).get("pageProps", {}) or {}

    shop_data: dict = (
        page_props.get("shopDetail")
        or page_props.get("shop")
        or page_props.get("data", {}).get("shopDetail")
        or page_props.get("data", {}).get("shop")
        or {}
    )

    if not shop_data:
        # Try dehydratedState queries
        dehydrated = next_data.get("props", {}).get("dehydratedState", {})
        queries = dehydrated.get("queries", []) if isinstance(dehydrated, dict) else []
        for query in queries:
            state_data = query.get("state", {}).get("data") or {}
            if isinstance(state_data, dict):
                candidate = (
                    state_data.get("shopDetail")
                    or state_data.get("shop")
                    or state_data.get("data")
                )
                if isinstance(candidate, dict) and candidate.get("shopid"):
                    shop_data = candidate
                    break

    if not shop_data:
        logger.warning(
            "get_shop_info shop_id=%d: no shop data found in __NEXT_DATA__. "
            "Available pageProps keys: %s",
            shop_id,
            list(page_props.keys())[:15],
        )
        return None

    return {
        "shop_name": _safe_str(
            shop_data.get("shop_name") or shop_data.get("name") or ""
        ),
        "username": _safe_str(
            shop_data.get("username") or shop_data.get("account", {}).get("username") or ""
        ),
        "is_official_shop": bool(
            shop_data.get("is_official_shop")
            or shop_data.get("is_preferred_plus_seller")
        ),
        "follower_count": int(
            shop_data.get("follower_count") or shop_data.get("follower") or 0
        ),
        "rating": float(
            shop_data.get("rating_star")
            or shop_data.get("rating")
            or shop_data.get("shop_rating", {}).get("rating_star")
            or 0.0
        ),
    }


def parse_risktoken_input(text: str) -> RiskToken | None:
    """
    Re-export of :func:`fingerprint.parse_risktoken` for use in command handlers.

    Args:
        text: Raw text input from the Telegram user.

    Returns:
        A :class:`RiskToken` on success, ``None`` otherwise.
    """
    return parse_risktoken(text)
