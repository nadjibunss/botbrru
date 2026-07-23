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
from src.services.session_service import validate_cookie
from src.utils.crypto import decrypt
from src.utils.database import get_db
from src.utils.telegram import send_message

logger = logging.getLogger(__name__)

_workers: dict[int, asyncio.Task] = {}

SHOPEE_SEARCH_URL = "https://shopee.co.id/api/v4/search/search_items"


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


async def _disable_for_expired_session(telegram_id: int, reason: str) -> None:
    """Stop monitor and ask user to refresh cookie only (no password/OTP)."""
    db = await get_db()

    await db.users.update_one(
        {"telegram_id": telegram_id},
        {
            "$set": {
                "monitoring_active": False,
                "cookie_enc": None,
                "account_username": None,
            }
        },
    )

    await send_message(
        telegram_id,
        f"\u26a0\ufe0f Sesi berakhir: {reason}\n\n"
        "Monitoring dinonaktifkan. Jalankan /setcredentials untuk mengirim cookie baru.",
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

            try:
                cookie = decrypt(cookie_enc)
            except ValueError:
                await _disable_for_expired_session(
                    telegram_id,
                    "data cookie tidak dapat dibaca",
                )
                return

            # Re-validasi sesi lewat endpoint resmi (jika dikonfigurasi)
            if settings.session_validation_url.strip():
                session = await validate_cookie(cookie)
                if not session.valid:
                    await _disable_for_expired_session(
                        telegram_id,
                        session.reason or "cookie tidak valid",
                    )
                    return

            # --- Shopee Search API monitoring ---
            keywords_raw = user.get("keywords") or settings.default_keywords
            keywords = [kw.strip() for kw in keywords_raw.split("|") if kw.strip()]
            checked_items: list = user.get("checked_items") or []
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
                    items = await _search_shopee(cookie, keyword)
                except SessionExpiredError:
                    await _disable_for_expired_session(
                        telegram_id,
                        "Cookie tidak valid atau expired (401/403)",
                    )
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
                    item_id = item.get("itemid")
                    shop_id = item.get("shopid")
                    item_basic = item.get("item_basic") or item

                    stock = item_basic.get("stock", 0)
                    if stock <= 0:
                        continue

                    if item_id in checked_items:
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

                # Sleep antar keyword
                await asyncio.sleep(
                    random.uniform(
                        settings.request_delay_min,
                        settings.request_delay_max,
                    )
                )

            # Update checked_items di DB
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


class SessionExpiredError(Exception):
    """Raised when Shopee returns 401/403."""
    pass


async def _search_shopee(cookie: str, keyword: str) -> list[dict]:
    """
    Hit Shopee Indonesia search API with user's session cookie.
    Returns list of item dicts.
    """
    params = {
        "keyword": keyword,
        "limit": 30,
        "order": "asc",
        "page_type": "search",
        "scenario": "PAGE_GLOBAL_SEARCH",
        "version": 2,
    }

    headers = {
        "Cookie": cookie,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://shopee.co.id/search?keyword={quote_plus(keyword)}",
        "X-Requested-With": "XMLHttpRequest",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            SHOPEE_SEARCH_URL,
            params=params,
            headers=headers,
        )

    if response.status_code in (401, 403):
        raise SessionExpiredError("Unauthorized")

    # Cek response body untuk indikasi unauthorized
    try:
        body = response.json()
    except Exception:
        logger.warning("Non-JSON response from Shopee search: %s", response.status_code)
        return []

    # Cek error di response body
    error_msg = body.get("error_msg") or body.get("error") or ""
    if "unauthorized" in str(error_msg).lower():
        raise SessionExpiredError(error_msg)

    # Parse items - coba kedua struktur response
    items = []

    # Struktur 1: data.items (newer API)
    if "data" in body and body["data"]:
        data = body["data"]
        if isinstance(data, dict):
            items = data.get("items") or []

    # Struktur 2: result.item (older API) - fallback
    if not items and "items" in body:
        items = body.get("items") or []

    if not items and "result" in body:
        result = body.get("result")
        if isinstance(result, dict):
            items = result.get("items") or result.get("item") or []

    return items
