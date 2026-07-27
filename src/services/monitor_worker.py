"""Monitoring worker lifecycle.

Hubungkan search/inventory hanya ke sumber yang didokumentasikan
dan diotorisasi (SESSION_VALIDATION_URL / inventory API milikmu).
"""
from __future__ import annotations

import asyncio
import logging
import random
from urllib.parse import quote_plus

import httpx

from src.config import settings
from src.utils.crypto import decrypt
from src.utils.database import get_db
from src.utils.proxy import ProxyPool, load_proxies, mask
from src.utils.telegram import send_message

logger = logging.getLogger(__name__)

_workers: dict[int, asyncio.Task] = {}

SHOPEE_SEARCH_URL = "https://shopee.co.id/api/v4/search/search_items"

# Maximum number of already-notified item ids kept per user. Prevents the
# MongoDB document from growing unbounded (16MB cap) and keeps membership
# checks fast. Oldest ids are evicted first; an evicted item that comes back
# in stock may notify again — an acceptable trade-off for a large cap.
MAX_CHECKED_ITEMS = 2000

# Shared proxy pool, built lazily from settings on first search.
_proxy_pool: ProxyPool | None = None


def _get_proxy_pool() -> ProxyPool:
    """Build (once) and return the shared proxy pool.

    Priority: PROXY_FILE (rotating list) → PROXY_URL (single) → empty (direct).
    """
    global _proxy_pool
    if _proxy_pool is None:
        proxies = load_proxies(settings.proxy_file)
        if not proxies and settings.proxy_url:
            proxies = [settings.proxy_url]
        _proxy_pool = ProxyPool(proxies, cooldown=settings.proxy_cooldown)
    return _proxy_pool


def proxy_pool_size() -> int:
    """Number of proxies currently loaded (surfaced by /status)."""
    return len(_get_proxy_pool())


class SessionExpiredError(Exception):
    """Raised when Shopee returns genuine session-expired (401 or is_login=false)."""
    pass


class AntiBotError(Exception):
    """Raised when Shopee anti-bot blocks the request (not a session issue)."""
    pass


def _extract_csrftoken(cookie: str) -> str:
    """Extract csrftoken value from a cookie header string."""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("csrftoken="):
            return part.split("=", 1)[1]
    return ""


def _extract_item_fields(item: dict) -> tuple[int | None, int | None, dict]:
    """Return (item_id, shop_id, item_basic) from a Shopee search result entry.

    Shopee nests the core product fields under ``item_basic``; some API
    versions also mirror ``itemid``/``shopid`` at the top level. Read the
    top level first, then fall back to ``item_basic`` so both shapes work.
    """
    item_basic = item.get("item_basic") or item
    item_id = item.get("itemid") or item_basic.get("itemid")
    shop_id = item.get("shopid") or item_basic.get("shopid")
    return item_id, shop_id, item_basic


async def start_worker(telegram_id: int) -> bool:
    """Start one monitoring task per Telegram user."""
    current = _workers.get(telegram_id)

    if current and not current.done():
        return False

    task = asyncio.create_task(_monitor_loop(telegram_id))
    _workers[telegram_id] = task
    return True


async def stop_worker(telegram_id: int) -> bool:
    """Cancel an active worker."""
    task = _workers.get(telegram_id)

    if not task:
        return False

    task.cancel()
    return True


def is_worker_running(telegram_id: int) -> bool:
    """Return current worker status."""
    task = _workers.get(telegram_id)
    return bool(task and not task.done())


async def _disable_for_expired_session(telegram_id: int) -> None:
    """Stop monitor and notify user. Keep cookie for reference (do not clear)."""
    db = await get_db()

    await db.users.update_one(
        {"telegram_id": telegram_id},
        {
            "$set": {
                "monitoring_active": False,
            }
        },
    )

    await send_message(
        telegram_id,
        "\u26a0\ufe0f Sesi Shopee berakhir.\n\n"
        "Monitoring dihentikan. Jalankan /setcredentials untuk cookie baru.",
    )


