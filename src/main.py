"""FastAPI entry point and Telegram long-polling lifecycle."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.config import settings
from src.handlers.commands import handle_update
from src.services.monitor_worker import start_worker
from src.utils.database import close_db, get_db
from src.utils.telegram import delete_webhook, get_updates

logging.basicConfig(
    # LOG_LEVEL di .env: INFO (default) atau DEBUG untuk diagnosa lebih detail
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

_polling_task: asyncio.Task | None = None


async def polling_loop() -> None:
    """Receive Telegram updates indefinitely."""
    logger.info("Telegram polling started")

    await delete_webhook()

    offset = 0

    while True:
        try:
            updates = await get_updates(offset=offset, timeout=30)

            for update in updates:
                offset = update["update_id"] + 1

                try:
                    await handle_update(update)
                except Exception:
                    logger.exception(
                        "Update processing failed: %s",
                        update.get("update_id"),
                    )

        except asyncio.CancelledError:
            logger.info("Telegram polling stopped")
            raise

        except Exception:
            logger.exception("Telegram polling failure")
            await asyncio.sleep(5)


async def restore_workers() -> None:
    """Restart workers for users with monitoring_active=True."""
    db = await get_db()
    cursor = db.users.find({"monitoring_active": True, "cookie_enc": {"$ne": None}})

    async for user in cursor:
        tid = user["telegram_id"]
        logger.info("Restoring worker for telegram_id=%s", tid)
        await start_worker(tid)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Manage database and polling task."""
    global _polling_task

    await get_db()
    await restore_workers()
    _polling_task = asyncio.create_task(polling_loop())

    try:
        yield
    finally:
        if _polling_task:
            _polling_task.cancel()

            try:
                await _polling_task
            except asyncio.CancelledError:
                pass

        await close_db()


app = FastAPI(
    title="Shopee Monitor Bot",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    """Container health endpoint."""
    return {"status": "ok", "mode": "polling"}
