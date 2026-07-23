"""Monitoring worker lifecycle.

Hubungkan search/inventory hanya ke sumber yang didokumentasikan
dan diotorisasi (SESSION_VALIDATION_URL / inventory API milikmu).
"""
from __future__ import annotations

import asyncio
import logging
import random

from src.config import settings
from src.services.session_service import validate_cookie
from src.utils.crypto import decrypt
from src.utils.database import get_db
from src.utils.telegram import send_message

logger = logging.getLogger(__name__)

_workers: dict[int, asyncio.Task] = {}


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
        f"⚠️ Sesi berakhir: {reason}\n\n"
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
                    "⚠️ Cookie sesi tidak tersedia. Jalankan /setcredentials.",
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

            # --- Integrasi inventory resmi di sini ---
            # Gunakan cookie hanya ke API yang kamu miliki/otorisasi.
            # Jangan kirim password/OTP. Jangan log cookie.
            #
            # Contoh pola (pseudo):
            #   items = await authorized_inventory.search(
            #       cookie=cookie,
            #       keywords=user["keywords"],
            #       area=user["area"],
            #   )
            #   if unauthorized → _disable_for_expired_session(...)
            #   else report ke group via custom bot token (decrypt on use)

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