async def _monitor_loop(telegram_id: int) -> None:
    """Run monitoring loop until stopped or session disappears."""
    try:
        db = await get_db()

        while True:
            user = await db.users.find_one({"telegram_id": telegram_id})

            if not user or not user.get("monitoring_active"):
                return

            cookie_enc = user.get("cookie_enc")
            if not cookie_enc:
                await db.users.update_one(
                    {"telegram_id": telegram_id},
                    {"$set": {"monitoring_active": False}},
                )
                await send_message(
                    telegram_id,
                    "\u26a0\ufe0f Cookie sesi tidak tersedia. Jalankan /setcredentials.",
                )
                return

            # Load risktoken if stored
            risktoken: str | None = None
            risktoken_enc = user.get("risktoken_enc")
            if risktoken_enc:
                try:
                    risktoken = decrypt(risktoken_enc)
                except ValueError:
                    risktoken = None

            try:
                cookie = decrypt(cookie_enc)
            except ValueError:
                await _disable_for_expired_session(telegram_id)
                return

            # --- Shopee Search API monitoring ---
            keywords_raw = user.get("keywords") or settings.default_keywords
            keywords = [kw.strip() for kw in keywords_raw.split("|") if kw.strip()]
            checked_items: list = user.get("checked_items") or []
            checked_set: set = set(checked_items)  # O(1) membership lookups
            new_checked_items: list = list(checked_items)

            # Tentukan target chat (group atau private)
            target_chat = user.get("group_chat_id") or telegram_id

            # Resolve custom bot token jika ada
            bot_token = None
            custom_token_enc = user.get("custom_bot_token_enc")
            if custom_token_enc:
                try:
                    bot_token = decrypt(custom_token_enc)
                except ValueError:
                    bot_token = None

            for keyword in keywords:
                try:
                    items = await _search_shopee(cookie, keyword, risktoken=risktoken)
                except AntiBotError as e:
                    logger.warning(
                        "Anti-bot block untuk user=%s keyword=%s: %s",
                        telegram_id,
                        keyword,
                        e,
                    )
                    # Jangan disable monitoring, cukup sleep lebih lama dan lanjut
                    await asyncio.sleep(random.uniform(60, 120))
                    continue
                except SessionExpiredError:
                    await _disable_for_expired_session(telegram_id)
                    return
                except Exception:
                    logger.exception(
                        "Shopee search failed for user=%s keyword=%s",
                        telegram_id,
                        keyword,
                    )
                    # Lanjut ke keyword berikutnya
                    await asyncio.sleep(
                        random.uniform(
                            settings.request_delay_min,
                            settings.request_delay_max,
                        )
                    )
                    continue

                for item in items:
                    item_id, shop_id, item_basic = _extract_item_fields(item)

                    # Lewati entri tanpa id valid (mencegah link rusak &
                    # None mencemari checked_items)
                    if not item_id or not shop_id:
                        continue

                    stock = item_basic.get("stock", 0)
                    if stock <= 0:
                        continue

                    if item_id in checked_set:
                        continue

                    # Item baru dengan stok tersedia
                    name = item_basic.get("name", "Unknown")
                    price_raw = item_basic.get("price", 0)
                    price = price_raw / 100000  # Shopee micro unit
                    location = item_basic.get("shop_location", "-")
                    shop_name = item_basic.get("shop_name", "shop")

                    link = f"https://shopee.co.id/{quote_plus(shop_name)}-i.{shop_id}.{item_id}"

                    notification = (
                        f"\ud83d\udfe2 STOK TERSEDIA!\n"
                        f"\ud83d\udce6 {name}\n"
                        f"\ud83d\udcb0 Rp{price:,.0f}\n"
                        f"\ud83d\udccd {location}\n"
                        f"\ud83d\udd17 {link}"
                    )

                    await send_message(target_chat, notification, token=bot_token)
                    new_checked_items.append(item_id)
                    checked_set.add(item_id)

                # Sleep antar keyword
                await asyncio.sleep(
                    random.uniform(
                        settings.request_delay_min,
                        settings.request_delay_max,
                    )
                )

            # Update checked_items di DB (batasi ke N terakhir agar dokumen
            # tidak tumbuh tanpa batas)
            if len(new_checked_items) > MAX_CHECKED_ITEMS:
                new_checked_items = new_checked_items[-MAX_CHECKED_ITEMS:]

            if new_checked_items != checked_items:
                await db.users.update_one(
                    {"telegram_id": telegram_id},
                    {"$set": {"checked_items": new_checked_items}},
                )

            # Sleep sebelum cycle berikutnya
            await asyncio.sleep(
                random.uniform(
                    settings.request_delay_min,
                    settings.request_delay_max,
                )
            )

    except asyncio.CancelledError:
        logger.info("Worker cancelled: %s", telegram_id)
        raise

    except Exception:
        logger.exception("Worker failed: %s", telegram_id)

    finally:
        _workers.pop(telegram_id, None)


async def _search_shopee(cookie: str, keyword: str, risktoken: str | None = None) -> list[dict]:
    """
    Hit Shopee Indonesia search API with user's session cookie.
    Returns list of item dicts.

    Raises:
        SessionExpiredError: when the session is genuinely expired (401 or is_login=false)
        AntiBotError: when Shopee anti-bot blocks the request (cookie still valid)
    """
    params = {
        "keyword": keyword,
        "limit": 30,
        "newest": 0,
        "order": "asc",
        "page_type": "search",
        "scenario": "PAGE_GLOBAL_SEARCH",
        "version": 2,
        "by": "relevancy",
        "match_id": 0,
        "src": "search",
        "fs_only": 0,
    }

    # Append RiskSessionID to cookie if risktoken provided
    effective_cookie = cookie
    if risktoken:
        effective_cookie = f"{cookie}; RiskSessionID={risktoken}"

    csrf_token = _extract_csrftoken(effective_cookie)

    headers = {
        "Cookie": effective_cookie,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://shopee.co.id/search?keyword={quote_plus(keyword)}",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "x-shopee-language": "id",
        "x-api-source": "pc",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }

    if csrf_token:
        headers["x-csrftoken"] = csrf_token
    if risktoken:
        headers["x-sz-secsdk-token"] = risktoken

    # --- Request with proxy rotation + anti-bot classification ---
    # A 403 / in-body anti-bot block means the current IP is flagged, so we
    # park that proxy and retry through the next one. A genuine session
    # problem (401 or is_login=false) can't be fixed by another proxy, so it
    # is raised immediately.
    pool = _get_proxy_pool()
    tries = max(1, settings.proxy_max_tries) if not pool.empty else 1

    body: dict | None = None
    last_error: Exception | None = None

    for attempt in range(1, tries + 1):
        used_proxy = pool.get()  # None → direct connection

        try:
            async with httpx.AsyncClient(
                timeout=30,
                proxy=used_proxy,
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    SHOPEE_SEARCH_URL,
                    params=params,
                    headers=headers,
                )
        except (httpx.TransportError, httpx.TimeoutException, ValueError) as exc:
            last_error = exc
            logger.warning(
                "Shopee search transport or configuration error (attempt %d/%d via %s): %s",
                attempt, tries, mask(used_proxy), exc,
            )
            pool.mark_bad(used_proxy)
            continue

        if response.status_code == 401:
            raise SessionExpiredError("HTTP 401 - Unauthorized")

        if response.status_code == 403:
            try:
                body_403 = response.json()
            except Exception:
                body_403 = {}
            if isinstance(body_403, dict) and body_403.get("is_login") is False:
                raise SessionExpiredError("Session tidak valid (is_login=false)")
            logger.warning(
                "Shopee search 403 anti-bot (attempt %d/%d via %s)",
                attempt, tries, mask(used_proxy),
            )
            pool.mark_bad(used_proxy)
            last_error = AntiBotError("HTTP 403 anti-bot")
            continue

        # Parse the (non-401/403) body.
        try:
            parsed = response.json()
        except Exception:
            logger.warning(
                "Non-JSON response from Shopee search: HTTP %s",
                response.status_code,
            )
            pool.mark_good(used_proxy)
            return []

        # In-body anti-bot / session signals (e.g. error 90309999).
        error_code = parsed.get("error")
        error_msg = parsed.get("error_msg") or ""
        is_login = parsed.get("is_login", True)

        if error_code:
            if not is_login:
                raise SessionExpiredError(f"Session expired: {error_msg}")
            logger.warning(
                "Shopee search in-body block %s (attempt %d/%d via %s): %s",
                error_code, attempt, tries, mask(used_proxy), error_msg,
            )
            pool.mark_bad(used_proxy)
            last_error = AntiBotError(f"Shopee error {error_code}: {error_msg}")
            continue

        pool.mark_good(used_proxy)
        body = parsed
        break

    if body is None:
        if isinstance(last_error, SessionExpiredError):
            raise last_error
        scope = "direct" if pool.empty else f"{len(pool)} proxy"
        raise AntiBotError(
            f"Pencarian gagal setelah {tries} percobaan ({scope}): {last_error}"
        )

    # --- Parse items — coba beberapa struktur response ---
    items: list = []

    # Struktur 1: data.items (newer API)
    if "data" in body and body["data"]:
        data = body["data"]
        if isinstance(data, dict):
            items = data.get("items") or []

    # Struktur 2: items di root (fallback)
    if not items and "items" in body:
        items = body.get("items") or []

    # Struktur 3: result.items / result.item (older API)
    if not items and "result" in body:
        result = body.get("result")
        if isinstance(result, dict):
            items = result.get("items") or result.get("item") or []

    return items
